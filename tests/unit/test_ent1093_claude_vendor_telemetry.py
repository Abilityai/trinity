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
  ``process.env`` at init (no gate was seen firing before settings load);
- the customer OTLP exporter (``CLAUDE_CODE_ENABLE_TELEMETRY``, which Trinity
  sets for ``/api/ops/costs``) is gated on that name alone; the privacy level
  turns off only Anthropic's own metrics reader beside it;
- Claude Code applies ``env`` from the agent-writable ``~/.claude/settings.json``
  and ``.claude/settings.local.json`` over the process env; only managed
  (policy) ``env`` beats them. The managed path is fixed at ``/etc/claude-code``
  (its override resolver is a stub). The CLI merges ``managed-settings.json``
  and the sorted drop-ins into one file tier, then unions ``env`` across admin
  tiers (an org's remote managed settings, MDM, file);
  ``CLAUDE_CODE_DISABLE_ADMIN_ENV_UNION`` keeps only the highest tier's ``env``,
  which drops the whole file tier when an org's remote tier is present. Both it
  and ``CLAUDE_CODE_MANAGED_SETTINGS_PATH`` are process-env-only: the CLI drops
  them from every settings source's ``env`` before applying it (and strips the
  union flag from policy ``env`` too), so only the process env, which ``.env``
  feeds, could set them;

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
  intake on a happy-path turn but left the MCP-registry and bootstrap fetches;
- with the drop-in alone (no process-env value at all) nothing reaches event
  logging or the third-party intake, and an OTLP collector configured as the
  platform configures it still receives ``claude_code.cost.usage`` and
  ``claude_code.token.usage`` — the same metrics as with the opt-out removed;
- the agent's own ``~/.claude/settings.json`` setting both detaching names
  (``CLAUDE_CODE_DISABLE_ADMIN_ENV_UNION=1`` and a managed-settings path) never
  reaches the CLI's shell, while a marker in the same file does; a ``""`` pin
  for the union flag in the drop-in changes nothing, so the drop-in carries none;
- ``claude --version`` (the one ``claude`` run before the drop-in exists in the
  build) makes no request, even with the opt-out entirely absent.
  No error report was provoked in any run, so "error reports are off" rests on
  the binary read above.

No image ``ENV`` carries the names: a recreate replays the old container's
``Config.Env``, so an image value would be frozen into every agent and a later
admin opt-in could not reach it.

Pinned here: the drop-in's exact content and root-owned install before every
``claude`` run but the install's ``--version``; no image ``ENV`` for any of the
names; no other managed source declaring them; every managed ``env`` name being
one ``.env`` cannot set; ``.env`` unable to blank the opt-out on Claude (and
free to set it on another runtime) or to detach managed ``env`` on any runtime
(through the real ``build_execution_env``); the ``/health`` field that says
whether the opt-out is in force; and a gate that fails when the CLI pin moves,
naming what to re-check.

Module under test: docker/base-image/hooks/managed-settings.d/70-vendor-telemetry.json,
docker/base-image/Dockerfile, docker/base-image/agent_server/services/execution_env.py,
docker/base-image/agent_server/routers/info.py.
"""
from __future__ import annotations

import importlib.util
import json
import asyncio
import os
import re
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
    narrow names follow the same rule on 2.1.281. No detaching-name pin: the
    CLI drops those names from every settings `env`, policy included."""
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


def test_no_image_env_sets_any_of_the_names():
    """A recreate replays `Config.Env`, which includes the image's ENV, so an
    image value would live on in every agent recreated from it."""
    names = (*OPT_OUT, *DETACHING)
    envs = [ins for ins in _instructions() if ins.startswith(("ENV ", "ARG "))
            and any(re.search(rf"(?<![A-Z_]){name}(?![A-Z_])", ins) for name in names)]
    assert not envs, envs


# A `claude` command word (not `claude-code`, `.claude/`, or text inside a
# quoted echo), with its first argument.
_CLAUDE_CALL = re.compile(r"(?:^|[\s;&|(!])claude(?![\w./-])(?:\s+([^\s;&|)]+))?")
_QUOTED = re.compile(r"\"[^\"]*\"|'[^']*'")


