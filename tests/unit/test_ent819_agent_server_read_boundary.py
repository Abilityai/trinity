"""Download and preview never follow a link (abilityai/trinity-enterprise#819).

The agent server opens the requested path one component at a time without
following any link and serves the descriptor it opened. A path that is a link,
or passes through one, is refused with 403 `resolved_path_mismatch` for every
caller. Real links under a temporary home; the SHIPPED router, loaded by path
(it must stay standalone-importable, #1795).
"""
import importlib.util
import os
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ROUTER = _REPO_ROOT / "docker/base-image/agent_server/routers/files.py"
LINK_CODE = "resolved_path_mismatch"


def _load():
    spec = importlib.util.spec_from_file_location("_test819_agent_files", str(_ROUTER))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def home(tmp_path, monkeypatch):
    mod = _load()
    base = (tmp_path / "home").resolve()
    base.mkdir()
    (base / ".env").write_text("SECRET=1\n")
    (base / "notes.md").write_text("hello notes\n")
    (base / "docs").mkdir()
    (base / "docs/deep.txt").write_text("deep\n")
    (base / ".ssh").mkdir()
    (base / ".ssh/id_rsa").write_text("PRIVATE\n")
    (base / "notes-link").symlink_to(base / ".env")              # a link to a file
    (base / "keys").symlink_to(base / ".ssh")                    # a linked directory
    (base / "dangling").symlink_to(base / "nowhere")             # a dangling link
    (base / "loop").symlink_to(base / "loop")                    # a self-loop
    (base / "pic.png").write_bytes(b"\x89PNG\r\n\x1a\nDATA")
    monkeypatch.setattr(mod, "_HOME", base)
    app = FastAPI()
    app.include_router(mod.router)
    return mod, base, TestClient(app, raise_server_exceptions=True)


def _read(mod, path, base):
    fd, st = mod._open_for_read(path, base)
    with os.fdopen(fd, "rb") as f:
        return f.read(), st


# ---- _open_for_read ---------------------------------------------------------------

@pytest.mark.parametrize("rel", ["notes-link", "keys/id_rsa", "dangling", "loop"])
def test_a_link_anywhere_in_the_path_is_refused(home, rel):
    mod, base, _ = home
    with pytest.raises(HTTPException) as exc:
        mod._open_for_read(rel, base)
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == LINK_CODE


@pytest.mark.parametrize("path,content", [
    ("notes.md", b"hello notes\n"),
    ("docs/deep.txt", b"deep\n"),
    ("docs/../notes.md", b"hello notes\n"),
    (".env/", b"SECRET=1\n"),
])
def test_a_regular_file_opens_and_the_descriptor_holds_its_bytes(home, path, content):
    mod, base, _ = home
    data, st = _read(mod, path, base)
    assert data == content
    assert st.st_size == len(content)


def test_leading_slashes_are_collapsed_before_the_fence(home):
    mod, base, _ = home
    data, _ = _read(mod, "/" + str(base / "notes.md"), base)          # `//<base>/notes.md`
    assert data == b"hello notes\n"


def test_a_missing_file_is_404(home):
    mod, base, _ = home
    with pytest.raises(HTTPException) as exc:
        mod._open_for_read("absent.txt", base)
    assert exc.value.status_code == 404


def test_a_directory_and_a_fifo_are_not_files(home):
    mod, base, _ = home
    os.mkfifo(base / "pipe")
    for rel in ("docs", "pipe", "notes.md/x"):
        with pytest.raises(HTTPException) as exc:
            mod._open_for_read(rel, base)
        assert exc.value.status_code == 400, rel


def test_paths_outside_the_home_are_fenced(home):
    mod, base, _ = home
    for path in ("/proc/self/cwd/notes.md", str(base) + "2/x", "../../etc/passwd", "/etc/passwd"):
        with pytest.raises(HTTPException) as exc:
            mod._open_for_read(path, base)
        assert exc.value.status_code == 403, path
        assert isinstance(exc.value.detail, str), path


def test_the_descriptor_not_the_name_is_what_is_read(home):
    """Swap the opened file for a link to another file before reading: the
    bytes are still the original file's."""
    mod, base, _ = home
    fd, _ = mod._open_for_read("notes.md", base)
    os.unlink(base / "notes.md")
    (base / "notes.md").symlink_to(base / ".env")
    with os.fdopen(fd, "rb") as f:
        assert f.read() == b"hello notes\n"


# ---- handlers ----------------------------------------------------------------------

@pytest.mark.parametrize("route", ["/api/files/download", "/api/files/preview"])
@pytest.mark.parametrize("rel", ["notes-link", "keys/id_rsa", "loop"])
def test_the_handlers_refuse_a_link(home, route, rel):
    _, _, client = home
    r = client.get(route, params={"path": rel})
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["code"] == LINK_CODE
    assert "link" in r.json()["detail"]["message"]


def test_download_serves_a_regular_file(home):
    _, _, client = home
    r = client.get("/api/files/download", params={"path": "notes.md"})
    assert r.status_code == 200
    assert r.text == "hello notes\n"


def test_preview_serves_a_regular_file_inline(home):
    _, _, client = home
    r = client.get("/api/files/preview", params={"path": "pic.png"})
    assert r.status_code == 200
    assert r.content == b"\x89PNG\r\n\x1a\nDATA"
    assert r.headers["content-type"] == "image/png"
    assert r.headers["content-length"] == str(len(b"\x89PNG\r\n\x1a\nDATA"))
    assert r.headers["content-disposition"] == 'inline; filename="pic.png"'


@pytest.mark.parametrize("route", ["/api/files/download", "/api/files/preview"])
def test_the_handlers_serve_what_they_opened(home, monkeypatch, route):
    """A swap between the open and the read changes nothing the caller sees."""
    mod, base, client = home
    real = mod._open_for_read

    def _open_then_swap(path, base_=None):
        opened = real(path, base_)
        os.unlink(base / "notes.md")
        (base / "notes.md").symlink_to(base / ".env")
        return opened

    monkeypatch.setattr(mod, "_open_for_read", _open_then_swap)
    r = client.get(route, params={"path": "notes.md"})
    assert r.status_code == 200
    assert r.content == b"hello notes\n"


@pytest.mark.parametrize("route", ["/api/files/download", "/api/files/preview"])
def test_the_handlers_keep_404_and_400(home, route):
    _, _, client = home
    assert client.get(route, params={"path": "absent.txt"}).status_code == 404
    assert client.get(route, params={"path": "docs"}).status_code == 400
