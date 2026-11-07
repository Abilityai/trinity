"""trinity-enterprise#1093 — a Claude Code agent sends Anthropic no vendor
telemetry, error reports or update/feedback checks by default, and nothing the
agent can write turns that back on.

Claude Code (pinned 2.1.281) emits a ``tengu_file_operation`` telemetry event
for every file it reads, carrying ``contentHash`` (an unsalted SHA-256 of the
full contents, for files up to 102,400 bytes) beside device and session ids.
Trinity set no opt-out anywhere, against the FAQ's "nothing leaves your server
without an admin-level opt-in".

Read from the pinned 2.1.281 binary before writing these tests (static, not
executed):

- the privacy level is ``essential-traffic`` when
  ``CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`` is non-empty, ``no-telemetry``
  for ``DISABLE_TELEMETRY`` (or a truthy ``DO_NOT_TRACK``), else ``default``;
- the first-party event logger (the ``contentHash`` carrier) starts only when
  the level is not ``default``, so either name stops it;
- the internal error sink returns early only on ``DISABLE_ERROR_REPORTING`` or
  the ``essential-traffic`` level, so ``DISABLE_TELEMETRY`` alone leaves it on;
- every gate reads ``process.env`` when it fires, and settings ``env`` reaches
  ``process.env`` at init. The image ``ENV`` is defence in depth (no gate was
  seen firing before settings load); it also puts the values in
  ``docker inspect``;
- Claude Code applies ``env`` from the agent-writable ``~/.claude/settings.json``
  and ``.claude/settings.local.json`` over the process env; only managed
  (policy) ``env`` beats them. The managed path is fixed at ``/etc/claude-code``
  (its override resolver is a stub). The CLI merges ``managed-settings.json``
  and the sorted drop-ins into one file tier, then unions ``env`` across admin
  tiers (an org's remote managed settings, MDM, file);
  ``CLAUDE_CODE_DISABLE_ADMIN_ENV_UNION`` keeps only the highest tier's ``env``,
  which drops the whole file tier when an org's remote tier is present.

Captured on the image this change builds (2.1.281, one Read turn on a 65-byte
file through the agent server, behind a logging proxy on a network with no
other way out; the log held host, path, field NAMES and a match boolean only):

- without the opt-out, a ``tengu_file_operation`` event reaches
  ``api.anthropic.com/api/event_logging/v2/batch``, and its base64
  ``additional_metadata`` carries ``contentHash`` equal to the file's SHA-256;
  logs also go to a third-party intake (``http-intake.logs.*.datadoghq.com``);
- with it: no event-logging POST, no third-party intake, no feature-flag,
  MCP-registry or bootstrap fetches; the model reply still arrives, and an MCP
  tool call works on the chat and headless paths;
- the agent writing ``""`` for all three names into ``~/.claude/settings.json``,
  ``.claude/settings.local.json`` and ``.env`` (both settings files shown to be
  read) changes nothing; with the drop-in removed and only the image ``ENV``
  left, the same settings files DO cancel it and the hash is sent again. The
  drop-in is the layer that holds;
- ``DISABLE_TELEMETRY=1`` alone stopped event logging and the third-party
  intake on a happy-path turn but left the MCP-registry and bootstrap fetches.
  No error report was provoked in any run, so "error reports are off" rests on
  the binary read above.

Pinned here: the drop-in's exact content and root-owned install before the
build's own ``claude`` runs; the image ``ENV`` carrying the same names; no other
managed source declaring them; every managed ``env`` name being one ``.env``
cannot set; ``.env`` unable to blank the opt-out or detach managed ``env``
(through the real ``build_execution_env``); and a gate that fails when the CLI
pin moves, naming what to re-check.

Module under test: docker/base-image/hooks/managed-settings.d/70-vendor-telemetry.json,
docker/base-image/Dockerfile, docker/base-image/agent_server/services/execution_env.py.
"""
from __future__ import annotations

