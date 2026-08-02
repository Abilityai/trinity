"""Per-agent credential requirements + live set/missing status (ent#127).

Joins the ent#128 declaration (`normalize_credential_requirements`) against
what the agent's `.env` ACTUALLY holds, so the Agent Detail checklist can say
"OPENAI_API_KEY — missing — get one at platform.openai.com" instead of leaving
an operator to already know.

HTTP-free (Invariant #1): the router is a thin projection over `build_report`.

WHY A `docker exec` PROBE AND NOT AN AGENT-SERVER ENDPOINT
----------------------------------------------------------
An agent-server route would need a base-image rebuild, so every already-deployed
agent would read `unknown` until it was recreated. AC #3 is "works post-hoc for
seeded and forked agents" — a checklist that is inert for exactly the fleet it
exists to serve fails its own audience. The probe works on the whole existing
fleet the moment the backend updates, and nothing is vendored into the image, so
no Invariant #5 parity obligation attaches.

WHY IT IS *NOT* THE #668 COMPATIBILITY COLLECTOR
------------------------------------------------
Deliberately separate, do not "unify" them without reading this paragraph: the
compat snapshot treats `.env` as **existence-only** by security design and its
payload feeds AI checks, so adding value-derived data to that shared, LLM-bound
payload is a widening. Being separate also decouples this panel from compat's
cadence. Accepted cost: two exec paths to keep aligned.

BOUNDING THE EXEC — ALL THREE, NONE IS SUFFICIENT ALONE
-------------------------------------------------------
`docker_service.execute_command_in_container` ACCEPTS a `timeout` and then
never references it again; `docker_utils.container_exec_run` has no timeout
parameter, docker-py's `exec_run` has none, and docker-py's socket reader polls
with no timeout before every `recv`. So the call is unbounded, and it runs on a
`ThreadPoolExecutor(max_workers=4)` shared by EVERY Docker operation in the
backend (Invariant #11). Four wedged calls stop the backend's whole Docker
layer, and it is agent-triggerable: the agent owns `/home/developer/.env`, and
`mkfifo /home/developer/.env` makes an `open()` block forever.

  1. Container-side `timeout(1)` — the load-bearing one. Self-termination closes
     the socket, so the pool thread is actually reclaimed. (An `asyncio` timeout
     cancels the await, never the thread.)
  2. `asyncio.wait_for` — bounds the REQUEST, so a wedged probe degrades to
     `agent_unreachable` instead of hanging the caller.
  3. `stat.S_ISREG` before `open()` — closes the FIFO vector at the source.

`compatibility/collector.py:_EXEC_TIMEOUT` is decorative for the same reason and
is tracked as its own fix; it is out of scope here.

WHAT CROSSES THE IMAGE BOUNDARY
-------------------------------
Exactly one policy: the empty-value predicate, and it is *defined* as agreement
with the agent's own exporter (see `_env_pairs`). It is a real module-level
function, unit-tested directly AND source-injected into the script, so the
tested code and the shipped code are the same code. Everything else in the
script is file reading with caps — no charset filter (that would be a hidden
fifth member of `services/credential_charset.py`'s explicit MEMBERS list, and
narrower than the runtime it audits), no YAML parsing (alias expansion is a
443 B -> 52 MB amplifier; the backend parses), no name detection.

SECRETS (AC #6)
---------------
The probe returns key NAMES with a non-empty value — never a value, a length or
a hash. The backend then PROJECTS that list onto the declared set before it
reaches a response, so an operator-added undeclared variable
(`CLIENT_ACME_PROD_TOKEN`, which leaks a customer relationship) is never
disclosed. `result["output"]` is never logged or returned: on failure
`execute_command_in_container` puts an exception string there.
"""

import asyncio
import inspect
import json
import logging
from typing import Any, Dict, List, Optional

import yaml

from services.credential_charset import (
    CREDENTIAL_DETECTOR_REF_RE,
    is_credential_var_name,
)
from services.docker_service import execute_command_in_container, get_agent_container
from services.setup_url_display import describe_setup_url_or_none
from services.template_service import (
    get_github_template,
    get_local_template,
    normalize_credential_requirements,
    operator_supplied_credential_names,
)

