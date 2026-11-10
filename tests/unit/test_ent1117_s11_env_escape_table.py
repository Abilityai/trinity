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
decoder, and the agent's whole-file parser. (On ``dev`` it also proves the
backend's ``agent_env_patch`` writer; that module does not exist on ``main``.) Its twin,
``src/frontend/tests/unit/ent1117EnvEscape.spec.js``, proves the browser's
``utils/envFile.js`` against the same rows. A change to the encoding on either
side turns one of the two red.

The agent has a SECOND reader of the same file: ``agent_server/mcp_template.py``
renders ``.mcp.json.template`` from ``.env`` at every container start
(``startup.sh``). It reversed only ``\\"``, so once the panel escaped ``\\`` a
panel-saved ``C:\\path`` was rendered into ``.mcp.json`` as ``C:\\\\path``. It is
held here to the agent's ``unquote_env_value``: per row, over the whole file,
and over every short string.

Lines the new writer never produced (the previous panel writer's output, hand
edits, pastes) are in ``tests/fixtures/ent1117-env-legacy-reads.json``: every
agent-side reader must keep reading them as ``execution_env`` does (the
``agent`` column), and the browser spec holds the panel to the ``panel``
column. The two columns differ on one row only, a lone ``'``.

Characterization for the encoders and ``execution_env``: they were already
correct, so those tests pass on the unfixed tree and keep the fixture honest.
The ``mcp_template`` tests are the behavioural ones: red before its decoder was
fixed.
"""

from __future__ import annotations

import importlib.util
import itertools
import json
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_FIXTURE = _REPO / "tests" / "fixtures" / "ent1117-env-escape-cases.json"
_AGENT = _REPO / "docker" / "base-image" / "agent_server" / "services" / "execution_env.py"
_AGENT_SERVER = _REPO / "docker" / "base-image" / "agent_server"
_MCP_TEMPLATE = _AGENT_SERVER / "mcp_template.py"
_LEGACY_FIXTURE = _REPO / "tests" / "fixtures" / "ent1117-env-legacy-reads.json"

pytestmark = pytest.mark.unit

ROWS = json.loads(_FIXTURE.read_text(encoding="utf-8"))
LEGACY = json.loads(_LEGACY_FIXTURE.read_text(encoding="utf-8"))
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
def mcp_template():
    # Its flat-import fallback needs the vendored validator on sys.path, as in
    # an image run (the test_2007_mcp_template_render.py idiom).
    sys.path.insert(0, str(_AGENT_SERVER))
    try:
        return _load("_ent1117_mcp_template", _MCP_TEMPLATE)
    finally:
        sys.path.remove(str(_AGENT_SERVER))


def _mcp_read(mod, tmp_path: Path, rhs: str) -> str:
    """What mcp_template renders for one `.env` line `K=<rhs>`, through its real
    file reader (the function `render()` calls)."""
    env = tmp_path / ".env"
    env.write_text(f"K={rhs}\n", encoding="utf-8")
    return mod.parse_env_file(env)["K"]


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


def test_agent_reads_a_whole_file_of_table_lines(agent, tmp_path):
    # The file the panel writes: its header, a blank line, then one line per key.
    lines = [r["line"].replace("KEY=", f"K_{i}=", 1) for i, r in enumerate(ROWS)]
    env = tmp_path / ".env"
    env.write_text(
        "# Credential file - managed by Trinity\n\n" + "\n".join(lines) + "\n",
        encoding="utf-8",
    )
    assert agent.parse_env_file(env) == {f"K_{i}": r["value"] for i, r in enumerate(ROWS)}


@pytest.mark.parametrize("row", ROWS, ids=[r["id"] for r in ROWS])
def test_mcp_template_reads_each_table_line_back_exactly(mcp_template, tmp_path, row):
    assert _mcp_read(mcp_template, tmp_path, _rhs(row["line"])) == row["value"]


def test_mcp_template_reads_a_whole_file_of_table_lines(mcp_template, tmp_path):
    lines = [r["line"].replace("KEY=", f"K_{i}=", 1) for i, r in enumerate(ROWS)]
    env = tmp_path / ".env"
    env.write_text(
        "# Credential file - managed by Trinity\n\n" + "\n".join(lines) + "\n",
        encoding="utf-8",
    )
    assert mcp_template.parse_env_file(env) == {f"K_{i}": r["value"] for i, r in enumerate(ROWS)}


def test_mcp_template_renders_a_panel_saved_backslash_value_exactly(mcp_template, tmp_path):
    # The end-to-end shape: a value saved through the panel, used in a template.
    value = "C:" + B + "path" + B + "new"
    tpl, cfg, env = tmp_path / ".mcp.json.template", tmp_path / ".mcp.json", tmp_path / ".env"
    tpl.write_text(json.dumps({"mcpServers": {"files": {
        "command": "npx", "args": ["-y", "some-mcp-server"], "env": {"ROOT": "${ROOT_DIR}"},
    }}}), encoding="utf-8")
    env.write_text(next(r["line"] for r in ROWS if r["value"] == value).replace("KEY=", "ROOT_DIR=", 1)
                   + "\n", encoding="utf-8")
    result = mcp_template.render(template_file=tpl, config_file=cfg, env_file=env)
    assert result["added"] == ["files"], result
    assert json.loads(cfg.read_text())["mcpServers"]["files"]["env"]["ROOT"] == value


def test_mcp_template_agrees_with_the_agent_reader_on_every_short_value(agent, mcp_template, tmp_path):
    # Every right-hand side up to 6 characters over the four characters the
    # encoding is about (5461 values), each written twice: `K_i=v`, and
    # `J_i = v` with blanks around `=` as a hand edit has them, which both
    # parsers strip before decoding. Read by both parsers from one file.
    rhs_all = ["".join(t) for n in range(7) for t in itertools.product(B + "\"'a", repeat=n)]
    env = tmp_path / ".env"
    env.write_text(
        "".join(f"K_{i}={v}\nJ_{i} = {v}\n" for i, v in enumerate(rhs_all)), encoding="utf-8"
    )
    expected = {}
    for i, v in enumerate(rhs_all):
        expected[f"K_{i}"] = expected[f"J_{i}"] = agent.unquote_env_value(v)
    assert agent.parse_env_file(env) == expected
    assert mcp_template.parse_env_file(env) == expected


def test_the_legacy_table_carries_the_shapes_an_existing_file_holds():
    by_id = {r["id"]: r for r in LEGACY}
    assert len(by_id) == len(LEGACY), "row ids must be unique"
    assert by_id["old-writer-windows-path"]["rhs"] == '"C:' + B + 'path"'
    assert by_id["old-writer-backslash-then-quote"]["rhs"] == '"a' + B + B + '"b"'
    assert by_id["old-writer-double-backslash"]["rhs"] == '"a' + B + B + 'b"'
    assert by_id["single-quoted-escapes-kept"]["rhs"] == "'a" + B + B + "b " + B + "\"'"
    assert by_id["lone-single-quote"]["rhs"] == "'"
    # The one place the previous panel reader and the agent disagree.
    assert [r["id"] for r in LEGACY if r["panel"] != r["agent"]] == ["lone-single-quote"]


@pytest.mark.parametrize("row", LEGACY, ids=[r["id"] for r in LEGACY])
def test_every_agent_side_reader_keeps_reading_old_lines_as_before(
    agent, mcp_template, tmp_path, row
):
    assert agent.unquote_env_value(row["rhs"]) == row["agent"]
    assert _mcp_read(mcp_template, tmp_path, row["rhs"]) == row["agent"]
