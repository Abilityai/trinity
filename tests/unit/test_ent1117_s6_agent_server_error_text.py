"""Agent server: no raw exception text in an API answer (trinity-enterprise#1117,
trinity#4284), the snapshot `backup_dir` barrier (#153) and the dashboard
answers (#30, #31).

Each changed 5xx site is driven through its real handler with an exception
whose text carries a secret-looking string (an `sk-` key and a credential in a
URL's userinfo) plus a harmless marker. The answer must carry the exception's
class name and neither the secret nor the marker; the log record must carry the
marker (so the operator still has the diagnostic) with the secret scrubbed.

Characterization tests (green before the fix too) are named as such.
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import os
from pathlib import Path

import pytest
from fastapi import HTTPException

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[2]
_AGENT_SERVER = _REPO / "docker" / "base-image" / "agent_server"

# Built by concatenation so no source line (and so no traceback frame) holds it.
_SECRET = "sk-ant-api03-" + "S6SENTINEL" + "x" * 24
_MARKER = "boom-marker-s6"
_PLANT = (
    f"{_MARKER} https://x-access-token:{_SECRET}@github.com/acme/repo.git key={_SECRET}"
)


def _load(rel: str, name: str):
    spec = importlib.util.spec_from_file_location(name, str(_AGENT_SERVER / rel))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _rendered(record: logging.LogRecord) -> str:
    """The record as a handler would write it, traceback included."""
    return logging.Formatter("%(message)s").format(record)


def _assert_answer_clean(detail, class_name: str) -> None:
    text = str(detail)
    assert _SECRET not in text, f"secret reached the answer: {text!r}"
    assert _MARKER not in text, f"exception text reached the answer: {text!r}"
    assert class_name in text, f"class name missing from the answer: {text!r}"


def _assert_log_scrubbed(records, needle: str) -> None:
    """Some record mentioning `needle` carries the marker; no record carries the secret."""
    rendered = [_rendered(r) for r in records]
    for line in rendered:
        assert _SECRET not in line, f"secret reached the log: {line!r}"
    hits = [line for line in rendered if needle in line and _MARKER in line]
    assert hits, f"no {needle!r} record carries the exception text: {rendered!r}"


# ---------------------------------------------------------------------------
# #153 — snapshot backup_dir
# ---------------------------------------------------------------------------


@pytest.fixture
def snapshot(tmp_path, monkeypatch):
    mod = _load("routers/snapshot.py", "_ent1117_s6_snapshot")
    home = (tmp_path / "home").resolve()
    home.mkdir()
    (home / "workspace").mkdir()
    (home / "workspace" / "a.txt").write_text("a")
    monkeypatch.setattr(mod, "_HOME", home)
    return mod, home


def _snap(mod, backup_dir):
    return asyncio.run(
        mod.snapshot_endpoint({"paths": ["workspace/**"], "backup_dir": backup_dir})
    )


def test_snapshot_backup_dir_dot_is_accepted(snapshot):
    """`"."` writes the snapshot into the home itself, as it does today."""
    mod, home = snapshot
    resp = _snap(mod, ".")
    assert (home / "snapshot.tar").is_file()
    assert resp.headers["X-Snapshot-Path"] == "snapshot.tar"


def test_snapshot_backup_dir_subdirectory_is_accepted(snapshot):
    mod, home = snapshot
    resp = _snap(mod, "backups/run-1")
    assert (home / "backups" / "run-1" / "snapshot.tar").is_file()
    assert resp.headers["X-Snapshot-Path"] == "backups/run-1/snapshot.tar"


@pytest.mark.parametrize(
    "backup_dir",
    ["..", "../escape", "/etc", "a/../../escape", "../home2", "../home-x/sub"],
)
def test_snapshot_backup_dir_outside_home_is_refused(snapshot, backup_dir):
    """Characterization: refused today by `relative_to`, and still refused with
    the barrier in front of it. `../home2` is the sibling-prefix case the
    trailing separator on the base closes."""
    mod, home = snapshot
    with pytest.raises(HTTPException) as exc:
        _snap(mod, backup_dir)
    assert exc.value.status_code == 400
    assert exc.value.detail == "backup_dir must be inside /home/developer"
    assert not list(home.parent.glob("**/snapshot.tar"))


def test_snapshot_backup_dir_through_a_link_out_of_home_is_refused(snapshot, tmp_path):
    """Characterization: the lexical barrier admits `out/x`; the `resolve()` +
    `relative_to` check behind it still refuses a link that leaves the home."""
    mod, home = snapshot
    outside = tmp_path / "outside"
    outside.mkdir()
    (home / "out").symlink_to(outside)
    with pytest.raises(HTTPException) as exc:
        _snap(mod, "out/x")
    assert exc.value.status_code == 400
    assert not (outside / "x").exists()


@pytest.mark.parametrize("backup_dir", [".", "sub"])
def test_snapshot_backup_dir_is_accepted_under_a_linked_home(
    tmp_path, monkeypatch, backup_dir
):
    """The barrier compares in unresolved space and the header is computed
    against the resolved home, so a home reached through a link (macOS `/var`
    -> `/private/var`) still accepts legitimate names."""
    mod = _load("routers/snapshot.py", "_ent1117_s6_snapshot_link")
    real = (tmp_path / "real-home").resolve()
    real.mkdir()
    link = tmp_path / "home-link"
    link.symlink_to(real)
    monkeypatch.setattr(mod, "_HOME", link)
    resp = _snap(mod, backup_dir)
    expected = "snapshot.tar" if backup_dir == "." else f"{backup_dir}/snapshot.tar"
    assert resp.headers["X-Snapshot-Path"] == expected
    assert (real / expected).is_file()


def test_snapshot_backup_dir_with_exactly_two_leading_slashes_is_refused(snapshot):
    """Behaviour change, kept on purpose. `//<home>/x` used to be accepted,
    because `resolve()` collapses the double slash. The barrier compares the
    `os.path.normpath` form, and POSIX `normpath` keeps exactly two leading
    slashes (that prefix is implementation-defined), so the path no longer
    starts with the home and is refused. Nothing sends `backup_dir` today, and
    refusing is the safe direction."""
    mod, home = snapshot
    with pytest.raises(HTTPException) as exc:
        _snap(mod, "/" + str(home) + "/x")
    assert exc.value.status_code == 400
    assert exc.value.detail == "backup_dir must be inside /home/developer"
    assert not list(home.parent.glob("**/snapshot.tar"))


@pytest.mark.parametrize("prefix", ["", "//"], ids=["one-slash", "three-slashes"])
def test_snapshot_backup_dir_absolute_inside_home_is_accepted(snapshot, prefix):
    """Characterization: an absolute path inside the home is accepted, before
    and after the barrier. Three leading slashes normalise to one, so only the
    exact two-slash form above is refused."""
    mod, home = snapshot
    resp = _snap(mod, prefix + str(home) + "/x")
    assert (home / "x" / "snapshot.tar").is_file()
    assert resp.headers["X-Snapshot-Path"] == "x/snapshot.tar"


def test_snapshot_barrier_precedes_resolve():
    """Structural pin for the CodeQL py/path-injection barrier (#153): a
    normalised, separator-terminated base and its own `startswith` test come
    before the `resolve()` sink. Behaviour is unchanged by it, so CodeQL is the
    real red signal; this keeps the shape from being refactored away."""
    src = (_AGENT_SERVER / "routers" / "snapshot.py").read_text()
    body = src[
        src.index("async def snapshot_endpoint(") : src.index(
            "async def restore_endpoint("
        )
    ]
    assert 'os.path.join(os.path.normpath(str(_HOME)), "")' in body
    assert "if not full.startswith(base):" in body
    assert body.index("if not full.startswith(base):") < body.index(".resolve()")
    assert "relative_to(_HOME.resolve())" in body


def test_snapshot_does_not_write_through_a_link_at_snapshot_tar(snapshot, tmp_path):
    """#153: the leaf is opened without following a link, so a link planted at
    `<backup_dir>/snapshot.tar` is refused rather than written through, and the
    file it points at (here outside the home) keeps its content."""
    mod, home = snapshot
    victim = tmp_path / "outside-victim.txt"
    victim.write_text("ORIGINAL-OUTSIDE\n")
    (home / "backups").mkdir()
    (home / "backups" / "snapshot.tar").symlink_to(victim)
    with pytest.raises(HTTPException) as exc:
        _snap(mod, "backups")
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "resolved_path_mismatch"
    assert victim.read_text() == "ORIGINAL-OUTSIDE\n"
    assert (home / "backups" / "snapshot.tar").is_symlink()


def test_snapshot_does_not_write_through_a_link_at_files_txt(snapshot):
    """#153: the same for `files.txt`, here pointing at another file inside the
    home, which `relative_to` alone cannot tell from a legitimate target."""
    mod, home = snapshot
    victim = home / ".bashrc"
    victim.write_text("ORIGINAL-BASHRC\n")
    (home / "backups").mkdir()
    (home / "backups" / "files.txt").symlink_to(victim)
    with pytest.raises(HTTPException) as exc:
        _snap(mod, "backups")
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "resolved_path_mismatch"
    assert victim.read_text() == "ORIGINAL-BASHRC\n"


def test_snapshot_does_not_write_through_a_linked_directory_inside_home(snapshot):
    """#153: a link anywhere on the way is refused too, even one that stays in
    the home and so passes the `resolve()` + `relative_to` check."""
    mod, home = snapshot
    (home / "elsewhere").mkdir()
    (home / "backups").symlink_to(home / "elsewhere")
    with pytest.raises(HTTPException) as exc:
        _snap(mod, "backups/run-1")
    assert exc.value.status_code == 403
    assert not (home / "elsewhere" / "run-1").exists()


def test_snapshot_backup_dir_written_twice_replaces_the_files(snapshot):
    """Legitimate reuse of a `backup_dir`: the second snapshot replaces the
    first one's regular files."""
    mod, home = snapshot
    _snap(mod, "backups")
    (home / "workspace" / "b.txt").write_text("b")
    _snap(mod, "backups")
    assert (home / "backups" / "files.txt").read_text() == "workspace/a.txt\nworkspace/b.txt\n"
    assert not (home / "backups" / "snapshot.tar").is_symlink()


@pytest.mark.parametrize(
    "backup_dir",
    [5, 1.5, True, False, 0, ["backups"], {"a": 1}, "back\x00ups"],
    ids=["int", "float", "true", "false", "zero", "list", "dict", "nul"],
)
def test_snapshot_backup_dir_that_is_not_a_path_string_is_a_400(snapshot, backup_dir):
    """A `backup_dir` that is not a path string is the caller's error (400),
    not a 500 from `os.path.join` or `resolve()`; nothing is written."""
    mod, home = snapshot
    with pytest.raises(HTTPException) as exc:
        _snap(mod, backup_dir)
    assert exc.value.status_code == 400
    assert exc.value.detail == "backup_dir must be a path string"
    assert not list(home.parent.glob("**/snapshot.tar"))


@pytest.mark.parametrize("backup_dir", [None, ""], ids=["absent", "empty"])
def test_snapshot_without_backup_dir_writes_nothing(snapshot, backup_dir):
    """Characterization: no `backup_dir` (or an empty one) is no disk copy."""
    mod, home = snapshot
    resp = _snap(mod, backup_dir)
    assert resp.headers["X-Snapshot-Path"] == ""
    assert not list(home.parent.glob("**/snapshot.tar"))


# ---------------------------------------------------------------------------
# #30 / #31 — dashboard
# ---------------------------------------------------------------------------


@pytest.fixture
def dashboard(tmp_path, monkeypatch):
    import sys

    base = str(_REPO / "docker" / "base-image")
    if base not in sys.path:
        sys.path.insert(0, base)
    import importlib

    mod = importlib.import_module("agent_server.routers.dashboard")
    path = tmp_path / "dashboard.yaml"
    monkeypatch.setattr(mod, "get_dashboard_path", lambda: path)
    return mod, path


def _dash(mod):
    return asyncio.run(mod.get_dashboard())


_VALID_WIDGET = "{type: text, content: hi}"


@pytest.mark.parametrize(
    ("yaml_text", "expected"),
    [
        pytest.param(
            "- a\n- b\n",
            "dashboard.yaml must be a mapping (key: value pairs) at the top level",
            id="top-level-list",
        ),
        pytest.param(
            "just a sentence\n",
            "dashboard.yaml must be a mapping (key: value pairs) at the top level",
            id="top-level-string",
        ),
        pytest.param(
            f"title: T\nsections:\n  - widgets: [{_VALID_WIDGET}]\n  - not-a-section\n",
            "dashboard.yaml: section 1 must be a mapping (key: value pairs)",
            id="section-not-a-mapping",
        ),
        pytest.param(
            f"title: T\nsections:\n  - widgets: [{_VALID_WIDGET}, just-text]\n",
            "dashboard.yaml: section 0, widget 1 must be a mapping (key: value pairs)",
            id="widget-not-a-mapping",
        ),
    ],
)
def test_dashboard_shape_failures_get_a_named_answer(dashboard, yaml_text, expected):
    """#31: a structure the validator cannot walk used to surface as the raw
    AttributeError/TypeError text; now the owner is told what to fix."""
    mod, path = dashboard
    path.write_text(yaml_text)
    out = _dash(mod)
    assert out["has_dashboard"] is False
    assert out["error"] == expected


def test_dashboard_missing_title_keeps_its_validation_answer(dashboard):
    """Characterization: a missing title is a validation answer, not an exception."""
    mod, path = dashboard
    path.write_text(f"sections:\n  - widgets: [{_VALID_WIDGET}]\n")
    out = _dash(mod)
    assert out["has_dashboard"] is False
    assert out["error"] == "Validation errors: Missing required 'title' field"


def test_dashboard_a_valid_file_still_renders(dashboard):
    """Characterization: the happy path is untouched."""
    mod, path = dashboard
    path.write_text(f"title: T\nsections:\n  - widgets: [{_VALID_WIDGET}]\n")
    out = _dash(mod)
    assert out["has_dashboard"] is True
    assert out["error"] is None


def test_dashboard_unreadable_file_names_the_failure(dashboard, caplog):
    mod, path = dashboard
    path.mkdir()  # a directory where the file should be: IsADirectoryError
    with caplog.at_level(logging.ERROR, logger=mod.logger.name):
        out = _dash(mod)
    assert out["has_dashboard"] is False
    assert (
        out["error"]
        == "Failed to read dashboard.yaml: the file could not be read (IsADirectoryError)"
    )
    assert any("dashboard.yaml" in r.getMessage() for r in caplog.records)


def test_dashboard_unreadable_file_leaves_no_descriptor_open(dashboard):
    """The no-follow open hands back a descriptor before the read can fail (a
    directory opens, then refuses to be read); it is closed on that path too."""
    mod, path = dashboard
    path.mkdir()
    before = len(os.listdir("/dev/fd"))
    for _ in range(5):
        _dash(mod)
    assert len(os.listdir("/dev/fd")) == before


def test_dashboard_permission_denied_names_the_failure(dashboard, monkeypatch, caplog):
    """The OSError arm names the class; the error's own text (here a filename
    carrying a planted credential) reaches the log only through the sanitizer."""
    mod, path = dashboard
    path.write_text("title: T\n")

    def _denied(*a, **kw):
        raise PermissionError(13, "Permission denied", _PLANT)

    monkeypatch.setattr(mod, "_read_dashboard", _denied)
    with caplog.at_level(logging.ERROR, logger=mod.logger.name):
        out = _dash(mod)
    assert (
        out["error"]
        == "Failed to read dashboard.yaml: the file could not be read (PermissionError)"
    )
    _assert_answer_clean(out["error"], "PermissionError")
    _assert_log_scrubbed(caplog.records, "dashboard.yaml")


def test_dashboard_non_utf8_file_names_the_failure(dashboard):
    mod, path = dashboard
    path.write_bytes(b"title: \xff\xfe broken\n")
    out = _dash(mod)
    assert out["has_dashboard"] is False
    assert (
        out["error"]
        == "Failed to read dashboard.yaml: the file is not valid UTF-8 text"
    )


def test_dashboard_unknown_failure_returns_the_class_name_only(
    dashboard, monkeypatch, caplog
):
    """#31 sentinel: an unforeseen exception's text stays in the (scrubbed) log."""
    mod, path = dashboard
    path.write_text(f"title: T\nsections:\n  - widgets: [{_VALID_WIDGET}]\n")

    def _boom(_config):
        raise RuntimeError(_PLANT)

    monkeypatch.setattr(mod, "validate_dashboard", _boom)
    with caplog.at_level(logging.ERROR, logger=mod.logger.name):
        out = _dash(mod)
    assert out["has_dashboard"] is False
    assert out["error"] == "Failed to read dashboard.yaml: RuntimeError"
    _assert_answer_clean(out["error"], "RuntimeError")
    _assert_log_scrubbed(caplog.records, "dashboard.yaml")


def test_dashboard_malformed_yaml_keeps_the_named_refusal(dashboard):
    """Characterization (#30, #282): every YAML parse failure arrives as
    HardenedYamlError, whose named answer is the product."""
    mod, path = dashboard
    path.write_text("title: [unclosed\n")
    out = _dash(mod)
    assert out["has_dashboard"] is False
    assert out["error"].startswith("workspace_yaml_yaml_invalid: ")


def test_dashboard_has_no_unreachable_yaml_error_arm():
    """#30: `load_hardened_yaml` converts every yaml.YAMLError, so the arm
    that caught one could never run. It and its import are gone."""
    src = (_AGENT_SERVER / "routers" / "dashboard.py").read_text()
    assert "except yaml.YAMLError" not in src
    assert "\nimport yaml\n" not in src
    assert "except HardenedYamlError" in src  # test_1965's contract


# What the #282 tests plant: one line of a credentials file the dashboard must
# never quote (a fake; built by concatenation so no source line holds it whole).
_ENV_LINE = "DB_URL: postgres://u:" + "FAKEPASS" + "@h/db"


def test_dashboard_link_to_another_file_is_refused_without_its_text(dashboard, tmp_path):
    """#282: `dashboard.yaml` is opened without following a link, so a link to
    another file (here a stand-in for `~/.env`, which the shared tier cannot
    read through Files) is refused and none of that file's text is returned."""
    mod, path = dashboard
    env = tmp_path / "dot-env"
    env.write_text("A=1\n" + _ENV_LINE + "\n")
    path.symlink_to(env)
    out = _dash(mod)
    assert out == {
        "has_dashboard": False,
        "config": None,
        "last_modified": None,
        "error": "Failed to read dashboard.yaml: it is a link, and links are not followed",
    }
    assert "FAKEPASS" not in str(out)


def test_dashboard_link_to_a_valid_dashboard_is_refused(dashboard, tmp_path):
    """#282: the success path is held to the same rule: a link to another file
    that happens to be a valid dashboard is not served either."""
    mod, path = dashboard
    other = tmp_path / "other.yaml"
    other.write_text(f"title: Other\nsections:\n  - widgets: [{_VALID_WIDGET}]\n")
    path.symlink_to(other)
    out = _dash(mod)
    assert out["has_dashboard"] is False and out["config"] is None
    assert out["error"] == "Failed to read dashboard.yaml: it is a link, and links are not followed"


def test_dashboard_dangling_link_gets_the_same_answer(dashboard, tmp_path):
    """#282: whether a link's target exists is not disclosed either: a dangling
    link is answered exactly like a live one."""
    mod, path = dashboard
    path.symlink_to(tmp_path / "no-such-file")
    out = _dash(mod)
    assert out["error"] == "Failed to read dashboard.yaml: it is a link, and links are not followed"


def test_dashboard_hard_link_to_another_file_is_refused_without_its_text(dashboard, tmp_path):
    """#282: a second name for another file is refused like a link. A hard link
    is invisible to O_NOFOLLOW, and the agent can make one on request in chat;
    Files refuses a hard-linked file below the owner tier for the same reason
    (ent#820). None of the other file's text is returned."""
    mod, path = dashboard
    env = path.parent / "dot-env"  # a hard link needs the same filesystem
    env.write_text("A=1\n" + _ENV_LINE + "\n")
    os.link(env, path)
    out = _dash(mod)
    assert out["has_dashboard"] is False and out["config"] is None
    assert out["error"] == "Failed to read dashboard.yaml: it is a link, and links are not followed"
    assert "FAKEPASS" not in str(out)


def test_dashboard_fifo_does_not_hang_the_endpoint(dashboard):
    """A FIFO at the path is answered at once, never left blocking the open."""
    import threading

    mod, path = dashboard
    os.mkfifo(path)
    result = {}
    worker = threading.Thread(target=lambda: result.setdefault("out", _dash(mod)), daemon=True)
    worker.start()
    worker.join(timeout=5)
    assert not worker.is_alive(), "opening a FIFO at dashboard.yaml blocked the endpoint"
    assert result["out"]["has_dashboard"] is False


def test_dashboard_missing_file_keeps_its_answer(dashboard):
    """Characterization: no file at the path is still "no dashboard"."""
    mod, _path = dashboard
    out = _dash(mod)
    assert out == {
        "has_dashboard": False,
        "config": None,
        "last_modified": None,
        "error": "No dashboard.yaml found at /home/developer/dashboard.yaml",
    }


def test_dashboard_regular_file_reports_its_own_mtime(dashboard):
    """Legitimate input: a regular file renders, with its own modification time."""
    mod, path = dashboard
    path.write_text(f"title: T\nsections:\n  - widgets: [{_VALID_WIDGET}]\n")
    os.utime(path, (1_700_000_000, 1_700_000_000))
    out = _dash(mod)
    assert out["has_dashboard"] is True
    assert out["last_modified"] == mod.iso_z_from_mtime(1_700_000_000)


def test_dashboard_parse_error_still_points_at_the_offending_line(dashboard):
    """Kept on purpose: a parse error names the line and quotes it, because
    with links refused that line is from `dashboard.yaml` itself, a file the
    same caller reads whole through Files and on every successful read."""
    mod, path = dashboard
    path.write_text("A=1\n" + _ENV_LINE + "\n")
    out = _dash(mod)
    assert out["error"].startswith("workspace_yaml_yaml_invalid: ")
    assert "line 2, column 7" in out["error"]
    assert _ENV_LINE in out["error"]


# ---------------------------------------------------------------------------
# trinity#4284 — files router
# ---------------------------------------------------------------------------


@pytest.fixture
def files(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    mod = _load("routers/files.py", "_ent1117_s6_files")
    home = (tmp_path / "home").resolve()
    (home / "workspace").mkdir(parents=True)
    (home / "workspace" / "a.txt").write_text("hello")
    monkeypatch.setattr(mod, "_HOME", home)
    app = FastAPI()
    app.include_router(mod.router)
    return mod, home, TestClient(app, raise_server_exceptions=True)


def _raise_plant(*_a, **_kw):
    raise PermissionError(_PLANT)


def _check_files_500(resp, prefix, caplog, needle):
    assert resp.status_code == 500, resp.text
    detail = resp.json()["detail"]
    assert detail == f"{prefix}: PermissionError"
    _assert_answer_clean(detail, "PermissionError")
    _assert_log_scrubbed(caplog.records, needle)


def test_files_list_error_returns_the_class_name(files, monkeypatch, caplog):
    mod, home, client = files

    class _Path(type(Path())):
        def relative_to(self, *a, **kw):
            raise PermissionError(_PLANT)

    monkeypatch.setattr(mod, "Path", _Path)
    with caplog.at_level(logging.ERROR, logger=mod.logger.name):
        resp = client.get("/api/files", params={"path": str(home / "workspace")})
    _check_files_500(resp, "Failed to list files", caplog, "File listing error")


def test_files_download_error_returns_the_class_name(files, monkeypatch, caplog):
    mod, home, client = files
    real_fdopen = os.fdopen

    class _Reader:
        def __init__(self, fd):
            self._f = real_fdopen(fd, "rb")

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self._f.close()
            return False

        def read(self, *_a):
            raise PermissionError(_PLANT)

    monkeypatch.setattr(mod.os, "fdopen", lambda fd, *_a, **_kw: _Reader(fd))
    with caplog.at_level(logging.ERROR, logger=mod.logger.name):
        resp = client.get("/api/files/download", params={"path": "workspace/a.txt"})
    _check_files_500(resp, "Failed to read file", caplog, "File download error")


def test_files_delete_error_returns_the_class_name(files, monkeypatch, caplog):
    mod, home, client = files
    monkeypatch.setattr(mod.os, "unlink", _raise_plant)
    with caplog.at_level(logging.ERROR, logger=mod.logger.name):
        resp = client.delete("/api/files", params={"path": "workspace/a.txt"})
    _check_files_500(resp, "Failed to delete", caplog, "File delete error")


def test_files_update_error_returns_the_class_name(files, monkeypatch, caplog):
    mod, home, client = files
    monkeypatch.setattr(mod, "_write_text_atomic_at", _raise_plant)
    with caplog.at_level(logging.ERROR, logger=mod.logger.name):
        resp = client.put(
            "/api/files", params={"path": "workspace/a.txt"}, json={"content": "new"}
        )
    _check_files_500(resp, "Failed to update file", caplog, "File update error")


def test_files_mkdir_error_returns_the_class_name(files, monkeypatch, caplog):
    mod, home, client = files
    real_mkdir = os.mkdir

    def _mkdir(path, *a, **kw):
        if path == "new-folder":
            raise PermissionError(_PLANT)
        return real_mkdir(path, *a, **kw)

    monkeypatch.setattr(mod.os, "mkdir", _mkdir)
    with caplog.at_level(logging.ERROR, logger=mod.logger.name):
        resp = client.post("/api/files/mkdir", params={"path": "workspace/new-folder"})
    _check_files_500(resp, "Failed to create folder", caplog, "Folder create error")


def test_files_error_through_the_package_import_is_scrubbed(
    tmp_path, monkeypatch, caplog
):
    """The production load path. The agent server imports this router as
    `agent_server.routers.files`, so `_log_failure` takes its package branch
    (`from ..utils.credential_sanitizer import sanitize_text`). The other files
    tests load the router by file path and take the by-path branch instead, so
    a broken package import would pass them all."""
    import importlib

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    monkeypatch.syspath_prepend(str(_REPO / "docker" / "base-image"))
    mod = importlib.import_module("agent_server.routers.files")
    assert mod.__package__ == "agent_server.routers"  # the branch under test
    home = (tmp_path / "home").resolve()
    (home / "workspace").mkdir(parents=True)
    (home / "workspace" / "a.txt").write_text("hello")
    monkeypatch.setattr(mod, "_HOME", home)
    monkeypatch.setattr(mod.os, "unlink", _raise_plant)
    app = FastAPI()
    app.include_router(mod.router)
    client = TestClient(app, raise_server_exceptions=True)
    with caplog.at_level(logging.ERROR, logger=mod.logger.name):
        resp = client.delete("/api/files", params={"path": "workspace/a.txt"})
    _check_files_500(resp, "Failed to delete", caplog, "File delete error")
    assert any(r.name == "agent_server.routers.files" for r in caplog.records)


# ---------------------------------------------------------------------------
# trinity#4284 — runtimes
# ---------------------------------------------------------------------------


def _runtime_records(caplog, logger_name):
    return [r for r in caplog.records if r.name == logger_name]


@pytest.mark.asyncio
async def test_claude_chat_error_returns_the_class_name(monkeypatch, caplog):
    from agent_server.services import claude_code

    def _boom():
        raise RuntimeError(_PLANT)

    monkeypatch.setattr(claude_code, "_load_guardrails", _boom)
    with caplog.at_level(logging.ERROR, logger=claude_code.logger.name):
        with pytest.raises(HTTPException) as exc:
            await claude_code._execute_claude_code_once(
                "hi",
                False,
                None,
                None,
                "exec-s6",
                None,
            )
    assert exc.value.status_code == 500
    assert exc.value.detail == "Execution error: RuntimeError"
    _assert_answer_clean(exc.value.detail, "RuntimeError")
    _assert_log_scrubbed(
        _runtime_records(caplog, claude_code.logger.name), "Claude Code execution error"
    )


@pytest.mark.asyncio
async def test_gemini_chat_error_returns_the_class_name(monkeypatch, caplog):
    from agent_server.services import gemini_runtime

    def _boom(*_a, **_kw):
        raise RuntimeError(_PLANT)

    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-test")
    monkeypatch.setattr(gemini_runtime, "build_chat_argv", _boom)
    runtime = gemini_runtime.GeminiRuntime()
    with caplog.at_level(logging.ERROR, logger=gemini_runtime.logger.name):
        with pytest.raises(HTTPException) as exc:
            await runtime._execute_chat_once("hi", None, None, "exec-s6", None)
    assert exc.value.status_code == 500
    assert exc.value.detail == "Execution error: RuntimeError"
    _assert_answer_clean(exc.value.detail, "RuntimeError")
    _assert_log_scrubbed(
        _runtime_records(caplog, gemini_runtime.logger.name), "Gemini execution error"
    )


@pytest.mark.asyncio
async def test_gemini_task_error_returns_the_class_name(monkeypatch, caplog):
    from agent_server.services import gemini_runtime

    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-test")
    runtime = gemini_runtime.GeminiRuntime()
    monkeypatch.setattr(runtime, "is_available", lambda: True)

    def _boom(*_a, **_kw):
        raise RuntimeError(_PLANT)

    monkeypatch.setattr(gemini_runtime.subprocess, "Popen", _boom)
    with caplog.at_level(logging.ERROR, logger=gemini_runtime.logger.name):
        with pytest.raises(HTTPException) as exc:
            await runtime.execute_headless(prompt="hello")
    assert exc.value.status_code == 500
    assert exc.value.detail == "Task execution error: RuntimeError"
    _assert_answer_clean(exc.value.detail, "RuntimeError")
    _assert_log_scrubbed(
        _runtime_records(caplog, gemini_runtime.logger.name), "Execution error"
    )


@pytest.mark.asyncio
async def test_gemini_task_timeout_answers_with_its_own_text(monkeypatch, caplog):
    """The headless path raises its own TimeoutError("Task execution timed out
    after N seconds"); the 504 is built from that value, never from the caught
    exception, so a foreign TimeoutError's text stays out of the answer and the
    log carries it scrubbed. "timed out" stays: the backend reads it."""
    from agent_server.services import gemini_runtime

    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-test")
    runtime = gemini_runtime.GeminiRuntime()
    monkeypatch.setattr(runtime, "is_available", lambda: True)

    def _boom(*_a, **_kw):
        raise TimeoutError(_PLANT)

    monkeypatch.setattr(gemini_runtime.subprocess, "Popen", _boom)
    with caplog.at_level(logging.ERROR, logger=gemini_runtime.logger.name):
        with pytest.raises(HTTPException) as exc:
            await runtime.execute_headless(prompt="hello", timeout_seconds=42)
    assert exc.value.status_code == 504
    assert exc.value.detail == "Task execution timed out after 42 seconds"
    assert _SECRET not in exc.value.detail and _MARKER not in exc.value.detail
    _assert_log_scrubbed(_runtime_records(caplog, gemini_runtime.logger.name), "Timeout")


@pytest.fixture
def headless(monkeypatch):
    from agent_server.services import headless_executor as he

    monkeypatch.setattr(he.agent_state, "claude_code_available", True)

    class _Ctx:
        task_session_id = "exec-s6"
        effective_timeout = 10

        def terminate(self):
            return None

    class _Registry:
        def unregister(self, _id):
            return None

    monkeypatch.setattr(he, "_setup_headless_command", lambda **_kw: _Ctx())
    monkeypatch.setattr(he, "get_process_registry", lambda: _Registry())
    return he


@pytest.mark.asyncio
async def test_headless_task_error_returns_the_class_name(
    headless, monkeypatch, caplog
):
    def _boom(_ctx):
        raise RuntimeError(_PLANT)

    monkeypatch.setattr(headless, "_run_headless_subprocess", _boom)
    with caplog.at_level(logging.ERROR, logger=headless.logger.name):
        with pytest.raises(HTTPException) as exc:
            await headless.execute_headless_task(prompt="hello")
    assert exc.value.status_code == 500
    assert exc.value.detail == "Task execution error: RuntimeError"
    _assert_answer_clean(exc.value.detail, "RuntimeError")
    _assert_log_scrubbed(
        _runtime_records(caplog, headless.logger.name), "Execution error"
    )


@pytest.mark.asyncio
async def test_headless_permission_bypass_text_is_kept(headless, monkeypatch):
    """Characterization: a message the agent server writes itself, matched by
    the backend's `is_container_fault` ("permission bypass failed"), stays
    verbatim."""
    text = "Permission bypass failed: permissionMode=default. Try restarting the agent container."

    def _boom(_ctx):
        raise RuntimeError(text)

    monkeypatch.setattr(headless, "_run_headless_subprocess", _boom)
    with pytest.raises(HTTPException) as exc:
        await headless.execute_headless_task(prompt="hello")
    assert exc.value.status_code == 503
    assert exc.value.detail == text


@pytest.fixture
def codex(monkeypatch):
    from agent_server.services import codex_runtime

    rt = codex_runtime.CodexRuntime()
    monkeypatch.setattr(rt, "is_available", lambda: True)
    return codex_runtime, rt


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("entry", "prefix", "needle"),
    [
        ("chat", "Execution error", "execution error"),
        ("task", "Task execution error", "task execution error"),
    ],
)
async def test_codex_error_returns_the_class_name(
    codex, monkeypatch, caplog, entry, prefix, needle
):
    mod, rt = codex

    async def _boom(**_kw):
        raise RuntimeError(_PLANT)

    monkeypatch.setattr(rt, "_execute_codex", _boom)
    with caplog.at_level(logging.ERROR, logger=mod.logger.name):
        with pytest.raises(HTTPException) as exc:
            if entry == "chat":
                await rt.execute("hi")
            else:
                await rt.execute_headless(prompt="hi")
    assert exc.value.status_code == 500
    assert exc.value.detail == f"{prefix}: RuntimeError"
    _assert_answer_clean(exc.value.detail, "RuntimeError")
    _assert_log_scrubbed(_runtime_records(caplog, mod.logger.name), needle)


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["chat", "task"])
async def test_codex_foreign_timeout_returns_the_class_name(
    codex, monkeypatch, caplog, entry
):
    """Every timeout the Codex path owns is already a 504 HTTPException; a
    TimeoutError reaching this arm is foreign, so its text is not ours to
    return. "timed out" stays in the answer for the backend's text checks."""
    mod, rt = codex

    async def _boom(**_kw):
        raise TimeoutError(_PLANT)

    monkeypatch.setattr(rt, "_execute_codex", _boom)
    with caplog.at_level(logging.ERROR, logger=mod.logger.name):
        with pytest.raises(HTTPException) as exc:
            if entry == "chat":
                await rt.execute("hi")
            else:
                await rt.execute_headless(prompt="hi")
    assert exc.value.status_code == 504
    assert exc.value.detail == "Codex execution timed out (TimeoutError)"
    _assert_answer_clean(exc.value.detail, "TimeoutError")
    _assert_log_scrubbed(_runtime_records(caplog, mod.logger.name), "timeout")


# ---------------------------------------------------------------------------
# The backend reads agent detail text: the new fixed strings trip none of it.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "detail",
    [
        "Execution error: RuntimeError",
        "Task execution error: RuntimeError",
        "Task execution error: OSError",
        "Codex execution timed out (TimeoutError)",
        "Failed to read file: PermissionError",
    ],
)
def test_new_detail_strings_trip_no_backend_classifier(detail):
    """Characterization of the sweep: the backend's substring classifiers
    (`failure_classifier`, used by SUB-003, `classify_switch_failure` and the
    scheduler) read nothing from the fixed wording the agent now writes."""
    fc = importlib.util.spec_from_file_location(
        "_ent1117_s6_failure_classifier",
        str(_REPO / "src/backend/services/failure_classifier.py"),
    )
    mod = importlib.util.module_from_spec(fc)
    fc.loader.exec_module(mod)
    assert not mod.is_auth_failure(detail)
    assert not mod.is_turn_limit(detail)
    assert not mod.is_container_fault(detail)
    assert not mod.is_prompt_too_long(detail)
    assert not mod.is_model_rejection(detail)