logger = logging.getLogger(__name__)

# The agent home is the fixed, only location for these files: the agent server
# serves `/home/developer/template.yaml` from `get_template_path()`, and
# credential injection writes `/home/developer/.env`. Fixed, so the script needs
# no path discovery exec (the legacy `workspace/` git-dir probe is another
# unbounded call) and no caller data is ever interpolated.
_ROOT = "/home/developer"

# Input caps, applied INSIDE the container.
_TEMPLATE_CAP = 1024 * 1024      # template.yaml
_CONFIG_CAP = 256 * 1024         # .mcp.json.template, .env.example
_ENV_CAP = 256 * 1024            # .env — read for key names only
_MAX_ENV_KEYS = 2000

# Output cap. An input cap does NOT bound the output: JSON escaping inflates,
# and three capped files sum. Over budget, the text fields are dropped and the
# probe says so rather than emitting an unbounded blob into the exec socket.
_OUTPUT_CAP = 2 * 1024 * 1024

# (1) container-side self-termination and (2) the request bound. The request
# bound is deliberately the larger: it must not fire while the container-side
# kill is still doing its job, or a bounded probe would report as a hang.
_SCRIPT_TIMEOUT = 10
_REQUEST_TIMEOUT = 20


def _env_pairs(lines) -> Dict[str, str]:
    """`.env` lines -> `{key: value}` EXACTLY as the agent's own exporter sets them.

    This is the reference implementation, and it is a *copy of behaviour by
    contract*, not an independent parser. The agent server, after every
    credential injection, does this
    (`docker/base-image/agent_server/routers/credentials.py`)::

        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key:
                    os.environ[key] = value

    Every quirk below is that code's quirk, kept on purpose:

    * `.strip` on a quote character peels ALL layers of it, not one pair, so a
      value of four double-quotes collapses to empty, and `KEY="'v'"` yields `v`.
    * The two quote characters are stripped independently and in order (double
      first, then single), so `KEY='"v"'` keeps its inner double quotes.
    * A duplicate key is last-wins, because `os.environ[key] = value` is.
    * A leading `export ` is NOT stripped, so `export KEY=v` binds the name
      `export KEY` and the agent genuinely cannot see `KEY`. Reporting that as
      "set" would be a false green — the operator's variable really is
      unavailable to the runtime. (The plan called for dotenv-style
      export-stripping; agreement with the runtime wins, because nothing in the
      agent — not the exporter, not `startup.sh`'s anchored `^GITHUB_PAT=` grep
      — strips it.)

    A pinned parity test compares this against a replica of that loop. If the
    exporter ever changes, that test fails and this docstring is the reason why.
    """
    pairs = {}
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if not key:
            continue
        pairs[key] = value
    return pairs


def _env_keys_with_values(lines) -> List[str]:
    """Sorted names of `.env` keys whose value is non-empty. THE status predicate.

    Non-empty is the whole point. A freshly-created agent's generated `.env`
    carries a bare `KEY=` for every variable its template declares
    (`generate_credential_files`), and the frontend rewrites a cleared field as
    `KEY=""` — so "the key appears in the file" reports **set** for an agent
    nobody has configured, which is the exact bug this feature exists to fix.

    This is the one place a POLICY sits on top of the parity-pinned parse:
    emptiness is tested after `.strip()`, so a value of only whitespace reads as
    **missing**. Strict parity would call it set — the agent really does hold a
    three-space string — but that is a green row in front of an agent that will
    401, and being stricter than the runtime here only ever prompts an operator
    to fix a value that is already broken. `_env_pairs` stays byte-faithful; the
    judgement lives here, where it is visible.
    """
    pairs = _env_pairs(lines)
    return sorted(key for key, value in pairs.items() if value.strip())


