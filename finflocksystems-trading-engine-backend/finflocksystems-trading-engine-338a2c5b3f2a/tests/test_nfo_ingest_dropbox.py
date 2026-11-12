"""Shared-folder acquirer against a fake Dropbox. No network."""
from datetime import date

import pytest
import requests

from nfo_ingestion import dropbox_shared as ds

LINK = "https://www.dropbox.com/scl/fo/SECRETLINKID/AAA?rlkey=SECRETKEY&dl=0"
ENV = {"DROPBOX_APP_KEY": "key", "DROPBOX_APP_SECRET": "sec-sec", "DROPBOX_REFRESH_TOKEN": "rt-SECRET", "GDF_DROPBOX_SHARED_LINK": LINK}


class IngestError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class Reply:
    def __init__(self, status=200, body=None, content=b""):
        self.status_code, self._body, self._content = status, body or {}, content

    def json(self):
        return self._body

    def iter_content(self, size):
        yield self._content


def file(name, size, modified="2026-11-02T17:00:00Z"):
    return {".tag": "file", "name": name, "size": size, "server_modified": modified}


def folder(name):
    return {".tag": "folder", "name": name}


TREE = {
    "": [folder("NFO")],
    "/NFO": [folder("2026"), file("readme.txt", 5)],
    "/NFO/2026": [folder("OCT_2026"), folder("NOV_2026")],
    "/NFO/2026/OCT_2026": [file("GFDLNFO_CONTRACT_30102026.zip", 4), file("GFDLNFO_CONTRACT_30102026.zip.tmp", 1)],
    "/NFO/2026/NOV_2026": [file("GFDLNFO_CONTRACT_02112026.zip", 3), file("GFDLNFO_CONTRACT_03112026.zip", 6, "2026-11-03T15:30:00Z")],
}
CONTENT = {"/NFO/2026/OCT_2026/GFDLNFO_CONTRACT_30102026.zip": b"oct1", "/NFO/2026/NOV_2026/GFDLNFO_CONTRACT_02112026.zip": b"nov",
           "/NFO/2026/NOV_2026/GFDLNFO_CONTRACT_03112026.zip": b"nov333"}


class FakeDropbox:
    def __init__(self, tree=TREE, token_status=200, download_status=200, truncate=False, error=None):
        self.tree, self.token_status, self.download_status, self.truncate, self.error = tree, token_status, download_status, truncate, error
        self.calls, self.downloads = [], []

    def post(self, url, **kw):
        self.calls.append((url, kw.get("json")))
        if self.error:
            raise self.error
        if url == ds.TOKEN_URL:
            return Reply(self.token_status, {"access_token": "AT"})
        if url == ds.LIST_URL:
            assert kw["json"]["recursive"] is False, "a shared link cannot be listed recursively"
            assert kw["json"]["shared_link"] == {"url": LINK}
            path = kw["json"]["path"]
            return Reply(200, {"entries": self.tree[path], "has_more": False}) if path in self.tree else Reply(409, {})
        if url == ds.DOWNLOAD_URL:
            import json
            path = json.loads(kw["headers"]["Dropbox-API-Arg"])["path"]
            self.downloads.append(path)
            content = CONTENT[path]
            return Reply(self.download_status, content=content[:-1] if self.truncate else content)
        raise AssertionError(url)


def acquirer(http=None, env=None):
    return ds.SharedFolderAcquirer(IngestError, session=http or FakeDropbox(), env=ENV if env is None else env)


def test_walks_the_folder_level_by_level_and_finds_only_daily_zips():
    items = acquirer().scan()
    assert [(i["session"].isoformat(), i["path"]) for i in items] == [
        ("2026-10-30", "/NFO/2026/OCT_2026/GFDLNFO_CONTRACT_30102026.zip"),
        ("2026-11-02", "/NFO/2026/NOV_2026/GFDLNFO_CONTRACT_02112026.zip"),
        ("2026-11-03", "/NFO/2026/NOV_2026/GFDLNFO_CONTRACT_03112026.zip")]


def test_list_sessions_returns_only_those_newer_than_the_ledger():
    assert acquirer().list_sessions(date(2026, 11, 2)) == [date(2026, 11, 3)]
    assert len(acquirer().list_sessions(None)) == 3