import importlib.util
import json
import re
import shlex
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]
_BASE = _ROOT / "docker" / "base-image"
_HOOKS = _BASE / "hooks"
_DROP_IN_DIR = _HOOKS / "managed-settings.d"
_DROP_IN_SRC = _DROP_IN_DIR / "70-vendor-telemetry.json"
_DROP_IN = "/etc/claude-code/managed-settings.d/70-vendor-telemetry.json"
_MAIN_SRC = _HOOKS / "managed-settings.json"
_WRITER = _HOOKS / "write-routing-env.py"
_MODULE = _BASE / "agent_server" / "services" / "execution_env.py"
_DOCKERFILE = (_BASE / "Dockerfile").read_text()

OPT_OUT = {
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    "DISABLE_ERROR_REPORTING": "1",
    "DISABLE_TELEMETRY": "1",
}
# Not opt-out values, but names that would detach every managed drop-in's `env`.
DETACHING = ("CLAUDE_CODE_MANAGED_SETTINGS_PATH", "CLAUDE_CODE_DISABLE_ADMIN_ENV_UNION")

_CLI_VERIFIED = "2.1.281"


def _instructions() -> list[str]:
    """The Dockerfile as instructions: comments dropped, `\\` continuations joined."""
    lines = [line for line in _DOCKERFILE.splitlines() if not line.lstrip().startswith("#")]
    joined = re.sub(r"\\\n", " ", "\n".join(lines))
    return [line.strip() for line in joined.splitlines() if line.strip()]