# The in-container script, minus the predicate, which is spliced in from real
# source at import (see `_build_collector_script`). Plain 3.8-compatible syntax:
# an agent running an older base image must still be probeable.
_SCRIPT_BODY = r'''
import json, os, stat, sys


def read_text(rel, cap):
    """Read one file with a byte cap. Never blocks, never raises."""
    info = {"present": False, "text": None, "truncated": False}
    path = os.path.join(ROOT, rel)
    try:
        st = os.stat(path)
    except OSError:
        return info
    info["present"] = True
    # A FIFO (or a device, or a directory) makes `open()` block forever, which
    # would wedge a thread in the backend's shared 4-slot Docker pool. The agent
    # owns this directory, so this is reachable, not theoretical.
    if not stat.S_ISREG(st.st_mode):
        info["not_regular"] = True
        return info
    try:
        with open(path, "rb") as handle:
            raw = handle.read(cap + 1)
    except OSError:
        return info
    if len(raw) > cap:
        raw = raw[:cap]
        info["truncated"] = True
    if b"\x00" in raw:
        info["binary"] = True
        return info
    # errors="replace": one stray byte degrades one line, not the whole file.
    info["text"] = raw.decode("utf-8", "replace")
    return info


try:
    template = read_text("template.yaml", TEMPLATE_CAP)
    mcp_template = read_text(".mcp.json.template", CONFIG_CAP)
    env_example = read_text(".env.example", CONFIG_CAP)
    env_file = read_text(".env", ENV_CAP)

    env_keys = []
    if env_file.get("text"):
        env_keys = _env_keys_with_values(env_file["text"].splitlines())[:MAX_ENV_KEYS]

    out = {
        "schema": 1,
        "template_present": template["present"],
        "template_text": template["text"],
        "template_truncated": template["truncated"],
        "mcp_template_present": mcp_template["present"],
        "mcp_template_text": mcp_template["text"],
        "env_example_present": env_example["present"],
        "env_example_text": env_example["text"],
        "env_file_present": env_file["present"],
        # NAMES ONLY. `env_file["text"]` is deliberately never referenced again.
        "env_keys_nonempty": env_keys,
        "output_capped": False,
    }

    payload = json.dumps(out)
    if len(payload) > OUTPUT_CAP:
        out["template_text"] = None
        out["mcp_template_text"] = None
        out["env_example_text"] = None
        out["output_capped"] = True
        payload = json.dumps(out)
    sys.stdout.write(payload)
except Exception:
    # Zero content, deliberately: a traceback raised while handling a `.env`
    # line can embed that line, and stderr is merged into stdout by exec_run.
    sys.stdout.write('{"schema": 1, "error": "collect_failed"}')
'''


def _build_collector_script(root: str = _ROOT) -> str:
    """Header + the real predicate source + the body.

    `inspect.getsource` rather than a second copy: the predicate is the one
    piece of policy crossing the image boundary, and a hand-copied duplicate is
    a contract that drifts silently the first time someone fixes only one side.

    `root` is parameterised for TESTS ONLY, so the assembled script can be run
    against a fixture directory by the local interpreter — which is the only way
    to exercise the FIFO guard, the caps and the absent-`.env` path without
    Docker. Production always uses the module constant; nothing caller-supplied
    ever reaches it.
    """
    header = (
        "ROOT = {root!r}\n"
        "TEMPLATE_CAP = {template_cap}\n"
        "CONFIG_CAP = {config_cap}\n"
        "ENV_CAP = {env_cap}\n"
        "MAX_ENV_KEYS = {max_env_keys}\n"
        "OUTPUT_CAP = {output_cap}\n"
        # The spliced predicate carries its real annotations, and on an older
        # base image they are evaluated at `def` time — so `typing` has to be
        # imported BEFORE the splice, not with the body's other imports.
        "from typing import Dict, List\n"
    ).format(
        root=root,
        template_cap=_TEMPLATE_CAP,
        config_cap=_CONFIG_CAP,
        env_cap=_ENV_CAP,
        max_env_keys=_MAX_ENV_KEYS,
        output_cap=_OUTPUT_CAP,
    )
    predicate_src = inspect.getsource(_env_pairs) + "\n\n" + inspect.getsource(_env_keys_with_values)
    return header + "\n" + predicate_src + "\n" + _SCRIPT_BODY


try:
    _COLLECTOR_SCRIPT: Optional[str] = _build_collector_script()
except Exception:  # noqa: BLE001 — a checklist panel must not stop the backend booting
    _COLLECTOR_SCRIPT = None
    logger.error(
        "[credential-requirements] collector script could not be assembled; "
        "per-variable status will report as unavailable",
        exc_info=True,
    )


