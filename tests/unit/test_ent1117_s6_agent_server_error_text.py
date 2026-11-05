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