def test_the_drop_in_is_in_place_before_every_claude_run_but_the_install_check():
    """The build runs `claude` more than once (the plugin pre-install at least).
    Each run must see the opt-out, except the install's own `claude --version`,
    which makes no request. A smoke turn added above the COPY fails here."""
    instructions = _instructions()
    copy = _index(lambda ins: ins.startswith("COPY ") and _DROP_IN in ins)
    runs = [(i, m.group(1)) for i, ins in enumerate(instructions) if ins.startswith("RUN ")
            for m in _CLAUDE_CALL.finditer(_QUOTED.sub('""', ins[len("RUN "):]))]
    assert any(i > copy and arg == "plugin" for i, arg in runs), runs  # the guard is not vacuous
    early = [(i, arg) for i, arg in runs if i < copy and arg != "--version"]
    assert not early, early


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
    guarded = (set(mod.PROTECTED_KEYS) | set(mod.ROUTING_KEYS) | set(mod.RUNTIME_ENDPOINT_KEYS["claude"])
               | set(mod.RUNTIME_OPT_OUT_KEYS["claude"]))
    unguarded = {name: names - guarded for name, names in _managed_env_sources().items()
                 if names - guarded}
    assert not unguarded, unguarded


# --- `.env` cannot undo it ------------------------------------------------------

def _env_module(monkeypatch, runtime: str):
    for key in (*OPT_OUT, *DETACHING, "AGENT_RUNTIME"):
        monkeypatch.delenv(key, raising=False)
    mod = _load(f"_execution_env_ent1093_{runtime}", _MODULE)
    mod.INITIAL_ENV = {"AGENT_RUNTIME": runtime}
    return mod


@pytest.mark.parametrize("value", ["", "0", "false"])
def test_a_claude_agents_env_cannot_blank_the_opt_out(monkeypatch, tmp_path, caplog, value):
    """Not set from `.env`, so the drop-in's `1` is what the CLI sees."""
    mod = _env_module(monkeypatch, "claude-code")
    env_file = tmp_path / ".env"
    env_file.write_text("".join(f"{name}={value}\n" for name in OPT_OUT) + 'CANARY_VAR="kept"\n')

    with caplog.at_level("WARNING"):
        env = mod.build_execution_env(env_file=env_file)

    assert not set(OPT_OUT) & set(env)
    assert env["CANARY_VAR"] == "kept"
    assert {r.args[0] for r in caplog.records if "vendor-traffic opt-out" in r.getMessage()} == set(OPT_OUT)


@pytest.mark.parametrize("runtime", ["gemini", "codex"])
def test_another_runtimes_env_may_set_the_generic_names(monkeypatch, tmp_path, caplog, runtime):
    """`DISABLE_TELEMETRY` is read by other CLIs and tools too: only a Claude
    agent's `.env` is refused it."""
    mod = _env_module(monkeypatch, runtime)
    env_file = tmp_path / ".env"
    env_file.write_text("".join(f"{name}=1\n" for name in OPT_OUT))

    with caplog.at_level("WARNING"):
        env = mod.build_execution_env(env_file=env_file)

    assert {name: env.get(name) for name in OPT_OUT} == OPT_OUT
    assert not [r for r in caplog.records if any(name in r.getMessage() for name in OPT_OUT)]


@pytest.mark.parametrize("runtime", ["claude-code", "gemini", "codex"])
def test_env_cannot_detach_managed_env_on_any_runtime(monkeypatch, tmp_path, runtime):
    mod = _env_module(monkeypatch, runtime)
    env_file = tmp_path / ".env"
    env_file.write_text("CLAUDE_CODE_MANAGED_SETTINGS_PATH=/home/developer/policy\n"
                        "CLAUDE_CODE_DISABLE_ADMIN_ENV_UNION=1\n")

    env = mod.build_execution_env(env_file=env_file)

    assert not set(DETACHING) & set(env)


# --- /health says whether it is in force -----------------------------------------

