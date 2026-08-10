"""The `.env` set/missing predicate and the bounded probe (trinity-enterprise#127 §1, §2.1).

The highest-value test here is `TestExporterParity`: "set" has to mean what the
AGENT sees, so the predicate is *defined* as agreement with the agent server's
own post-injection exporter. Everything else is caps, degradation and the
"never emit a value" contract.
"""

import ast
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from services import credential_requirements_service as crs

AGENT_SERVER_CREDENTIALS = (
    Path(__file__).resolve().parents[2]
    / "docker" / "base-image" / "agent_server" / "routers" / "credentials.py"
)
# #1999 moved the exporter: the set-only loop that used to live inside
# `routers/credentials.py` is now `parse_env_file`, which both the process
# mirror and every spawned execution's environment are built from. The parsing
# is byte-faithful to the old loop precisely so THIS predicate did not have to
# move with it — only the anchor below did.
AGENT_SERVER_EXECUTION_ENV = (
    Path(__file__).resolve().parents[2]
    / "docker" / "base-image" / "agent_server" / "services" / "execution_env.py"
)


def _exporter_replica(content: str):
    """A byte-faithful replica of the agent server's post-injection export loop.

    Kept beside `TestExporterParity.test_source_anchor_still_present`, which
    fails if the real loop is ever edited — so this replica cannot quietly
    become fiction.
    """
    env = {}
    for line in content.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key:
                env[key] = value
    return env


PARITY_FIXTURES = [
    "KEY=value",
    "KEY=",
    'KEY=""',
    "KEY=''",
    'KEY="   "',
    'KEY="v"',
    "KEY='v'",
    'KEY=""""',
    'KEY="\'v\'"',
    "KEY='\"v\"'",
    'KEY="',
    "KEY = v",
    "  KEY=v  ",
    "KEY=a=b=c",
    "KEY=value # not a comment",
    '# KEY=commented',
    "",
    "no_equals_here",
    "=novalue",
    "KEY=v\nKEY=w",
    "export KEY=v",
    "export\tKEY=v",
    "A=1\r\nB=2\r\n",
    "﻿KEY=v",
    "KEY=  spaced  ",
    "lower_key=v",
    "K-E-Y=v",
]


class TestExporterParity:
    """`_env_pairs` must agree with the agent's runtime, key for key, value for value."""

    @pytest.mark.parametrize("content", PARITY_FIXTURES)
    def test_pairs_match_the_exporter(self, content):
        assert crs._env_pairs(content.splitlines()) == _exporter_replica(content)

    def test_source_anchor_still_present(self):
        """The replica above is only honest while the real loop is unchanged.

        Anchored on the OWNING FUNCTION via `ast`, not on a `str.find` offset:
        a find-based slice returns -1 on a rename, the slice silently becomes
        `''`, and the guard then asserts against nothing.

        #1999 relocated that loop out of `routers/credentials.py` and into
        `services/execution_env.parse_env_file` — same parsing, new home and a
        delete phase. The anchor follows it.
        """
        src = AGENT_SERVER_EXECUTION_ENV.read_text()
        tree = ast.parse(src)
        owners = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                segment = ast.get_source_segment(src, node) or ""
                if """parsed[key] = value.strip().strip('"').strip("'")""" in segment:
                    owners.append((node.name, segment))

        assert owners, (
            "no function in agent_server/services/execution_env.py still "
            "contains the byte-faithful `.env` value parse — the parity anchor "
            "moved. Re-derive `_env_pairs` against the new exporter before "
            "touching this test."
        )

    def test_the_set_only_mirror_did_not_come_back(self):
        """#1999: the loop this predicate mirrors used to have no delete phase,
        so a key removed from `.env` kept reaching every spawned execution. Its
        return would reintroduce that silently."""
        src = AGENT_SERVER_CREDENTIALS.read_text()
        assert "os.environ[key] = value" not in src, (
            "the set-only .env mirror is back in agent_server/routers/"
            "credentials.py (#1999)"
        )


