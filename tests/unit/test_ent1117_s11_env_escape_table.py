"""trinity-enterprise#1117 — one value -> `.env` line table for every writer and reader.

The Credentials panel in the browser rewrites an agent's whole `.env` on each
save, so its reader and writer must agree with the agent's own encoding
(`execution_env.format_env_line` / `unquote_env_value`, #2023): backslash
escaped BEFORE quote, both reversed in one scan. The panel used to escape only
the quote, so a credential containing `\\\\` lost a backslash on the next save
of any credential in the file.

The contract is the shared fixture ``tests/fixtures/ent1117-env-escape-cases.json``.
Each row's ``line`` was generated from the agent's real ``format_env_line``,
never hand-typed. THIS test proves every row against the agent's encoder and
decoder, the agent's whole-file parser, and the backend's ``agent_env_patch``
writer (the backend's second writer of the same file). Its twin,
``src/frontend/tests/unit/ent1117EnvEscape.spec.js``, proves the browser's
``utils/envFile.js`` against the same rows. A change to the encoding on either
side turns one of the two red.

Characterization on the Python side: the agent and backend encoders were
already correct, so this file passes on the unfixed tree. Its job is to keep
the fixture honest — a row that stops matching the agent fails here, not only
in the browser spec.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_FIXTURE = _REPO / "tests" / "fixtures" / "ent1117-env-escape-cases.json"
_AGENT = _REPO / "docker" / "base-image" / "agent_server" / "services" / "execution_env.py"
_BACKEND = _REPO / "src" / "backend" / "services" / "agent_env_patch.py"

pytestmark = pytest.mark.unit

ROWS = json.loads(_FIXTURE.read_text(encoding="utf-8"))
B = "\\"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def agent():
    return _load("_ent1117_execution_env", _AGENT)


@pytest.fixture(scope="module")
def backend():
    return _load("_ent1117_agent_env_patch", _BACKEND)


def _rhs(line: str) -> str:
    return line.partition("=")[2]


def test_the_table_carries_the_cases_the_defect_is_about():
    by_id = {r["id"]: r["value"] for r in ROWS}
    assert by_id["double-backslash"] == "a" + B + B + "b"
    assert by_id["embedded-quote"] == 'a"b'
    assert by_id["trailing-backslash"] == "x" + B
    assert by_id["windows-path"] == "C:" + B + "path" + B + "new"
    assert by_id["backslash-then-quote"] == "a" + B + '"b'
    assert len(ROWS) >= 15
    assert len(by_id) == len(ROWS), "row ids must be unique"


@pytest.mark.parametrize("row", ROWS, ids=[r["id"] for r in ROWS])
def test_agent_encodes_each_value_to_the_table_line(agent, row):
    assert agent.format_env_line("KEY", row["value"]) == row["line"]


@pytest.mark.parametrize("row", ROWS, ids=[r["id"] for r in ROWS])
def test_agent_decodes_each_table_line_back_exactly(agent, row):
    assert agent.unquote_env_value(_rhs(row["line"])) == row["value"]


@pytest.mark.parametrize("row", ROWS, ids=[r["id"] for r in ROWS])
def test_backend_writer_emits_the_same_line(backend, row):
    assert backend.format_env_line("KEY", row["value"]) == row["line"]


def test_agent_reads_a_whole_file_of_table_lines(agent, tmp_path):
    # The file the panel writes: its header, a blank line, then one line per key.
    lines = [r["line"].replace("KEY=", f"K_{i}=", 1) for i, r in enumerate(ROWS)]
    env = tmp_path / ".env"
    env.write_text(
        "# Credential file - managed by Trinity\n\n" + "\n".join(lines) + "\n",
        encoding="utf-8",
    )
    assert agent.parse_env_file(env) == {f"K_{i}": r["value"] for i, r in enumerate(ROWS)}
