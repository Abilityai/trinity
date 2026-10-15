"""#3510 — the agent image never calls a backend path that does not exist.

`startup.sh` called `POST /api/internal/decrypt-and-inject` from the day the
feature shipped, and no handler ever existed: every start with a committed
`.credentials.enc` and no `.env` failed, and the message blamed a missing
encryption key. Nothing compared the call against the route table.

Ruled 2026-10-15: the startup auto-import is removed (decrypting the file is
the owner's Import from Git, and the platform key never reaches the container).
This file pins both halves:

* every literal `http://backend:8000/api/...` call in the agent image resolves
  to a route the real app registers (read from the live route table, in a
  subprocess, through the #2996 census helper);
* the scanner is not vacuous — it flags the old call against the same table;
* startup tells the operator what actually happens to a `.credentials.enc`.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _route_census as rc  # noqa: E402

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]
_IMAGE = _ROOT / "docker" / "base-image"
_CALL = re.compile(r"http://backend:8000(/api/[A-Za-z0-9_\-./{}$]*)")


def _calls(text: str):
    """`/api/...` paths in `text`, shell/f-string variables kept as segments."""
    return sorted({m.group(1).rstrip("/.") for m in _CALL.finditer(text)})


def _image_calls():
    out = {}
    for path in sorted(_IMAGE.rglob("*")):
        if path.suffix in (".sh", ".py") and path.is_file():
            for call in _calls(path.read_text(errors="replace")):
                out.setdefault(call, []).append(path.relative_to(_ROOT).as_posix())
    return out


def _matcher(route_paths):
    """A route template matches a call when every segment is equal, or the
    template's is `{param}`, or the call's is a variable (`$X`, `${X}`, `{x}`)."""
    templates = [p.split(" ", 1)[1].strip("/").split("/") for p in route_paths]

    def resolves(call: str) -> bool:
        segs = call.strip("/").split("/")
        for t in templates:
            if len(t) == len(segs) and all(
                a == b or a.startswith("{") or b.startswith("$") or b.startswith("{")
                for a, b in zip(t, segs)
            ):
                return True
        return False
    return resolves


@pytest.fixture(scope="module")
def resolves():
    live = rc.runtime_paths(rc.runtime_routes())
    return _matcher([p for paths in live.values() for p in paths])


def test_every_backend_call_in_the_agent_image_exists(resolves):
    missing = {call: files for call, files in _image_calls().items() if not resolves(call)}
    assert not missing, f"the agent image calls backend paths with no route: {missing}"


def test_the_scanner_flags_the_old_dead_call(resolves):
    old = 'curl -s -X POST "http://backend:8000/api/internal/decrypt-and-inject" \\'
    assert _calls(old) == ["/api/internal/decrypt-and-inject"]
    assert resolves("/api/internal/decrypt-and-inject") is False
    assert resolves("/api/token") is True                     # a real route resolves


def test_startup_says_what_happens_to_an_encrypted_credentials_file():
    startup = (_IMAGE / "startup.sh").read_text()
    assert "decrypt-and-inject" not in startup
    assert "key may not be configured" not in startup          # the old wrong cause
    assert "Import from Git" in startup
    block = startup[startup.index('if [ -f ".credentials.enc" ] && [ ! -f ".env" ]'):]
    assert "curl" not in block[: block.index("\nfi")]


def test_the_dead_request_model_is_gone():
    models = (_ROOT / "src" / "backend" / "models.py").read_text()
    assert "InternalDecryptInjectRequest" not in models