def _shape_ok(facts: Any) -> bool:
    """Structural guard on the probe's JSON before anything reads it."""
    if not isinstance(facts, dict) or facts.get("schema") != 1:
        return False
    if facts.get("error"):
        return False
    if not isinstance(facts.get("env_keys_nonempty"), list):
        return False
    if not isinstance(facts.get("env_file_present"), bool):
        return False
    return True


def _normalize_facts(facts: Dict[str, Any]) -> Dict[str, Any]:
    """Coerce the probe's dict to the types the service is allowed to trust."""
    keys = [k for k in facts.get("env_keys_nonempty") or [] if isinstance(k, str)]
    return {
        "template_present": bool(facts.get("template_present")),
        "template_text": facts.get("template_text") if isinstance(facts.get("template_text"), str) else None,
        "template_truncated": bool(facts.get("template_truncated")),
        "mcp_template_text": facts.get("mcp_template_text") if isinstance(facts.get("mcp_template_text"), str) else None,
        "env_example_text": facts.get("env_example_text") if isinstance(facts.get("env_example_text"), str) else None,
        "env_file_present": bool(facts.get("env_file_present")),
        "env_keys_nonempty": keys[:_MAX_ENV_KEYS],
        "output_capped": bool(facts.get("output_capped")),
    }


async def collect_agent_credential_facts(agent_name: str) -> Dict[str, Any]:
    """One bounded probe of a running agent's workspace.

    Returns ``{"status", "facts"}`` where status is:
      * ``"ok"``           — probe ran, `facts` populated
      * ``"not_running"``  — no container, or it isn't running
      * ``"unavailable"``  — exec failed, timed out, or emitted unusable output
    """
    if _COLLECTOR_SCRIPT is None:
        return {"status": "unavailable", "facts": None}

    container = get_agent_container(agent_name)
    if container is None or getattr(container, "status", None) != "running":
        return {"status": "not_running", "facts": None}

    import base64

    b64 = base64.b64encode(_COLLECTOR_SCRIPT.encode("utf-8")).decode("ascii")
    # b64's charset is [A-Za-z0-9+/=] — shell-safe inside the double quotes, and
    # nothing caller-controlled is interpolated. `timeout` is coreutils, present
    # in the base image. `2>/dev/null` keeps stdout pure JSON (exec_run merges
    # stderr into stdout) and stops interpreter noise reaching a parse.
    command = (
        'bash -c "echo {b64} | base64 -d | timeout {t} python3 - 2>/dev/null"'
    ).format(b64=b64, t=_SCRIPT_TIMEOUT)

    try:
        result = await asyncio.wait_for(
            execute_command_in_container(
                container_name="agent-{0}".format(agent_name),
                command=command,
                timeout=_SCRIPT_TIMEOUT,
            ),
            timeout=_REQUEST_TIMEOUT,
        )
    except asyncio.TimeoutError:
        # The container-side `timeout` is what actually frees the pool thread;
        # this bound only stops the REQUEST hanging behind it.
        logger.warning(
            "[credential-requirements] probe timed out for %s after %ss",
            agent_name, _REQUEST_TIMEOUT,
        )
        return {"status": "unavailable", "facts": None}
    except Exception:  # noqa: BLE001 — a probe failure is a degraded read, never a 500
        logger.warning(
            "[credential-requirements] probe failed for %s", agent_name, exc_info=False
        )
        return {"status": "unavailable", "facts": None}

    exit_code = result.get("exit_code")
    if exit_code != 0:
        # Exit code ONLY. On failure `execute_command_in_container` puts an
        # exception string in `output`, and on success `output` is a JSON blob
        # derived from files the agent controls. Neither belongs in a log line.
        logger.warning(
            "[credential-requirements] probe exec failed for %s (exit=%s)",
            agent_name, exit_code,
        )
        return {"status": "unavailable", "facts": None}

    raw = (result.get("output") or "").strip()
    if not raw:
        return {"status": "unavailable", "facts": None}
    try:
        facts = json.loads(raw)
    except (ValueError, TypeError):
        logger.warning("[credential-requirements] probe output unparseable for %s", agent_name)
        return {"status": "unavailable", "facts": None}
    if not _shape_ok(facts):
        logger.warning("[credential-requirements] probe output failed the shape guard for %s", agent_name)
        return {"status": "unavailable", "facts": None}

    return {"status": "ok", "facts": _normalize_facts(facts)}