class TestPredicate:
    @pytest.mark.parametrize(
        "line,expected_set",
        [
            ("KEY=value", True),
            ("KEY=v", True),
            ('KEY="v"', True),
            ("KEY='v'", True),
            ("KEY=  v  ", True),
            ("KEY=value # not a comment", True),
            ('KEY="v # not a comment"', True),
            # The bug this feature exists to fix: a freshly-created agent's
            # generated .env is `KEY=` for every declared variable.
            ("KEY=", False),
            ('KEY=""', False),
            ("KEY=''", False),
            # Whitespace-only: strict parity would call this set (the agent
            # really holds "   "), but that is a green row in front of an agent
            # that will 401. See `_env_keys_with_values`.
            ('KEY="   "', False),
            ("KEY=   ", False),
            ("KEY=\t", False),
            # `.strip('"')` peels ALL layers, so four quotes collapse to empty.
            ('KEY=""""', False),
            # The frontend rewrites a cleared field as KEY="".
            ('KEY=""', False),
        ],
    )
    def test_emptiness(self, line, expected_set):
        assert ("KEY" in crs._env_keys_with_values([line])) is expected_set

    def test_lone_quote_is_not_a_value(self):
        assert crs._env_keys_with_values(['KEY="']) == []

    def test_duplicate_last_wins(self):
        assert crs._env_keys_with_values(["KEY=v", "KEY="]) == []
        assert crs._env_keys_with_values(["KEY=", "KEY=v"]) == ["KEY"]

    def test_comments_and_blanks_ignored(self):
        assert crs._env_keys_with_values(["# KEY=v", "", "   ", "novalue"]) == []

    def test_equals_in_value(self):
        assert crs._env_keys_with_values(["KEY=a=b=c"]) == ["KEY"]

    def test_crlf(self):
        assert crs._env_keys_with_values("A=1\r\nB=2\r\n".splitlines()) == ["A", "B"]

    def test_export_prefix_reads_as_missing(self):
        """Deliberate: nothing in the agent strips `export`, so `KEY` is genuinely
        unavailable to the runtime. See `_env_pairs`' docstring."""
        assert crs._env_keys_with_values(["export KEY=v"]) == ["export KEY"]
        assert "KEY" not in crs._env_keys_with_values(["export KEY=v"])

    def test_bom_binds_a_different_name(self):
        # Same reasoning: the exporter binds `﻿KEY`, so the agent cannot see KEY.
        assert "KEY" not in crs._env_keys_with_values(["﻿KEY=v"])

    def test_sorted_and_deduped(self):
        assert crs._env_keys_with_values(["B=1", "A=1", "B=2"]) == ["A", "B"]

    def test_lowercase_names_are_not_filtered(self):
        """No charset filter — the runtime it audits applies none, so filtering
        here would report a working lowercase variable as missing."""
        assert crs._env_keys_with_values(["my_var=v"]) == ["my_var"]


class TestScriptAssembly:
    def test_script_assembled_at_import(self):
        assert crs._COLLECTOR_SCRIPT is not None

    def test_predicate_is_spliced_from_real_source(self):
        """Same code shipped as tested — not a hand-copied duplicate."""
        assert "def _env_pairs(" in crs._COLLECTOR_SCRIPT
        assert "def _env_keys_with_values(" in crs._COLLECTOR_SCRIPT
        assert 'value = value.strip().strip(\'"\').strip("\'")' in crs._COLLECTOR_SCRIPT

    def test_no_policy_crosses_the_boundary(self):
        """No charset filter, no YAML parse, no name detection in-container.

        (`template.yaml` appears as a FILENAME, which is why this asserts on the
        parse call rather than on the substring "yaml".)
        """
        assert "import yaml" not in crs._COLLECTOR_SCRIPT
        assert "safe_load" not in crs._COLLECTOR_SCRIPT
        assert "A-Za-z" not in crs._COLLECTOR_SCRIPT
        assert "re.compile" not in crs._COLLECTOR_SCRIPT

    def test_fifo_guard_present(self):
        assert "stat.S_ISREG" in crs._COLLECTOR_SCRIPT

    def test_env_text_never_emitted(self):
        body = crs._COLLECTOR_SCRIPT
        assert '"env_keys_nonempty"' in body
        assert '"env_text"' not in body
        assert '"env_file_text"' not in body