@pytest.fixture
def health(tmp_path, monkeypatch):
    from agent_server.routers import info
    from agent_server.state import agent_state

    d = tmp_path / "managed-settings.d"
    d.mkdir()
    f = d / "70-vendor-telemetry.json"
    f.write_text(_DROP_IN_SRC.read_text())
    monkeypatch.setattr(info, "VENDOR_TELEMETRY_OPT_OUT", str(f))
    monkeypatch.setattr(agent_state, "agent_runtime", "claude-code")
    real = os.stat

    class _St:   # root ownership cannot be created without root (the #3105 shim)
        def __init__(self, st):
            self.st_uid = 0
            self.st_mode = st.st_mode

    monkeypatch.setattr(os, "stat", lambda p, *a, **k: _St(real(p, *a, **k)))
    f.chmod(0o444)
    d.chmod(0o555)
    yield info, agent_state, d, f
    d.chmod(0o755)
    if f.exists():
        f.chmod(0o644)


def test_health_reports_the_opt_out_in_force(health):
    if os.getuid() == 0:
        pytest.skip("root can write anything")
    info, *_ = health
    assert asyncio.run(info.health_check())["vendor_telemetry_opt_out"] == "ok"


def test_the_health_check_names_the_shipped_file():
    from agent_server.routers import info

    assert info.VENDOR_TELEMETRY_OPT_OUT == _DROP_IN
    assert set(info.VENDOR_TELEMETRY_OPT_OUT_NAMES) == set(OPT_OUT)


def test_health_says_missing_on_an_image_without_it(health):
    info, _state, d, f = health
    d.chmod(0o755)
    f.unlink()
    assert info._vendor_telemetry_opt_out() == "missing"


@pytest.mark.parametrize("env", [{**OPT_OUT, "DISABLE_TELEMETRY": ""}, {}, "not a dict"])
def test_health_says_altered_when_a_name_is_no_longer_on(health, env):
    if os.getuid() == 0:
        pytest.skip("root can write anything")
    info, _state, d, f = health
    f.chmod(0o644)
    f.write_text(json.dumps({"env": env}))
    f.chmod(0o444)
    assert info._vendor_telemetry_opt_out() == "altered"


def test_health_says_writable_when_the_agent_could_change_it(health):
    if os.getuid() == 0:
        pytest.skip("root can write anything")
    info, _state, _d, f = health
    f.chmod(0o644)
    assert info._vendor_telemetry_opt_out() == "writable"


@pytest.mark.parametrize("runtime", ["gemini", "codex"])
def test_health_says_unsupported_on_another_runtime(health, monkeypatch, runtime):
    info, state, *_ = health
    monkeypatch.setattr(state, "agent_runtime", runtime)
    assert info._vendor_telemetry_opt_out() == "unsupported_runtime"


# --- the facts above are version-specific --------------------------------------

def test_the_facts_were_read_from_the_cli_the_image_pins():
    pinned = re.search(r"^ARG CLAUDE_CODE_VERSION=(\S+)$", _DOCKERFILE, re.M).group(1)
    assert pinned == _CLI_VERIFIED, (
        f"The image now pins Claude Code {pinned}; the ent#1093 vendor-telemetry facts were read "
        f"from {_CLI_VERIFIED}. Before moving _CLI_VERIFIED: re-run the proxied capture of one Read "
        "turn (no `tengu_file_operation` / `contentHash`, no first-party event or error-tracker "
        "traffic, the model reply still arrives) and confirm an OTLP collector configured as "
        "crud.py configures it still receives `claude_code.cost.usage` (/api/ops/costs); confirm "
        "the CLI still drops CLAUDE_CODE_DISABLE_ADMIN_ENV_UNION and CLAUDE_CODE_MANAGED_SETTINGS_PATH "
        "from settings `env` (an agent's ~/.claude/settings.json value must not reach its Bash tool) "
        "and that `claude --version` still makes no request; re-read every CHANGELOG entry between the two "
        "pins that mentions telemetry, nonessential traffic or managed settings (2.1.282/2.1.283 "
        "change auto mode 'with telemetry off', 2.1.290 widens the broad variable); confirm the "
        "managed-settings path resolver is still a stub and that admin `env` still merges across "
        "drop-ins; check the permission mode an interactive terminal session starts in.")