# ---------------------------------------------------------------------------
# Report assembly
# ---------------------------------------------------------------------------

# Advisory rows (§2.6) are bounded independently of the normalizer's own record
# cap: they come from `${VAR}` references in files the AGENT can rewrite, so a
# hostile or broken template must not be able to render a 10,000-row checklist.
_MAX_ADVISORY = 50


class _NoAliasSafeLoader(yaml.SafeLoader):
    """`SafeLoader` that refuses YAML *aliases*.

    `template.yaml` here comes from a LIVE, agent-writable workspace, and alias
    expansion is a documented amplifier in this very codebase — `_clean_field`
    measures 443 B expanding to 52 MB in 1.5 s, x10 per alias level. Rejecting
    at compose time is precise: it costs a template nothing unless it actually
    *uses* an alias, unlike a textual `&`/`*` scan, which would false-reject any
    description containing an asterisk.
    """

    def compose_node(self, parent, index):
        if self.check_event(yaml.events.AliasEvent):
            raise yaml.YAMLError("YAML aliases are not permitted in template.yaml")
        return super().compose_node(parent, index)


def _parse_template_text(text: str) -> Optional[dict]:
    """Parse a live `template.yaml`. `None` for anything that isn't a mapping."""
    try:
        data = yaml.load(text, Loader=_NoAliasSafeLoader)
    except Exception:  # noqa: BLE001 — untrusted YAML; unreadable is a state, not a 500
        return None
    return data if isinstance(data, dict) else None


def _safe_normalize(data: dict) -> tuple:
    """`normalize_credential_requirements`, wrapped NARROWLY and deliberately.

    Its docstring calls "never raises" a load-bearing security property, and its
    own callers wrap it anyway rather than resting the property on one
    function's discipline. This wrapper is that belt — and it is scoped to the
    single call, not to the whole report build: the ent#128 lesson is that
    `run_static`-style blanket swallowing turns a raise into a verdict
    indistinguishable from a pass. Every other failure in this module sets an
    explicit `degraded_reason` instead.
    """
    try:
        return normalize_credential_requirements(data, source_trust="deployed")
    except Exception:  # noqa: BLE001
        logger.exception(
            "[credential-requirements] normalize raised on a deployed template"
        )
        return [], ["template declaration could not be read"]


def _observed_operator_names(facts: Dict[str, Any]) -> List[Dict[str, str]]:
    """Operator-suppliable variable names OBSERVED but never declared (§2.6).

    AC #1 names three sources. Using `credentials:` alone produces a
    confidently-wrong green: of the 25 bundled templates, 12 declare
    `credentials: {}` and 13 declare nothing at all, so a legacy template with
    `${SLACK_BOT_TOKEN}` in `.mcp.json.template` would render "Ready — this
    agent needs no credentials". The codebase already treats
    undeclared-but-referenced as a HARD compatibility failure (K-002/T-015).

    These names are an ANTI-GREEN SIGNAL and nothing more. `.mcp.json.template`
    never becomes a declaration authority — the schema's authoring note forbids
    it — so every row built from this is advisory: never `required`, never
    counted as blocking.
    """
    seen = []
    order = {}

    mcp_text = facts.get("mcp_template_text") or ""
    for name in CREDENTIAL_DETECTOR_REF_RE.findall(mcp_text):
        if name not in order:
            order[name] = "observed:mcp_template"
            seen.append(name)

    env_example_text = facts.get("env_example_text") or ""
    for line in env_example_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name = line.split("=", 1)[0].strip()
        if is_credential_var_name(name) and name not in order:
            order[name] = "observed:env_example"
            seen.append(name)

    # Platform-injected vars are never the operator's to supply. Filtered
    # through the PUBLIC accessor (fed a synthetic block) rather than the
    # private `_is_platform_injected`, so this module keeps its zero-edit
    # relationship with template_service.
    suppliable = operator_supplied_credential_names({"env_file": seen})
    return [{"name": n, "source": order[n]} for n in suppliable][:_MAX_ADVISORY]