class TestScriptExecution:
    """Run the ASSEMBLED script against fixture directories with the local
    interpreter. No Docker — but it is the real script, not a re-implementation."""

    def _run(self, root: Path):
        script = crs._build_collector_script(root=str(root))
        proc = subprocess.run(
            [sys.executable, "-"],
            input=script,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert proc.returncode == 0, proc.stderr
        return json.loads(proc.stdout)

    def test_configured_agent(self, tmp_path):
        (tmp_path / ".env").write_text('SET_KEY="abc"\nEMPTY_KEY=\n')
        (tmp_path / "template.yaml").write_text("name: demo\n")
        out = self._run(tmp_path)
        assert out["env_file_present"] is True
        assert out["env_keys_nonempty"] == ["SET_KEY"]
        assert out["template_present"] is True
        assert out["template_text"] == "name: demo\n"

    def test_env_absent_is_a_definite_answer(self, tmp_path):
        """The dominant case: a `github:` agent never gets a generated `.env`."""
        (tmp_path / "template.yaml").write_text("name: demo\n")
        out = self._run(tmp_path)
        assert out["env_file_present"] is False
        assert out["env_keys_nonempty"] == []

    def test_empty_workspace(self, tmp_path):
        out = self._run(tmp_path)
        assert out["template_present"] is False
        assert out["template_text"] is None
        assert out["env_file_present"] is False

    def test_fifo_env_does_not_hang(self, tmp_path):
        """`mkfifo /home/developer/.env` is the agent-triggerable wedge: without
        the S_ISREG guard `open()` blocks forever and burns one of the backend's
        four shared Docker pool threads."""
        os.mkfifo(tmp_path / ".env")
        out = self._run(tmp_path)  # the 30s subprocess timeout is the assertion
        assert out["env_file_present"] is True
        assert out["env_keys_nonempty"] == []

    def test_oversize_env_degrades_not_raises(self, tmp_path):
        (tmp_path / ".env").write_text("PAD=" + "x" * (crs._ENV_CAP + 5000) + "\nOK=1\n")
        out = self._run(tmp_path)
        # Truncated mid-value: the padding key survives, the trailing one is cut.
        assert "OK" not in out["env_keys_nonempty"]
        assert out["schema"] == 1

    def test_oversize_template_is_truncated_not_dropped(self, tmp_path):
        (tmp_path / "template.yaml").write_text("name: x\n" + "# " + "y" * (crs._TEMPLATE_CAP))
        out = self._run(tmp_path)
        assert out["template_truncated"] is True
        assert len(out["template_text"]) <= crs._TEMPLATE_CAP

    def test_binary_template_reports_no_text(self, tmp_path):
        (tmp_path / "template.yaml").write_bytes(b"\x00\x01binary")
        out = self._run(tmp_path)
        assert out["template_present"] is True
        assert out["template_text"] is None

    def test_non_utf8_env_still_reports_survivors(self, tmp_path):
        (tmp_path / ".env").write_bytes(b"GOOD=1\nBAD=\xff\xfe\nALSO=2\n")
        out = self._run(tmp_path)
        assert "GOOD" in out["env_keys_nonempty"]
        assert "ALSO" in out["env_keys_nonempty"]

    def test_no_value_ever_appears_in_the_output(self, tmp_path):
        secret = "sk-ent127-supersecret-value"
        (tmp_path / ".env").write_text("OPENAI_API_KEY={0}\n".format(secret))
        (tmp_path / ".mcp.json.template").write_text('{"mcpServers":{}}')
        out = self._run(tmp_path)
        assert secret not in json.dumps(out)
        assert out["env_keys_nonempty"] == ["OPENAI_API_KEY"]

    def test_directory_in_place_of_env(self, tmp_path):
        (tmp_path / ".env").mkdir()
        out = self._run(tmp_path)
        assert out["env_file_present"] is True
        assert out["env_keys_nonempty"] == []

    def test_output_cap_drops_text_not_the_answer(self, tmp_path):
        # Three near-cap files sum past the 2 MiB output budget once JSON-escaped.
        big = " " * (crs._CONFIG_CAP // 4)
        (tmp_path / "template.yaml").write_text("a" * crs._TEMPLATE_CAP)
        (tmp_path / ".mcp.json.template").write_text(big)
        (tmp_path / ".env.example").write_text(big)
        (tmp_path / ".env").write_text("KEY=v\n")
        out = self._run(tmp_path)
        if out["output_capped"]:
            assert out["template_text"] is None
        assert out["env_keys_nonempty"] == ["KEY"]


class TestShapeGuard:
    @pytest.mark.parametrize(
        "facts",
        [
            None,
            "not a dict",
            {},
            {"schema": 2, "env_keys_nonempty": [], "env_file_present": True},
            {"schema": 1, "error": "collect_failed"},
            {"schema": 1, "env_keys_nonempty": "AB", "env_file_present": True},
            {"schema": 1, "env_keys_nonempty": [], "env_file_present": "yes"},
        ],
    )
    def test_rejects_bad_shapes(self, facts):
        assert crs._shape_ok(facts) is False

    def test_accepts_good_shape(self):
        assert crs._shape_ok(
            {"schema": 1, "env_keys_nonempty": [], "env_file_present": False}
        ) is True

    def test_normalize_drops_non_strings_and_caps(self):
        got = crs._normalize_facts(
            {
                "schema": 1,
                "env_keys_nonempty": ["A", 3, None, "B"],
                "env_file_present": 1,
                "template_text": 42,
            }
        )
        assert got["env_keys_nonempty"] == ["A", "B"]
        assert got["env_file_present"] is True
        assert got["template_text"] is None
