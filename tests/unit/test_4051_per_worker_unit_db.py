"""#4051 — every unit-test process, each xdist worker included, has its own
default SQLite file (and retained-results dir and chat-session marker).

The xdist controller loads `tests/unit/conftest.py` first. A plain
`os.environ.setdefault(..., f"...-{os.getpid()}.db")` there stamped the
controller's pid, every worker inherited the variable, and every worker's own
`setdefault` was a no-op: under `-n auto` all workers opened one file. CI runs
this tier under `-n auto`, which is where this test bites; a serial run passes
either way.
"""
import os
import re
import tempfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

# The defaults tests/unit/conftest.py derives; any other value was set from outside.
_DEFAULTS = {
    "TRINITY_DB_PATH": r"trinity-unit-tests-(\d+)\.db",
    "TRINITY_RETAINED_RESULTS_DIR": r"trinity-unit-retained-(\d+)",
    "TRINITY_CHAT_SESSION_FILE": r"trinity-unit-chat-session-(\d+)\.json",
}


@pytest.mark.parametrize("name", sorted(_DEFAULTS))
def test_a_defaulted_path_belongs_to_this_process(name):
    value = Path(os.environ[name])
    match = re.fullmatch(_DEFAULTS[name], value.name)
    if value.parent != Path(tempfile.gettempdir()) or not match:
        pytest.skip(f"{name}={value} was set from outside the test run; it is left alone")
    assert int(match.group(1)) == os.getpid(), (
        f"{name}={value} was stamped by another process "
        f"(worker {os.environ.get('PYTEST_XDIST_WORKER', 'none')}, pid {os.getpid()})"
    )