def _requirement_row(record: Dict[str, Any], *, status: str, advisory: bool) -> Dict[str, Any]:
    """One API row from one normalized record."""
    setup_url = record.get("setup_url")
    display = describe_setup_url_or_none(setup_url)
    return {
        "name": record.get("name"),
        "title": record.get("title") or record.get("name"),
        "description": record.get("description"),
        # Tri-state, preserved end to end. `"unknown"` means the author never
        # opted in; rendering it as required cries wolf on every legacy template.
        "required": record.get("required", "unknown"),
        # Fail-safe: mask unless an author explicitly said otherwise.
        "secret": record.get("secret", True) is not False,
        "format": record.get("format"),
        # A PLACEHOLDER, never a prefilled value — see the response model.
        "default": record.get("default"),
        "source": record.get("source"),
        "advisory": advisory,
        "status": status,
        "setup_url": setup_url,
        "setup_url_display_host": (display or {}).get("display_host"),
        "setup_url_registrable": (display or {}).get("registrable"),
        "setup_url_verified": bool((display or {}).get("verified")),
    }


def _status_for(name: str, *, status_source: str, env_keys: set) -> str:
    """Tri-state per-variable status.

    `.env` ABSENT is a definite `missing`, not `unknown`: a `github:` agent never
    gets a generated `.env` at all (`_stage_config_files` guards on
    `template_data`, which only the `local:` arm populates), and the ent#123
    tokenless seeded fleet — AC #3's literal audience — is entirely `github:`.
    Reporting `unknown` there would make the feature inert for exactly the fleet
    it exists to serve. `unknown` is reserved for "we could not look".
    """
    if status_source != "live":
        return "unknown"
    return "set" if name in env_keys else "missing"


async def _catalog_requirements(agent_name: str) -> tuple:
    """`(records, errors, reason)` from the catalog entry named by the container label.

    Fallback only — reached when the live workspace could not be read. `reason`
    is non-None whenever the catalog could not answer either.
    """
    container = get_agent_container(agent_name)
    labels = {}
    if container is not None:
        try:
            labels = container.labels or {}
        except Exception:  # noqa: BLE001
            labels = {}
    template_id = labels.get("trinity.template") or ""
    if not template_id:
        return [], [], "template_label_missing"

    try:
        if template_id.startswith("local:"):
            entry = get_local_template(template_id)
        elif template_id.startswith("github:"):
            # `_get_cached_metadata` -> `_fetch_template_yaml` uses a SYNCHRONOUS
            # httpx client with a 10s timeout. Called inline from an async route,
            # a cache miss stalls the whole worker for those 10 seconds.
            entry = await asyncio.to_thread(get_github_template, template_id)
        else:
            return [], [], "catalog_unavailable"
    except Exception:  # noqa: BLE001
        logger.warning(
            "[credential-requirements] catalog lookup failed for %s", agent_name
        )
        return [], [], "catalog_unavailable"

    if not isinstance(entry, dict):
        return [], [], "catalog_unavailable"
    records = entry.get("credential_requirements") or []
    errors = entry.get("credential_errors") or []
    if not isinstance(records, list):
        return [], [], "catalog_unavailable"
    return records, [e for e in errors if isinstance(e, str)], None