def _index(predicate) -> int:
    hits = [i for i, ins in enumerate(_instructions()) if predicate(ins)]
    assert len(hits) == 1, hits
    return hits[0]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(f"{name}_{len(sys.modules)}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _managed_env_sources() -> dict[str, set[str]]:
    """Every managed-settings source the image ships and the `env` names in it."""
    sources = {_MAIN_SRC.name: set(json.loads(_MAIN_SRC.read_text()).get("env", {}))}
    for path in sorted(_DROP_IN_DIR.glob("*.json")):
        sources[path.name] = set(json.loads(path.read_text()).get("env", {}))
    # 60-routing-env.json is generated at build and boot, from this list.
    sources["60-routing-env.json"] = set(_load("_write_routing_env_ent1093", _WRITER).ROUTING_NAMES)
    return sources


# --- the drop-in --------------------------------------------------------------

def test_the_drop_in_is_exactly_the_opt_out():
    """Env-only, so a schema problem elsewhere can never void it, and nothing
    else rides in it. `"1"`: the CLI treats any non-empty value as on, and the
    narrow names follow the same rule on 2.1.281."""
    assert json.loads(_DROP_IN_SRC.read_text()) == {"env": OPT_OUT}


def test_the_image_installs_it_root_owned_and_read_only():
    instructions = _instructions()
    copy = f"COPY ./hooks/managed-settings.d/70-vendor-telemetry.json {_DROP_IN}"
    assert copy in instructions, "the drop-in must be a top-level COPY (never conditional)"
    chmod = next(ins for ins in instructions
                 if ins.startswith("RUN chown -R root:root /etc/claude-code/managed-settings.d"))
    assert "chmod 0755 /etc/claude-code/managed-settings.d" in chmod
    assert _DROP_IN in chmod.split("chmod 0444", 1)[1]
    assert not any("--chown" in ins and "70-vendor-telemetry" in ins for ins in instructions)


def test_the_image_env_carries_the_same_names():
    """The process-env layer, defence in depth beside the drop-in."""
    envs = [ins for ins in _instructions() if ins.startswith("ENV ")
            and any(name in ins for name in OPT_OUT)]
    assert len(envs) == 1, envs
    pairs = dict(token.split("=", 1) for token in shlex.split(envs[0][len("ENV "):]))
    assert pairs == OPT_OUT


def test_both_layers_are_in_place_before_the_plugin_pre_install():
    """The build's plugin pre-install is a `claude` run; it must run with them on."""
    pre_install = _index(lambda ins: "claude plugin marketplace add" in ins and ins.startswith("RUN "))
    copy = _index(lambda ins: ins.startswith("COPY ") and _DROP_IN in ins)
    env = _index(lambda ins: ins.startswith("ENV ") and "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC" in ins)
    assert copy < pre_install and env < pre_install


def test_no_other_managed_source_declares_the_opt_out():
    """Drop-ins merge in name order and the later file wins, so a name declared
    anywhere else (a future `80-*.json`, or added to the routing pins) would
    cancel the opt-out while every other test here stays green."""
    sources = _managed_env_sources()
    assert sources.pop(_DROP_IN_SRC.name) == set(OPT_OUT)
    clashes = {name: names & set(OPT_OUT) for name, names in sources.items() if names & set(OPT_OUT)}
    assert not clashes, clashes


def test_every_managed_env_name_is_one_env_cannot_set():
    """The class, not this one name: a managed pin is only a pin if the agent's
    `.env` cannot blank the same name in the spawn env."""
    mod = _load("_execution_env_ent1093_class", _MODULE)
    guarded = set(mod.PROTECTED_KEYS) | set(mod.ROUTING_KEYS) | set(mod.RUNTIME_ENDPOINT_KEYS["claude"])
    unguarded = {name: names - guarded for name, names in _managed_env_sources().items()
                 if names - guarded}
    assert not unguarded, unguarded


# --- `.env` cannot undo it ------------------------------------------------------

@pytest.fixture
def env_module(monkeypatch):
    """`execution_env` with a container baseline that carries the opt-out, on a
    runtime that is not Claude: nothing here may pass by being Claude-gated."""
    for key in (*OPT_OUT, *DETACHING, "AGENT_RUNTIME"):
        monkeypatch.delenv(key, raising=False)
    mod = _load("_execution_env_ent1093", _MODULE)
    mod.INITIAL_ENV = {"AGENT_RUNTIME": "gemini", **OPT_OUT}
    return mod


@pytest.mark.parametrize("value", ["", "0", "false"])
def test_env_cannot_blank_the_opt_out(env_module, tmp_path, value):
    env_file = tmp_path / ".env"
    env_file.write_text("".join(f"{name}={value}\n" for name in OPT_OUT) + 'CANARY_VAR="kept"\n')

    env = env_module.build_execution_env(env_file=env_file)

    assert {name: env.get(name) for name in OPT_OUT} == OPT_OUT
    assert env["CANARY_VAR"] == "kept"


def test_env_cannot_detach_managed_env(env_module, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("CLAUDE_CODE_MANAGED_SETTINGS_PATH=/home/developer/policy\n"
                        "CLAUDE_CODE_DISABLE_ADMIN_ENV_UNION=1\n")

    env = env_module.build_execution_env(env_file=env_file)

    assert not set(DETACHING) & set(env)


# --- the facts above are version-specific --------------------------------------

def test_the_facts_were_read_from_the_cli_the_image_pins():
    pinned = re.search(r"^ARG CLAUDE_CODE_VERSION=(\S+)$", _DOCKERFILE, re.M).group(1)
    assert pinned == _CLI_VERIFIED, (
        f"The image now pins Claude Code {pinned}; the ent#1093 vendor-telemetry facts were read "
        f"from {_CLI_VERIFIED}. Before moving _CLI_VERIFIED: re-run the proxied capture of one Read "
        "turn (no `tengu_file_operation` / `contentHash`, no first-party event or error-tracker "
        "traffic, the model reply still arrives); re-read every CHANGELOG entry between the two "
        "pins that mentions telemetry, nonessential traffic or managed settings (2.1.282/2.1.283 "
        "change auto mode 'with telemetry off', 2.1.290 widens the broad variable); confirm the "
        "managed-settings path resolver is still a stub and that admin `env` still merges across "
        "drop-ins; check the permission mode an interactive terminal session starts in.")