def test_downloads_only_new_sessions_into_the_providers_layout(tmp_path):
    http = FakeDropbox()
    out = acquirer(http).acquire(tmp_path, date(2026, 11, 2), have=set())
    assert out["downloaded"] == ["2026-11-03"] and http.downloads == ["/NFO/2026/NOV_2026/GFDLNFO_CONTRACT_03112026.zip"]
    target = tmp_path / "NFO" / "2026" / "NOV_2026" / "GFDLNFO_CONTRACT_03112026.zip"
    assert target.read_bytes() == b"nov333" and not list(tmp_path.rglob("*.part"))


def test_provider_timestamp_is_restored_so_the_stable_file_window_passes(tmp_path):
    acquirer().acquire(tmp_path, date(2026, 11, 2), have=set())
    target = tmp_path / "NFO" / "2026" / "NOV_2026" / "GFDLNFO_CONTRACT_03112026.zip"
    from datetime import datetime, timezone
    assert target.stat().st_mtime == datetime(2026, 11, 3, 15, 30, tzinfo=timezone.utc).timestamp()


def test_sessions_already_on_disk_or_in_have_are_not_downloaded(tmp_path):
    http = FakeDropbox()
    acquirer(http).acquire(tmp_path, date(2026, 10, 1), have={date(2026, 10, 30), date(2026, 11, 2)})
    assert http.downloads == ["/NFO/2026/NOV_2026/GFDLNFO_CONTRACT_03112026.zip"]
    http2 = FakeDropbox()
    existing = tmp_path / "NFO" / "2026" / "NOV_2026"
    existing.mkdir(parents=True, exist_ok=True)
    (existing / "GFDLNFO_CONTRACT_03112026.zip").write_bytes(b"nov333")   # same size: already complete
    assert acquirer(http2).acquire(tmp_path, date(2026, 11, 2), have=set())["downloaded"] == [] and http2.downloads == []


def test_one_specific_session_can_be_fetched_with_the_pipelines_trick(tmp_path):
    """The worker passes latest = day before and every other known session as `have` to fetch exactly one file."""
    http = FakeDropbox()
    out = acquirer(http).acquire(tmp_path, date(2026, 11, 1), have={date(2026, 10, 30), date(2026, 11, 3)})
    assert out["downloaded"] == ["2026-11-02"] and http.downloads == ["/NFO/2026/NOV_2026/GFDLNFO_CONTRACT_02112026.zip"]


def test_an_incomplete_download_is_rejected_and_leaves_nothing_behind(tmp_path):
    with pytest.raises(IngestError) as info:
        acquirer(FakeDropbox(truncate=True)).acquire(tmp_path, date(2026, 11, 2), have=set())
    assert info.value.code == "download_failed" and "Incomplete" in str(info.value)
    assert list(tmp_path.rglob("*.zip")) == [] and list(tmp_path.rglob("*.part")) == []


def test_http_errors_become_download_failed_without_leaking_secrets(tmp_path):
    for http in (FakeDropbox(token_status=401), FakeDropbox(download_status=500), FakeDropbox(error=requests.ConnectionError(f"boom {LINK} rt-SECRET"))):
        with pytest.raises(IngestError) as info:
            acquirer(http).acquire(tmp_path, date(2026, 11, 2), have=set())
        text = str(info.value)
        assert info.value.code == "download_failed" and "SECRET" not in text and "rlkey" not in text and "AT" != text


def test_missing_configuration_is_reported_by_name():
    with pytest.raises(IngestError, match="GDF_DROPBOX_SHARED_LINK is not configured"):
        acquirer(env={k: v for k, v in ENV.items() if k != "GDF_DROPBOX_SHARED_LINK"}).scan()


def test_nothing_is_ever_written_outside_the_input_directory(tmp_path):
    evil = {"": [folder("..")], "/..": [file("GFDLNFO_CONTRACT_04112026.zip", 3)]}
    CONTENT["/../GFDLNFO_CONTRACT_04112026.zip"] = b"bad"
    http = FakeDropbox(tree=evil)
    inner = tmp_path / "input"
    inner.mkdir()
    acquirer(http).acquire(inner, date(2026, 11, 2), have=set())
    assert http.downloads == [] and not (tmp_path / "GFDLNFO_CONTRACT_04112026.zip").exists()


def test_a_runaway_folder_tree_is_refused():
    tree = {"": [folder(f"d{i}") for i in range(500)]}
    tree.update({f"/d{i}": [] for i in range(500)})
    with pytest.raises(IngestError, match="more sub-folders than expected"):
        acquirer(FakeDropbox(tree=tree)).scan()
