"""A link planted in a shared folder never reaches a protected file, and the
agent server's own writers never write through a link (trinity-enterprise#822).

The shared-folder case: agent A writes its `shared-out`; agent B mounts it at
`/home/developer/shared-in/A/`. An ABSOLUTE link A plants there resolves inside
B's mount namespace, so `shared-in/A/s -> /home/developer/.claude` makes
`shared-in/A/s/settings.json` land on B's own instructions directory. The file
routes refuse it (the ent#820 no-follow walk + class check); pinned here for this
exact shape, which the ent#820 tests (links made inside one home) do not plant.
Reproduced live in two real containers linked by a shared folder on 2026-10-22:
PUT, DELETE, download, preview and list through the link were all 403
`resolved_path_mismatch`, B's settings.json hash unchanged, an ordinary shared
file downloaded 200.

The same issue named the other writers that materialise a path in a home. Two
still followed links: credential injection and snapshot restore resolved the
target and wrote to whatever it reached. Both now write through
`files.write_bytes_nofollow`, so a link in a parent or as the file itself is
refused (credential) or skipped (snapshot member) instead of redirecting the
write.
"""
import base64
import importlib.util
import io
import stat
import sys
import tarfile
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[2]
_BASE_IMAGE = _REPO_ROOT / "docker" / "base-image"
_ROUTER = _BASE_IMAGE / "agent_server/routers/files.py"
if str(_BASE_IMAGE) not in sys.path:
    sys.path.insert(0, str(_BASE_IMAGE))


def _load_files():
    spec = importlib.util.spec_from_file_location("_test822_agent_files", str(_ROUTER))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _is_link_refusal(r):
    return r.status_code == 403 and r.json()["detail"]["code"] == "resolved_path_mismatch"


@pytest.fixture
def consumer(tmp_path, monkeypatch):
    """Agent B's home: its own instructions, and agent A's shared folder mounted
    at shared-in/A holding an ordinary file and an ABSOLUTE link to B's `.claude`
    (absolute, as a link planted by another agent resolves in B's namespace)."""
    mod = _load_files()
    base = (tmp_path / "home").resolve()
    (base / ".claude").mkdir(parents=True)
    (base / ".claude/settings.json").write_text('{"hooks": "owner"}')
    shared = base / "shared-in/A"
    shared.mkdir(parents=True)
    (shared / "note.txt").write_text("hello from A")
    (shared / "s").symlink_to(base / ".claude")
    monkeypatch.setattr(mod, "_HOME", base)
    app = FastAPI()
    app.include_router(mod.router)
    return mod, base, TestClient(app, raise_server_exceptions=True)


THROUGH = "shared-in/A/s/settings.json"


class TestAShareFolderLinkNeverReachesTheConsumersFiles:
    def test_a_write_through_it_is_refused(self, consumer):
        _mod, base, client = consumer
        r = client.put("/api/files", params={"path": THROUGH}, json={"content": '{"hijacked": 1}'})
        assert _is_link_refusal(r), r.text
        assert (base / ".claude/settings.json").read_text() == '{"hooks": "owner"}'

    def test_a_delete_through_it_is_refused(self, consumer):
        _mod, base, client = consumer
        assert _is_link_refusal(client.delete("/api/files", params={"path": THROUGH}))
        assert (base / ".claude/settings.json").exists()

    @pytest.mark.parametrize("route", ["/api/files/download", "/api/files/preview"])
    def test_a_read_through_it_is_refused(self, consumer, route):
        _mod, _base, client = consumer
        assert _is_link_refusal(client.get(route, params={"path": THROUGH}))

    def test_listing_it_is_refused(self, consumer):
        _mod, _base, client = consumer
        assert _is_link_refusal(client.get("/api/files", params={"path": "shared-in/A/s"}))

    def test_an_ordinary_shared_file_stays_readable(self, consumer):
        _mod, _base, client = consumer
        r = client.get("/api/files/download", params={"path": "shared-in/A/note.txt"})
        assert r.status_code == 200 and "hello from A" in r.text


