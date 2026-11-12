"""GDF data acquisition. `local` only reports; `dropbox_api` downloads missing files
from a Dropbox shared folder. The shared link and credentials come from env vars
only and are never logged or returned."""
from __future__ import annotations

import os
import re
from datetime import date, datetime, timezone
from pathlib import Path

import requests

from .service import IngestError

DAILY_RE = re.compile(r"GFDLNFO_CONTRACT_(\d{2})(\d{2})(\d{4})\.zip$", re.I)
TOKEN_URL = "https://api.dropboxapi.com/oauth2/token"
LIST_URL = "https://api.dropboxapi.com/2/files/list_folder"
LIST_MORE_URL = "https://api.dropboxapi.com/2/files/list_folder/continue"
DOWNLOAD_URL = "https://content.dropboxapi.com/2/sharing/get_shared_link_file"


def daily_session(name: str) -> date | None:
    match = DAILY_RE.search(name)
    return date(int(match[3]), int(match[2]), int(match[1])) if match else None


class LocalAcquirer:
    def acquire(self, input_dir: Path, latest_db: date | None, have: set[date]) -> dict:
        return {"downloaded": [], "message": "Using local data."}


class DropboxAcquirer:
    """Needs the Dropbox app scopes files.metadata.read and sharing.read."""

    def __init__(self, session=None, env=None):
        self.http = session or requests.Session()
        self.env = env if env is not None else os.environ

    def _need(self, name: str) -> str:
        value = self.env.get(name, "").strip()
        if not value:
            raise IngestError("download_failed", f"{name} is not configured.")
        return value

    def _token(self) -> str:
        resp = self.http.post(TOKEN_URL, data={
            "grant_type": "refresh_token", "refresh_token": self._need("DROPBOX_REFRESH_TOKEN"),
        }, auth=(self._need("DROPBOX_APP_KEY"), self._need("DROPBOX_APP_SECRET")), timeout=30)
        if resp.status_code != 200:
            raise IngestError("download_failed", "Dropbox authentication failed; check the app credentials.")
        return resp.json()["access_token"]

    def _list(self, headers: dict, link: str) -> list[dict]:
        resp = self.http.post(LIST_URL, headers=headers, timeout=60, json={
            "path": "", "shared_link": {"url": link}, "recursive": True,
        })
        entries = []
        while True:
            if resp.status_code != 200:
                raise IngestError("download_failed", f"Dropbox could not list the shared folder (HTTP {resp.status_code}).")
            body = resp.json()
            entries += [e for e in body["entries"] if e.get(".tag") == "file"]
            if not body.get("has_more"):
                return entries
            resp = self.http.post(LIST_MORE_URL, headers=headers, json={"cursor": body["cursor"]}, timeout=60)

    def acquire(self, input_dir: Path, latest_db: date | None, have: set[date]) -> dict:
        link = self._need("GDF_DROPBOX_SHARED_LINK")
        token = self._token()
        headers = {"Authorization": f"Bearer {token}"}
        wanted = []
        for entry in self._list(headers, link):
            session = daily_session(entry["name"])
            if session is None or session in have or (latest_db and session <= latest_db):
                continue
            rel = entry.get("path_display", "/" + entry["name"]).lstrip("/")
            wanted.append((session, rel, entry))
        downloaded = []
        for session, rel, entry in sorted(wanted, key=lambda w: w[0]):
            target = (Path(input_dir) / rel).resolve()
            if Path(input_dir).resolve() not in target.parents:
                continue  # never write outside the pipeline dir
            if target.is_file() and target.stat().st_size == entry.get("size"):
                continue
            self._download(headers, link, "/" + rel, target, entry)
            downloaded.append(str(session))
        msg = f"Downloaded {len(downloaded)} new session file(s)." if downloaded else "No new files on Dropbox."
        return {"downloaded": downloaded, "message": msg}

    def _download(self, headers: dict, link: str, remote_path: str, target: Path, entry: dict) -> None:
        import json
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_name(target.name + ".part")
        try:
            resp = self.http.post(DOWNLOAD_URL, stream=True, timeout=(30, 300), headers={
                **headers, "Dropbox-API-Arg": json.dumps({"url": link, "path": remote_path}),
            })
            if resp.status_code != 200:
                raise IngestError("download_failed", f"Dropbox download failed for {target.name} (HTTP {resp.status_code}).")
            with temp.open("wb") as out:
                for block in resp.iter_content(1024 * 1024):
                    out.write(block)
            if entry.get("size") is not None and temp.stat().st_size != entry["size"]:
                raise IngestError("download_failed", f"Incomplete download for {target.name}.")
            os.replace(temp, target)
        except requests.RequestException:
            raise IngestError("download_failed", f"Network error while downloading {target.name}.") from None
        finally:
            temp.unlink(missing_ok=True)
        # Keep the provider's timestamp so the runner's file-stability window doesn't reject a fresh download.
        modified = entry.get("server_modified")
        if modified:
            ts = datetime.fromisoformat(modified.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()
            os.utime(target, (ts, ts))


def get_acquirer(name: str):
    if name == "local":
        return LocalAcquirer()
    if name == "dropbox_api":
        return DropboxAcquirer()
    raise IngestError("download_failed", f"Unknown GDF_ACQUIRER '{name}' (use 'local' or 'dropbox_api').")