async def build_report(agent_name: str) -> Dict[str, Any]:
    """The whole per-agent credential-requirements report. Never raises.

    Authority is the LIVE workspace (`/home/developer/template.yaml`), because a
    forked or hand-edited agent's requirements drift from the catalog entry it
    was created from — the AC #3 case. The catalog is the fallback only, and it
    is reachable ONLY from an already-degraded live read, which is what makes
    "degraded dominates" (below) structural rather than a rule someone has to
    remember.
    """
    facts: Dict[str, Any] = {}
    records: List[Dict[str, Any]] = []
    errors: List[str] = []
    requirements_source = "none"
    status_source = "unavailable"
    degraded_reason = None

    probe = await collect_agent_credential_facts(agent_name)
    if probe["status"] == "not_running":
        degraded_reason = "agent_not_running"
    elif probe["status"] != "ok":
        degraded_reason = "agent_unreachable"
    else:
        facts = probe["facts"] or {}
        # The probe answered, so per-variable status is a live fact even if the
        # template turns out to be unreadable.
        status_source = "live"
        if not facts.get("template_present"):
            degraded_reason = "no_template"
        elif facts.get("template_text") is None:
            degraded_reason = "template_unreadable"
        else:
            data = _parse_template_text(facts["template_text"])
            if data is None:
                degraded_reason = "template_unreadable"
            else:
                records, errors = _safe_normalize(data)
                requirements_source = "live_workspace"

    if requirements_source == "none":
        catalog_records, catalog_errors, catalog_reason = await _catalog_requirements(
            agent_name
        )
        if catalog_reason is not None:
            # Keep the FIRST reason — it names why the live read failed, which is
            # the more actionable half ("agent not running" beats "catalog
            # unavailable" for an operator deciding what to do next).
            degraded_reason = degraded_reason or catalog_reason
        elif catalog_records:
            records = catalog_records
            errors = catalog_errors
            requirements_source = "catalog"
        else:
            # An EMPTY catalog result is degraded data, not data:
            # `get_github_template` returns `_build_template(repo, {})` — empty
            # requirements, not None — when the repo is gone or the fetch failed,
            # and with no PAT (the ent#123 tokenless fleet) GitHub's 60-req/hr
            # anonymous limit makes that the EXPECTED outcome.
            errors = catalog_errors
            degraded_reason = degraded_reason or "catalog_unavailable"

    env_keys = set(facts.get("env_keys_nonempty") or [])

    declared = [r for r in records if isinstance(r, dict) and r.get("name")]
    platform_injected_excluded = sum(1 for r in declared if r.get("platform_injected"))
    operator_records = [r for r in declared if not r.get("platform_injected")]

    requirements = [
        _requirement_row(
            r,
            status=_status_for(r["name"], status_source=status_source, env_keys=env_keys),
            advisory=False,
        )
        for r in operator_records
    ]

    # §2.6: only when `credentials:` declared NOTHING. A template that declares
    # only platform-injected variables HAS opted in, and collapses to
    # "no credentials required" rather than acquiring advisory noise.
    advisory_rows = []
    if not declared:
        for observed in _observed_operator_names(facts):
            advisory_rows.append(
                _requirement_row(
                    {
                        "name": observed["name"],
                        "title": observed["name"],
                        "required": "unknown",
                        "secret": True,
                        "source": observed["source"],
                    },
                    status=_status_for(
                        observed["name"], status_source=status_source, env_keys=env_keys
                    ),
                    advisory=True,
                )
            )
    requirements.extend(advisory_rows)

    # `degraded` DOMINATES, unconditionally. A degraded lookup and a genuinely
    # credential-free agent produce a textually identical empty requirement set,
    # and "Ready — this agent needs no credentials" is the one state a user will
    # never investigate. Reachability is structural (the catalog arm is only
    # entered from a failed live read), but the precedence is stated here so it
    # survives someone adding a fifth state.
    if degraded_reason is not None:
        state = "degraded"
    elif requirements and not advisory_rows:
        state = "ok"
    elif advisory_rows:
        state = "declaration_incomplete"
    else:
        state = "no_credentials_required"

    counts = {"set": 0, "missing": 0, "unknown": 0}
    for row in requirements:
        counts[row["status"]] = counts.get(row["status"], 0) + 1

    return {
        "agent_name": agent_name,
        "state": state,
        "requirements_source": requirements_source,
        "status_source": status_source,
        "degraded_reason": degraded_reason,
        "requirements": requirements,
        "summary": {
            "total": len(requirements),
            "set": counts["set"],
            "missing": counts["missing"],
            "unknown": counts["unknown"],
            # Server-side so the badge and the headline cannot drift. Advisory
            # rows carry `required == "unknown"` and so can never land here.
            "blocking": sum(
                1
                for row in requirements
                if row["required"] is True and row["status"] == "missing"
            ),
            "platform_injected_excluded": platform_injected_excluded,
            "advisory": len(advisory_rows),
        },
        # The normalizer's sanitized messages. Non-printables stripped and
        # length-capped, but NOT HTML-escaped — text-interpolate only.
        "errors": [e for e in errors if isinstance(e, str)][:20],
    }
