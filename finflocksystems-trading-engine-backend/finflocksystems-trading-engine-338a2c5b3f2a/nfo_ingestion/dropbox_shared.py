"""Dropbox shared-folder acquisition for the nightly ingestion (GDF_ACQUIRER=dropbox_shared).

Replaces `nfo_pipeline.acquire.DropboxAcquirer`, which cannot work against the provider's real folder: Dropbox refuses a recursive
listing of a shared link ("Recursive list folder is not supported for shared link"), and entries listed through a shared link carry no
`path_display`, so that class would also request every nested file from the wrong path. `nfo_pipeline` is not edited; the worker uses
this module instead. Everything else behaves the same:

  * only daily files GFDLNFO_CONTRACT_DDMMYYYY.zip, and only sessions newer than the DB ledger and not already on disk
  * each download goes to `<name>.part`, the size is checked, then it is renamed atomically
  * the provider's modification time is restored so the pipeline's 120 s stable-file window passes
  * files keep the provider's folder layout under input_dir, and nothing is ever written outside input_dir

Credentials and the shared link come from environment variables only (DROPBOX_APP_KEY, DROPBOX_APP_SECRET, DROPBOX_REFRESH_TOKEN,
GDF_DROPBOX_SHARED_LINK) and are never logged or placed in an error message. Needs the Dropbox scopes files.metadata.read,
files.content.read and sharing.read.
"""
from __future__ import annotations

import json
import os
import re
from datetime import date, datetime, timezone
from pathlib import Path

import requests

DAILY_RE = re.compile(r"GFDLNFO_CONTRACT_(\d{2})(\d{2})(\d{4})\.zip$", re.I)
TOKEN_URL = "https://api.dropboxapi.com/oauth2/token"
LIST_URL = "https://api.dropboxapi.com/2/files/list_folder"
LIST_MORE_URL = "https://api.dropboxapi.com/2/files/list_folder/continue"
DOWNLOAD_URL = "https://content.dropboxapi.com/2/sharing/get_shared_link_file"
MAX_FOLDERS, MAX_DEPTH = 400, 6


def daily_session(name: str) -> date | None:
    match = DAILY_RE.search(name)
    return date(int(match[3]), int(match[2]), int(match[1])) if match else None


class SharedFolderAcquirer:
    def __init__(self, error_class, session=None, env=None):
        self.error = error_class            # nfo_pipeline.service.IngestError: (code, message)
        self.http = session or requests.Session()
        self.env = env if env is not None else os.environ

    def _fail(self, message):
        return self.error("download_failed", message)

    def _need(self, name):
        value = str(self.env.get(name, "")).strip()
        if not value:
            raise self._fail(f"{name} is not configured.")
        return value

    def _token(self):
        try:
            reply = self.http.post(TOKEN_URL, data={"grant_type": "refresh_token", "refresh_token": self._need("DROPBOX_REFRESH_TOKEN")},
                                   auth=(self._need("DROPBOX_APP_KEY"), self._need("DROPBOX_APP_SECRET")), timeout=30)
        except requests.RequestException:
            raise self._fail("Network error while contacting Dropbox.") from None
        if reply.status_code != 200:
            raise self._fail("Dropbox authentication failed; check the app credentials and scopes.")
        return reply.json()["access_token"]

    def _list_dir(self, headers, link, path):
        entries = []
        try:
            reply = self.http.post(LIST_URL, headers=headers, timeout=60,
                                   json={"path": path, "shared_link": {"url": link}, "recursive": False})
            while True:
                if reply.status_code != 200:
                    raise self._fail(f"Dropbox could not list the shared folder (HTTP {reply.status_code}).")
                body = reply.json()
                entries += body["entries"]
                if not body.get("has_more"):
                    return entries
                reply = self.http.post(LIST_MORE_URL, headers=headers, json={"cursor": body["cursor"]}, timeout=60)
        except requests.RequestException:
            raise self._fail("Network error while listing the shared folder.") from None

    def scan(self, headers=None):
        """Every daily file in the shared folder: [{session, path, name, size, modified}], found by a level-by-level walk."""
        link = self._need("GDF_DROPBOX_SHARED_LINK")
        headers = headers or {"Authorization": f"Bearer {self._token()}"}
        found, pending, visited = [], [("", 0)], 0
        while pending:
            path, depth = pending.pop()
            visited += 1
            if visited > MAX_FOLDERS:
                raise self._fail("The shared folder has more sub-folders than expected; refusing to walk it.")
            for entry in self._list_dir(headers, link, path):
                child = f"{path}/{entry['name']}"
                if entry.get(".tag") == "folder":
                    if depth < MAX_DEPTH:
                        pending.append((child, depth + 1))
                elif entry.get(".tag") == "file" and daily_session(entry["name"]):
                    found.append({"session": daily_session(entry["name"]), "path": child, "name": entry["name"],
                                  "size": entry.get("size"), "modified": entry.get("server_modified")})
        return sorted(found, key=lambda item: item["session"])

    def list_sessions(self, latest_db=None):
        return sorted({item["session"] for item in self.scan() if latest_db is None or item["session"] > latest_db})

    def acquire(self, input_dir, latest_db, have):
        link = self._need("GDF_DROPBOX_SHARED_LINK")
        headers = {"Authorization": f"Bearer {self._token()}"}
        root = Path(input_dir).resolve()
        downloaded = []
        for item in self.scan(headers):
            session = item["session"]
            if session in have or (latest_db and session <= latest_db):
                continue
            target = (Path(input_dir) / item["path"].lstrip("/")).resolve()
            if root not in target.parents:
                continue  # never write outside the pipeline directory
            if target.is_file() and target.stat().st_size == item["size"]:
                continue
            self._download(headers, link, item, target)
            downloaded.append(str(session))
        message = f"Downloaded {len(downloaded)} new session file(s)." if downloaded else "No new files on Dropbox."
        return {"downloaded": downloaded, "message": message}

    def _download(self, headers, link, item, target):
        target.parent.mkdir(parents=True, exist_ok=True)
        part = target.with_name(target.name + ".part")
        try:
            reply = self.http.post(DOWNLOAD_URL, stream=True, timeout=(30, 300),
                                   headers={**headers, "Dropbox-API-Arg": json.dumps({"url": link, "path": item["path"]})})
            if reply.status_code != 200:
                raise self._fail(f"Dropbox download failed for {item['name']} (HTTP {reply.status_code}).")
            with part.open("wb") as out:
                for block in reply.iter_content(1024 * 1024):
                    out.write(block)
            if item["size"] is not None and part.stat().st_size != item["size"]:
                raise self._fail(f"Incomplete download for {item['name']}.")
            os.replace(part, target)
        except requests.RequestException:
            raise self._fail(f"Network error while downloading {item['name']}.") from None
        finally:
            part.unlink(missing_ok=True)
        if item["modified"]:  # keep the provider's timestamp so the pipeline's stable-file window does not reject a fresh download
            stamp = datetime.fromisoformat(item["modified"].replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()
            os.utime(target, (stamp, stamp))