# ---- the agent server's own writers -------------------------------------------------


def _credentials(monkeypatch, home):
    from agent_server.routers import credentials
    monkeypatch.setattr(credentials, "_HOME", home)
    return credentials


@pytest.fixture
def home(tmp_path):
    base = (tmp_path / "home").resolve()
    (base / ".claude").mkdir(parents=True)
    (base / ".claude/settings.json").write_text("owner settings")
    return base


class TestCredentialInjectionNeverWritesThroughALink:
    def test_a_link_in_a_parent_directory_is_refused(self, monkeypatch, home):
        (home / ".config").symlink_to(home / ".claude")      # .config/gcloud/... -> .claude/gcloud/...
        creds = _credentials(monkeypatch, home)
        with pytest.raises(HTTPException) as exc:
            creds._write_credential_file(".config/gcloud/adc.json", text='{"k": 1}')
        assert exc.value.status_code == 403
        assert not (home / ".claude/gcloud").exists()

    def test_a_link_as_the_file_itself_is_refused(self, monkeypatch, home):
        (home / ".env").symlink_to(home / ".claude/settings.json")
        creds = _credentials(monkeypatch, home)
        with pytest.raises(HTTPException) as exc:
            creds._write_credential_file(".env", text="KEY=v")
        assert exc.value.status_code == 403
        assert (home / ".claude/settings.json").read_text() == "owner settings"
        assert (home / ".env").is_symlink()                    # refused, not replaced

    @pytest.mark.parametrize("kwargs,expected", [
        ({"text": "KEY=v"}, b"KEY=v"),
        ({"b64": base64.b64encode(b"\x00bin").decode()}, b"\x00bin"),
    ])
    def test_an_ordinary_credential_is_written_0600(self, monkeypatch, home, kwargs, expected):
        creds = _credentials(monkeypatch, home)
        rel = ".env" if "text" in kwargs else ".config/gcloud/sa.json"
        assert creds._write_credential_file(rel, **kwargs) == rel
        target = home / rel
        assert target.read_bytes() == expected
        assert stat.S_IMODE(target.stat().st_mode) == 0o600

    def test_a_disallowed_path_is_still_refused_first(self, monkeypatch, home):
        creds = _credentials(monkeypatch, home)
        with pytest.raises(HTTPException) as exc:
            creds._write_credential_file(".bashrc", text="evil")
        assert exc.value.status_code == 400


def _tar(members):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class TestSnapshotRestoreNeverWritesThroughALink:
    def _restore(self, home, members):
        from agent_server.routers import snapshot
        return snapshot.restore_from_tar(home, _tar(members), ["data/**"])

    def test_a_member_under_a_linked_directory_is_skipped_the_rest_restore(self, home):
        (home / "data").symlink_to(home / ".claude")          # data/settings.json -> .claude/settings.json
        restored, skipped = self._restore(home, {"data/settings.json": b"hijacked"})
        assert skipped == ["data/settings.json"] and restored == []
        assert (home / ".claude/settings.json").read_text() == "owner settings"

    def test_a_member_that_is_a_link_is_skipped(self, home):
        (home / "data").mkdir()
        (home / "data/app.db").symlink_to(home / ".claude/settings.json")
        restored, skipped = self._restore(home, {"data/app.db": b"hijacked", "data/ok.db": b"GOOD"})
        assert restored == ["data/ok.db"] and skipped == ["data/app.db"]
        assert (home / ".claude/settings.json").read_text() == "owner settings"
        assert (home / "data/ok.db").read_bytes() == b"GOOD"


@pytest.mark.parametrize("rel", ["", "/abs/path", "../escape", "a/../../b"])
def test_the_writer_refuses_a_path_its_callers_should_never_pass(tmp_path, rel):
    mod = _load_files()
    with pytest.raises(HTTPException) as exc:
        mod.write_bytes_nofollow(rel, b"x", base=tmp_path)
    assert exc.value.status_code == 400
