"""OpenAI Codex CLI execution service (#1187).

Implements the :class:`AgentRuntime` interface for OpenAI's Codex CLI, the third
Trinity agent runtime alongside Claude Code and Gemini.

Built **independently** on the existing per-runtime primitives (process
registry, concurrency-safe orphan drain, activity tracking, credential
sanitizer) rather than on a shared subprocess helper — see #1187 decision 4.
That keeps Codex from inheriting Gemini's blanket ``kill_cgroup_orphans()``
(which SIGKILLs sibling executions in the same cgroup); Codex uses the
concurrency-safe ``_drain_bounded`` path that preserves other in-flight work.

Safety parity with the Claude path (#1187 decision 8, Phase C):
  * **System prompt / identity** — the backend's effective ``system_prompt``
    is prepended to every turn (Codex ``exec`` has no ``--append-system-prompt``);
    persistent identity comes from ``AGENTS.md`` (startup copies ``CLAUDE.md``).
  * **Read-only mode** — when ``~/.trinity/read-only-config.json`` is enabled,
    Codex runs with ``--sandbox read-only`` (the Claude hook can't apply here).
  * **Guardrails** — read-only is honored via the sandbox; ``disallowed_tools``
    that have no Codex equivalent are SURFACED in the logs, never silently
    dropped.
  * **Credential sanitization** — every stdout line, the final response, and
    stderr pass through ``utils.credential_sanitizer`` exactly as the Claude /
    headless paths do.

Codex specifics:
  * Non-interactive: ``codex exec [PROMPT]``; ``--json`` emits a JSONL event
    stream; ``-o/--output-last-message FILE`` is the durable result record
    (#548/#333) — read-then-delete in ``finally``.
  * Continuity: ``codex exec resume <thread_id>`` replays a prior thread.
  * No native cost — derived from ``turn.completed.usage`` token counts.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from fastapi import HTTPException

from ..models import ExecutionLogEntry, ExecutionMetadata
# CODEX_CONTEXT_WINDOW is re-exported deliberately: it is the legacy-family
# window and is referenced as a module attribute by tests. Not dead code.
from ..model_context import CODEX_CONTEXT_WINDOW, resolve_context_window  # noqa: F401
from ..state import agent_state
from ..utils.credential_sanitizer import (
    sanitize_dict,
    sanitize_subprocess_line,
    sanitize_text,
)
from ..utils.subprocess_pgroup import EXECUTION_TAG_NAME
from ._runtime_config import _DEFAULT_EXECUTION_TIMEOUT_SEC, _load_guardrails
from .activity_tracking import complete_tool_execution, start_tool_execution
from .execution_env import build_execution_env
from .process_registry import get_process_registry
from .runtime_adapter import AgentRuntime, RuntimeCapabilities
from .subprocess_lifecycle import (
    _capture_pgid,
    _drain_bounded,
    _safe_close_pipes,
    _terminate_process_group,
)

logger = logging.getLogger(__name__)

# One long-lived reader-thread worker (mirrors claude_code.py / gemini_runtime.py).
# A fresh ThreadPoolExecutor per call relies on CPython's non-deterministic
# weakref cleanup of the worker thread under load (#333 hardening).
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="codex-subproc")

# GPT-5 context window (input). Cosmetic — drives the context gauge only.
# Single-sourced from the shared model catalog (#1521); imported above.

# ---------------------------------------------------------------------------
# Pricing (#1187, corrected #2207)
# ---------------------------------------------------------------------------
# Codex reports NO cost of its own (``cost_reporting: "estimated"``), so the
# number computed here IS the number Trinity records — it feeds
# ``schedule_executions.cost``, agent analytics, cost-threshold alerts, and the
# loop cost budget ``max_cost_usd`` (#1155). A budget is a SPEND CONTROL, so an
# understated rate is not a cosmetic metric error: it is a control bypass. Rates
# are therefore chosen to fail toward OVER-reporting, mirroring the safe-failure
# direction the context catalog argues for in ``model_context.py``.
#
# Rates are USD **per 1,000,000 tokens**, stated exactly as OpenAI publishes
# them, so this table can be diffed against the rate card by eye. (#2207 changed
# the unit from per-1K: the defect being fixed was a stale transcription, and
# per-1K forced mental arithmetic on every audit.)
#
#   ``input``/``cached``/``output``  — standard tier.
#   ``long_*``                       — the >272K-input tier, present ONLY for
#                                      models OpenAI publishes one for.
#
# Canonical rate card (keep this comment as the bump-anchor):
#     https://developers.openai.com/api/docs/pricing
#     https://learn.chatgpt.com/docs/models   (which ids Codex accepts)
# Last synced: 2026-08-15 (#2207)
#
# CACHE WRITES ARE PRICED, because Codex does report them. A real gpt-5.6-sol
# turn on codex-cli 0.147.0 emits:
#     "usage": {"input_tokens": 11398, "cached_input_tokens": 0,
#               "cache_write_input_tokens": 11395, "output_tokens": 6, ...}
# i.e. ``cache_write_input_tokens`` is a SUBSET of ``input_tokens`` — the same
# subset trap as ``reasoning_output_tokens`` ⊂ ``output_tokens``. Input therefore
# decomposes three ways and each part bills at its own rate:
#     plain = input_tokens - cached_input_tokens - cache_write_input_tokens
# Writes carry a 1.25x surcharge on gpt-5.4+; on that measured turn 99.97% of the
# input were writes, so ignoring them under-reports the input side by ~25%.
#
# STILL out of scope: fast mode (~2x) is genuinely unobservable — nothing in the
# event stream distinguishes it. That omission understates cost, is bounded, and
# is documented rather than guessed.
#
# ``gpt-5.3-codex`` is deliberately absent: it is still reachable with an API key
# but OpenAI publishes no rate for it, so it resolves to ``default`` (which
# over-reports) rather than carrying a number we cannot source. Same for
# ``gpt-5.3-codex-spark``. NEVER add a rate that is not on the card.
LONG_CONTEXT_THRESHOLD_TOKENS = 272_000
"""Input-token count above which the long-context tier applies.

NOT a context window. The collision with the old flat ``CODEX_CONTEXT_WINDOW``
value is exactly the confusion #2207 was filed about: 272K is the price break,
while the 5.6 family's actual window is 1,050,000.
"""

# Field order mirrors the rate card left-to-right: standard tier, then the
# >272K long tier. ``cache_write`` is present ONLY for the models OpenAI lists as
# supporting prompt caching (the 5.6 family, 5.5, 5.4). It is deliberately absent
# from -mini/-nano/-pro and the legacy families: the 1.25x rule is published, but
# the list of models it applies to is ALSO published, and inferring a surcharge
# onto a model outside that list is exactly the "never add a rate that is not on
# the card" violation this table exists to prevent. Absent -> writes bill at the
# plain input rate (see calculate_codex_cost).
CODEX_PRICING: Dict[str, Dict[str, float]] = {
    # --- GPT-5.6 (current; the Codex default is sol) --------------------------
    "gpt-5.6-sol": {
        "input": 5.00, "cached": 0.50, "cache_write": 6.25, "output": 30.00,
        "long_input": 10.00, "long_cached": 1.00,
        "long_cache_write": 12.50, "long_output": 45.00,
    },
    "gpt-5.6-terra": {
        "input": 2.00, "cached": 0.20, "cache_write": 2.50, "output": 12.00,
        "long_input": 4.00, "long_cached": 0.40,
        "long_cache_write": 5.00, "long_output": 18.00,
    },
    "gpt-5.6-luna": {
        "input": 0.20, "cached": 0.02, "cache_write": 0.25, "output": 1.20,
        "long_input": 0.40, "long_cached": 0.04,
        "long_cache_write": 0.50, "long_output": 1.80,
    },
    # --- GPT-5.5 -------------------------------------------------------------
    "gpt-5.5": {
        "input": 5.00, "cached": 0.50, "cache_write": 6.25, "output": 30.00,
        "long_input": 10.00, "long_cached": 1.00,
        "long_cache_write": 12.50, "long_output": 45.00,
    },
    # Pro tiers publish no cached rate; ``cached`` mirrors ``input`` so a cached
    # token is never billed at a discount we cannot source (over-reports, safe).
    "gpt-5.5-pro": {
        "input": 30.00, "cached": 30.00, "output": 180.00,
        "long_input": 60.00, "long_cached": 60.00, "long_output": 270.00,
    },
    # --- GPT-5.4 (retires from Codex 2026-08-31; still on the API rate card) --
    "gpt-5.4": {
        "input": 2.50, "cached": 0.25, "cache_write": 3.125, "output": 15.00,
        "long_input": 5.00, "long_cached": 0.50,
        "long_cache_write": 6.25, "long_output": 22.50,
    },
    "gpt-5.4-mini": {"input": 0.75, "cached": 0.075, "output": 4.50},
    "gpt-5.4-nano": {"input": 0.20, "cached": 0.02, "output": 1.25},
    "gpt-5.4-pro": {
        "input": 30.00, "cached": 30.00, "output": 180.00,
        "long_input": 60.00, "long_cached": 60.00, "long_output": 270.00,
    },
    # --- GPT-5.2 -------------------------------------------------------------
    "gpt-5.2": {"input": 1.75, "cached": 0.175, "output": 14.00},
    "gpt-5.2-pro": {"input": 21.00, "cached": 21.00, "output": 168.00},
    # --- GPT-5.1 / GPT-5 (legacy; still resolvable for API-key callers) -------
    # The ``-codex`` variants carry no separate rate-card entry and bill at their
    # base model's rate; the explicit keys below are kept as documentation (the
    # variant-suffix rule in _resolve_pricing would reach the same rates anyway).
    "gpt-5.1-codex": {"input": 1.25, "cached": 0.125, "output": 10.00},
    "gpt-5.1-codex-max": {"input": 1.25, "cached": 0.125, "output": 10.00},
    "gpt-5.1": {"input": 1.25, "cached": 0.125, "output": 10.00},
    "gpt-5-codex": {"input": 1.25, "cached": 0.125, "output": 10.00},
    "gpt-5": {"input": 1.25, "cached": 0.125, "output": 10.00},
    "gpt-5-mini": {"input": 0.25, "cached": 0.025, "output": 2.00},
    "gpt-5-nano": {"input": 0.05, "cached": 0.005, "output": 0.40},
    "gpt-5-pro": {"input": 15.00, "cached": 15.00, "output": 120.00},
    # --- Fallback ------------------------------------------------------------
    # Deliberately the flagship (gpt-5.6-sol) rate, NOT the cheapest tier. Two
    # reasons, and the first is the common case rather than an edge case:
    #   1. An agent that pins no model reaches here. ``_CodexParseState.model``
    #      is the caller-supplied value and ``thread.started`` carries only a
    #      thread id, so an unpinned turn is priced with ``model=None`` while the
    #      CLI actually runs its own default — which IS gpt-5.6-sol.
    #   2. An id we have never seen is more likely a NEWER (pricier) model than
    #      an older one. Guessing cheap silently under-bills; guessing flagship
    #      over-bills a budget into stopping early. Only one of those is safe.
    "default": {
        "input": 5.00, "cached": 0.50, "cache_write": 6.25, "output": 30.00,
        "long_input": 10.00, "long_cached": 1.00,
        "long_cache_write": 12.50, "long_output": 45.00,
    },
}

# Bounded so a pathological stream of unknown ids cannot grow this without limit.
_WARNED_UNKNOWN_MODELS: set = set()
_MAX_WARNED_MODELS = 64


def _resolve_pricing(model: Optional[str]) -> Dict[str, float]:
    """Pricing for ``model`` — exact key, then longest *variant* prefix, then
    ``default``. Total function: never raises, always returns a rate dict.

    THE PREFIX MATCH IS BOUNDARY-AWARE, and that is the point (#2207). A bare
    ``startswith`` makes every key a catch-all for future generations: with
    ``gpt-5`` in the table, a ``gpt-5.7-sol`` released tomorrow silently resolves
    to the oldest, cheapest rate in the file — which is the very defect this
    table was rewritten to fix, pre-armed for the next release. So a prefix only
    matches when the remainder begins with ``-`` (a VARIANT or date suffix of
    the same model) and never when it begins with ``.`` (a new version number):

        gpt-5.1-codex-2025-11-01  -> gpt-5.1-codex   (remainder "-2025-11-01")
        gpt-5-mini-2025-xx        -> gpt-5-mini      (remainder "-2025-xx")
        gpt-5.2-codex             -> gpt-5.2         (remainder "-codex")
        gpt-5.7-sol               -> default         (remainder ".7-sol")

    An unknown id is logged ONCE (not per turn — this runs on every completed
    turn) so a newly-shipped model is visible in the log without flooding it.
    The dedup set is capped; past the cap ids beyond it warn every time rather
    than going silent, because a noisy unknown-model signal beats a hidden one.
    """
    if not model:
        return CODEX_PRICING["default"]
    key = model.strip().lower()
    if not key:
        return CODEX_PRICING["default"]
    if key in CODEX_PRICING:
        return CODEX_PRICING[key]
    candidates = [
        k
        for k in CODEX_PRICING
        if k != "default" and key.startswith(k) and key[len(k) :].startswith("-")
    ]
    if candidates:
        return CODEX_PRICING[max(candidates, key=len)]
    if key not in _WARNED_UNKNOWN_MODELS:
        if len(_WARNED_UNKNOWN_MODELS) < _MAX_WARNED_MODELS:
            _WARNED_UNKNOWN_MODELS.add(key)
        logger.warning(
            "_resolve_pricing: unrecognized Codex model %r; billing at the "
            "flagship default rate (over-reports rather than under-reports). "
            "Add it to CODEX_PRICING in codex_runtime.py if it is real.",
            model,
        )
    return CODEX_PRICING["default"]


def calculate_codex_cost(
    input_tokens: int,
    cached_input_tokens: int,
    output_tokens: int,
    model: Optional[str] = None,
    cache_write_input_tokens: int = 0,
) -> float:
    """Estimated USD cost for a Codex turn.

    ``reasoning_output_tokens`` is a SUBSET of ``output_tokens`` — bill
    ``output_tokens`` once, never ``output_tokens + reasoning_output_tokens``.
    Cached input tokens bill at the cheaper cached rate; only the uncached
    remainder bills at the full input rate.

    ``cache_write_input_tokens`` is likewise a SUBSET of ``input_tokens`` and
    bills at its own surcharged rate, so input splits three ways — plain, cached
    reads, cache writes — and the three must sum back to ``input_tokens``, never
    exceed it.

    Rates in ``CODEX_PRICING`` are per 1,000,000 tokens (#2207). A prompt over
    ``LONG_CONTEXT_THRESHOLD_TOKENS`` escalates the whole request to the model's
    long-context rates where it has them.

    ``cache_write_input_tokens`` defaults to 0 and is the LAST parameter so the
    existing positional call sites keep working unchanged.
    """
    pricing = _resolve_pricing(model)
    # Long-context tier: OpenAI prices the FULL request at 2x input / 1.5x output
    # once the prompt exceeds 272K input tokens. Only applied for models that
    # publish such a tier — the older families cannot reach the threshold anyway
    # (their whole window is <= 272K), so a blanket rule would be fiction.
    use_long = (
        input_tokens > LONG_CONTEXT_THRESHOLD_TOKENS and "long_input" in pricing
    )
    input_rate = pricing["long_input"] if use_long else pricing["input"]
    cached_rate = pricing["long_cached"] if use_long else pricing["cached"]
    output_rate = pricing["long_output"] if use_long else pricing["output"]
    # A model with no published cache-write surcharge bills writes as plain input
    # (the pre-gpt-5.4 behaviour) rather than inventing a multiplier.
    write_key = "long_cache_write" if use_long else "cache_write"
    write_rate = pricing.get(write_key, input_rate)

    cached = max(0, cached_input_tokens)
    writes = max(0, cache_write_input_tokens)
    # Both counters are subsets of input_tokens, but they arrive from an external
    # process: clamp so a malformed payload can never bill a negative plain
    # remainder (which would silently REDUCE the total) or over-count the input.
    if cached + writes > input_tokens:
        writes = max(0, min(writes, input_tokens))
        cached = max(0, min(cached, input_tokens - writes))
    plain_input = max(0, input_tokens - cached - writes)

    input_cost = (
        (plain_input / 1_000_000) * input_rate
        + (cached / 1_000_000) * cached_rate
        + (writes / 1_000_000) * write_rate
    )
    # Same external-payload defence on the output side: a negative count would
    # otherwise SUBTRACT from the bill. The loop budget (#1155) ignores
    # non-positive costs fail-open, so a negative here reads as "free".
    output_cost = (max(0, output_tokens) / 1_000_000) * output_rate
    return round(max(0.0, input_cost + output_cost), 6)


# ---------------------------------------------------------------------------
# Credentials, sandbox, CODEX_HOME (parity wiring — #1187 Phase C/T4)
# ---------------------------------------------------------------------------

_API_KEY_VARS = ("OPENAI_API_KEY", "CODEX_API_KEY")
_AGENT_HOME = "/home/developer"
_READ_ONLY_CONFIG = Path(_AGENT_HOME) / ".trinity" / "read-only-config.json"


def _parse_env_value(raw_value: str) -> str:
    """Extract a value from a ``.env`` ``KEY=VALUE`` right-hand side.

    Handles the shapes a human SSH-editing ``.env`` would produce that Trinity's
    own plain ``KEY=VALUE`` writer never emits: a quoted value (the quotes are
    stripped and an interior ``#`` is kept), and an unquoted value with a
    trailing ``# inline comment`` (dropped at the first whitespace-``#``).
    """
    value = raw_value.strip()
    if value[:1] in ('"', "'"):
        quote = value[0]
        end = value.find(quote, 1)
        return value[1:end] if end != -1 else value[1:]
    comment = value.find(" #")
    if comment != -1:
        value = value[:comment].rstrip()
    return value


def _load_api_key_with_source() -> Tuple[Optional[str], Optional[str]]:
    """Resolve the OpenAI/Codex API key AND which variable name it came from.

    The per-agent ``.env`` (CRED-002) is copied to ``/home/developer/.env`` by
    startup.sh but is NOT exported into the agent-server process — so unlike the
    Claude/Gemini key (a container env var), the Codex key must be read from the
    process env (if present) OR parsed out of ``.env`` (the cold-start path the
    outside-voice review flagged). Accepts either OPENAI_API_KEY or CODEX_API_KEY.

    #1971: the SOURCE is now returned, because it decides whether the child
    process is given ``CODEX_API_KEY`` at all. Trinity used to synthesize that
    variable from a key the operator had supplied under a different name, and
    the mere presence of ``CODEX_API_KEY`` flips the Codex CLI into API-key auth
    mode — discarding a valid subscription ``auth.json``.
    """
    for var in _API_KEY_VARS:
        value = os.environ.get(var)
        if value:
            return value, var
    env_path = Path(_AGENT_HOME) / ".env"
    try:
        # `errors="replace"`, found by /edge-cases: `.env` is hand-edited and
        # credentials get pasted, so a single non-UTF-8 byte (a Latin-1 value, a
        # smart quote from the wrong encoding) is entirely plausible — and a
        # bare `read_text()` raises UnicodeDecodeError there. That is a
        # ValueError, NOT an OSError, so the except below never caught it and
        # the error escaped all the way out of `_execute_codex`, failing the
        # dispatch instead of resolving the key or giving the honest 503.
        # Replacing the bad byte lets a valid key line on another row still be
        # found; a key that itself contains non-UTF-8 bytes was never usable.
        for raw in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            # Tolerate `export KEY=VALUE` (a hand-edited .env), not just KEY=VALUE.
            if line.startswith("export "):
                line = line[len("export "):].lstrip()
            key, _, value = line.partition("=")
            key = key.strip()
            if key in _API_KEY_VARS:
                cleaned = _parse_env_value(value)
                if cleaned:
                    return cleaned, key
    except (IOError, OSError):
        pass
    return None, None


def _load_openai_api_key() -> Optional[str]:
    """The key alone. Thin wrapper over :func:`_load_api_key_with_source`.

    Kept as the public shape because it is the seam existing tests monkeypatch;
    `_execute_codex` still calls THIS, not the tuple variant, so a patched
    `_load_openai_api_key` keeps working exactly as before.
    """
    return _load_api_key_with_source()[0]


def _api_key_source_name() -> Optional[str]:
    """Which variable the resolved key came from, or None.

    Split from the value lookup rather than returned alongside it so
    `_load_openai_api_key` stays the single patchable seam (#1971 review). The
    extra resolution is one dict lookup plus, at worst, one small file read.
    """
    return _load_api_key_with_source()[1]


# ``codex login --with-api-key`` writes {"auth_mode": "apikey", "OPENAI_API_KEY": ...};
# a ChatGPT-plan login writes {"auth_mode": "chatgpt", "tokens": {...}, ...}. The MODE
# is the discriminator, NOT the presence of an ``OPENAI_API_KEY`` field — a subscription
# auth.json carries that key too (usually null), so a presence test misreads it.
_AUTH_MODE_API_KEY = "apikey"

# One writer at a time. ``_execute_codex`` runs concurrently up to the agent's
# ``max_parallel_tasks``, and two simultaneous first turns would otherwise both
# shell out to ``codex login`` against the same file.
_AUTH_MATERIALIZE_LOCK = asyncio.Lock()

# ``codex login`` is a local file write; it must never hang a turn.
_LOGIN_TIMEOUT_SECONDS = 30


# Tri-state, because "no file" and "a file I cannot parse" must NOT collapse:
# the first is safe to write, the second is a credential of unknown provenance
# that we must leave exactly where it is.
_AUTH_ABSENT = "absent"
_AUTH_UNREADABLE = "unreadable"
_AUTH_PARSED = "parsed"


def _read_auth_json(codex_home: str) -> Tuple[str, Optional[Dict]]:
    """``(state, data)`` for ``$CODEX_HOME/auth.json``.

    ``data`` is populated only for :data:`_AUTH_PARSED`. Anything we cannot read
    or that is not a JSON object is :data:`_AUTH_UNREADABLE` — callers treat it
    as "not ours", never as "safe to overwrite".
    """
    auth = Path(codex_home) / "auth.json"
    try:
        if not auth.is_file() or auth.stat().st_size == 0:
            return _AUTH_ABSENT, None
        data = json.loads(auth.read_text())
    except OSError as exc:
        logger.warning("[Codex] auth.json present but unreadable (%s)", exc)
        return _AUTH_UNREADABLE, None
    except ValueError as exc:
        logger.warning("[Codex] auth.json present but not valid JSON (%s)", exc)
        return _AUTH_UNREADABLE, None
    if not isinstance(data, dict):
        logger.warning("[Codex] auth.json is not a JSON object; leaving it alone")
        return _AUTH_UNREADABLE, None
    return _AUTH_PARSED, data


def _has_subscription_auth(codex_home: str) -> bool:
    """True when this container holds a Codex **subscription** credential.

    #1971: subscription auth lives in ``$CODEX_HOME/auth.json`` and is the whole
    point of a ChatGPT-plan Codex agent — such a container has NO API key by
    design. Trinity modelled only the API-key path, so `_execute_codex` refused
    to start at all without one, and the reporter's working setup needed a
    *placeholder* ``OPENAI_API_KEY`` to get past the gate. A placeholder that
    exists solely to satisfy a check is the check being wrong.

    #2208 narrows it from existence to **mode**. That is not the token
    validation #1971 declined to do (still the CLI's job) — it is classifying
    who wrote the file. Trinity now writes an ``auth_mode: apikey`` auth.json
    itself, so an existence test would report every API-key agent as a
    subscription agent, and the gate below would then let a container whose key
    was REMOVED from ``.env`` keep running on the stale file it left behind —
    silent failure of credential revocation, the #1999 class.

    An auth.json with no ``auth_mode`` (older CLI) — or one we cannot parse —
    still counts as a subscription credential: unknown provenance is not ours to
    discount, and over-reporting here preserves the pre-#2208 behaviour exactly.
    That matters for the unreadable case specifically, because #1971's whole
    point is that a bad auth.json should surface as the CLI's own auth error,
    not as a Trinity 503 claiming no credential is configured.
    """
    state, data = _read_auth_json(codex_home)
    if state == _AUTH_ABSENT:
        return False
    if state == _AUTH_UNREADABLE:
        return True
    return data.get("auth_mode") != _AUTH_MODE_API_KEY


def _login_with_api_key(codex_home: str, api_key: str) -> Tuple[bool, str]:
    """Run ``codex login --with-api-key``, key on **stdin**.

    Never argv: a process listing is world-readable inside the container, and
    the agent's own turns can read it. Returns ``(ok, detail)``; ``detail`` is
    sanitized CLI output for logging, never the key.
    """
    try:
        proc = subprocess.run(
            ["codex", "login", "--with-api-key"],
            input=api_key,
            # #1999: every spawn site builds its env through the helper — a
            # `{**os.environ}` snapshot re-applies credentials the agent already
            # removed from `.env`. Guarded by test_1999_execution_env, which
            # caught this line.
            env=build_execution_env({"CODEX_HOME": codex_home}),
            capture_output=True,
            text=True,
            timeout=_LOGIN_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return False, f"timed out after {_LOGIN_TIMEOUT_SECONDS}s"
    except (OSError, ValueError) as exc:
        return False, sanitize_text(str(exc))
    if proc.returncode != 0:
        detail = sanitize_text((proc.stderr or proc.stdout or "").strip())
        return False, f"exit {proc.returncode}: {detail[:300]}"
    return True, ""


def _needs_api_key_login(codex_home: str, api_key: str) -> bool:
    """True only when we may write, and writing would change something.

    False for: a subscription credential, an auth.json we cannot classify, and
    one that already carries this exact key (so the common path is one small
    file read per turn, not a subprocess).
    """
    state, data = _read_auth_json(codex_home)
    if state == _AUTH_UNREADABLE:
        return False
    if state == _AUTH_ABSENT:
        return True
    if data.get("auth_mode") != _AUTH_MODE_API_KEY:
        return False
    return data.get("OPENAI_API_KEY") != api_key


async def _materialize_api_key_auth(codex_home: str, api_key: str) -> None:
    """Ensure ``$CODEX_HOME/auth.json`` carries the CURRENT API key (#2208).

    Passing ``OPENAI_API_KEY`` in the environment is no longer sufficient: the
    CLI authenticates its ``wss://api.openai.com/v1/responses`` transport from
    ``auth.json``, and that transport is no longer toggleable
    (``responses_websockets`` is listed as *removed* in ``codex features list``).
    With only the env var set, every turn fails ``401 Unauthorized`` and exits
    non-zero — i.e. an API-key Codex agent could not complete a single turn.

    Three properties, each load-bearing:

    * **Subscription auth wins** (#1971) — a non-``apikey`` auth.json is left
      untouched, as is one we cannot parse. We overwrite only a file we can
      positively identify as our own.
    * **Key rotation propagates** — a stored key that no longer matches the
      resolved one is re-logged-in. ``_load_api_key_with_source`` re-resolves
      per turn (process env first, then ``/home/developer/.env``), so an edited
      ``.env`` changes the key under a live container; an auth.json pinned at
      its first-turn value would silently outrank it. Note the precedence: a key
      baked into the container env still wins over ``.env`` — rotation via
      ``.env`` only reaches agents that do not carry one.
    * **Never raises** — a login failure is logged at ERROR and the turn
      proceeds to fail with the CLI's own auth error. Raising here would convert
      an auth problem into a Trinity 503 on the subscription path too.
    """
    try:
        if not _needs_api_key_login(codex_home, api_key):
            return

        async with _AUTH_MATERIALIZE_LOCK:
            # Re-check under the lock: a concurrent first turn may have just done it.
            if not _needs_api_key_login(codex_home, api_key):
                return
            rotating = _read_auth_json(codex_home)[0] == _AUTH_PARSED
            ok, detail = await asyncio.to_thread(_login_with_api_key, codex_home, api_key)
    except Exception as exc:  # never raises — see docstring
        # The helpers below catch OSError/ValueError/TimeoutExpired, but "never
        # raises" has to hold for everything: this runs on the turn path, and an
        # escaping exception would turn an auth problem into a 500 — including
        # for a subscription agent that needed nothing from this function.
        logger.error(
            "[Codex] auth materialisation failed unexpectedly (%s: %s); "
            "continuing — the turn will fail with the CLI's own auth error",
            type(exc).__name__,
            sanitize_text(str(exc)),
        )
        return

    if ok:
        logger.info(
            "[Codex] materialised API-key auth.json under %s (%s)",
            codex_home,
            "rotated" if rotating else "first use",
        )
    else:
        logger.error(
            "[Codex] could not write API-key auth.json under %s (%s); "
            "the turn will fail with the CLI's own auth error",
            codex_home,
            detail,
        )


def _codex_home() -> str:
    """Non-workspace home for Codex state + the ``-o`` result file.

    Codex defaults ``CODEX_HOME`` to ``~/.codex`` — inside the git-tracked agent
    repo, which would dirty auto-sync. Relocate it under ``$TMPDIR`` (the
    disk-backed ``/home/developer/.tmp`` scratch dir, #1098) which startup.sh
    gitignores for Codex agents.
    """
    explicit = os.environ.get("CODEX_HOME")
    if explicit:
        return explicit
    tmpdir = os.environ.get("TMPDIR") or os.path.join(_AGENT_HOME, ".tmp")
    return os.path.join(tmpdir, "codex")


def _ensure_codex_home() -> str:
    home = _codex_home()
    try:
        os.makedirs(home, exist_ok=True)
    except OSError as exc:  # pragma: no cover - defensive
        logger.warning("[Codex] could not create CODEX_HOME %s: %s", home, exc)
    return home


def _is_read_only() -> bool:
    """True when the backend has put this agent in read-only mode.

    The signal is the same JSON file the Claude read-only *hook* consumes
    (``~/.trinity/read-only-config.json`` → ``enabled``). Codex can't run Claude
    hooks, so we read the file directly and translate it to ``--sandbox
    read-only`` (a sandbox-native, non-cooperative enforcement).

    An absent file ⇒ not read-only (the normal writable-agent state — silent).
    A present-but-unreadable/corrupt file fails OPEN **and logs**, matching the
    reference hook (``read-only-guard.py`` logs ``read_only_config_load_error``
    and allows). Diverging one runtime to fail-closed would make read-only
    enforcement inconsistent across runtimes (CSO #1187 finding 3); if the
    platform wants fail-closed, change both loaders together in a dedicated
    issue.
    """
    try:
        raw = _READ_ONLY_CONFIG.read_text()
    except FileNotFoundError:
        return False
    except OSError as exc:
        logger.warning(
            "[Codex] read-only config unreadable (%s); treating as not read-only", exc
        )
        return False
    try:
        return bool(json.loads(raw).get("enabled"))
    except json.JSONDecodeError as exc:
        logger.warning(
            "[Codex] read-only config malformed (%s); treating as not read-only", exc
        )
        return False


def _resolve_sandbox_mode() -> str:
    """Map Trinity's mode to a Codex ``--sandbox`` value.

    Normal (writable) agents run with ``danger-full-access``, which DISABLES
    Codex's own bubblewrap sandbox. ``workspace-write``/``read-only`` both invoke
    ``bwrap`` to create a user namespace, which the hardened Trinity container
    forbids (``bwrap: No permissions to create a new namespace``) — so any
    in-sandbox mode blocks EVERY shell tool. The Trinity container is already the
    security boundary (``cap_drop ALL`` + AppArmor + ``no-new-privileges``),
    exactly the posture Claude and Gemini run under (no internal sandbox), so
    dropping Codex's redundant inner sandbox weakens nothing.

    Read-only mode is the deliberate exception: it keeps ``--sandbox read-only``
    (sandbox-native write protection) as the interim enforcement. A fail-closed
    read-only enforcement story for Codex is a fast-follow.
    """
    return "read-only" if _is_read_only() else "danger-full-access"


def _surface_unmapped_guardrails(allowed_tools: Optional[List[str]]) -> None:
    """Honor what maps to Codex's control surface; SURFACE (never silently
    drop) the rest (#1187 decision 8 + the unresolved-decision caveat).

    Read-only is enforced via the sandbox. Claude ``disallowed_tools`` names
    (Bash, Write, Edit, WebSearch, …) have no 1:1 Codex ``exec`` CLI toggle in
    the MVP, so we log them at WARNING for operator visibility rather than
    pretending they're enforced.
    """
    guardrails = _load_guardrails()
    disallowed = guardrails.get("disallowed_tools") or []
    if disallowed:
        logger.warning(
            "[Codex] guardrails disallow %s — Codex exec has no per-tool CLI "
            "toggle in the MVP; only read-only (sandbox) and network access are "
            "enforced. Tracking finer-grained Codex tool gating as a fast-follow.",
            disallowed,
        )
    if allowed_tools:
        logger.info(
            "[Codex] allowed_tools=%s requested; Codex exec runs its full tool "
            "set under the sandbox (no allowlist CLI flag in the MVP).",
            allowed_tools,
        )


def _compose_prompt(system_prompt: Optional[str], prompt: str) -> str:
    """Codex ``exec`` has no system-prompt flag, so the effective platform
    prompt (platform instructions + execution context + caller prompt, always
    sent by the backend) is prepended to the user message. Persistent identity
    additionally comes from AGENTS.md."""
    if system_prompt:
        return f"{system_prompt}\n\n---\n\n{prompt}"
    return prompt


def _ensure_within(base: str, path: str) -> str:
    """Resolve ``path`` and confirm it stays within ``base``; raise otherwise.

    Defense-in-depth at the filesystem sink. The result filename is already
    reduced to a safe token by ``_safe_result_token`` + a fixed ``-last.txt``
    suffix, so this never trips in practice — but anchoring the containment
    check at the ``open``/``unlink`` sink keeps the safety property local to the
    operation that actually touches the filesystem (and is the barrier static
    analysis recognizes)."""
    base_real = os.path.realpath(base)
    target = os.path.realpath(path)
    if target != base_real and not target.startswith(base_real + os.sep):
        raise ValueError(f"result path escapes codex_home: {path!r}")
    return target


def _read_and_consume_result_file(path: str, base: str) -> Optional[str]:
    """Read the ``-o`` durable result file. Deletion is the caller's ``finally``
    (read-then-delete, happy + error path — #1187 decision 5). ``base`` anchors
    the sink-side containment guard (see ``_ensure_within``)."""
    try:
        with open(_ensure_within(base, path), "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except ValueError:
        # Containment guard tripped — must never happen in practice; surface it
        # rather than masking a genuine path bug as a benign missing file.
        logger.warning("[Codex] refusing to read result file outside codex_home: %r", path)
        return None
    except (IOError, OSError):
        return None


def _safe_unlink(path: str, base: str) -> None:
    try:
        os.unlink(_ensure_within(base, path))
    except ValueError:
        logger.warning("[Codex] refusing to unlink result file outside codex_home: %r", path)
    except OSError:
        pass


def _safe_result_token(execution_id: str) -> str:
    """Filesystem-safe token for the ``-o`` result filename. ``execution_id`` is
    system-generated today (uuid4 fallback / backend urlsafe token), but never
    build a path from it unguarded: reduce it to a basename and a conservative
    charset so a '/' or '..' can't escape CODEX_HOME (defense-in-depth — CSO
    #1187 finding 2)."""
    token = re.sub(r"[^A-Za-z0-9_.-]", "_", os.path.basename(execution_id))
    return token or "codex"


def _resolve_returned_session_id(metadata: ExecutionMetadata) -> Optional[str]:
    """The thread id to cache for chat continuity (review I4).

    Codex emits ``thread.started`` on every ``exec``; if it somehow didn't,
    return ``None`` so the next turn degrades to a fresh run — NOT a fabricated
    id (e.g. the random ``execution_id``), which would make the next
    ``codex exec resume <id>`` fail hard and repeat every turn.
    """
    return metadata.session_id


def _finalize_codex_response(
    result_file_text: Optional[str], response_parts: List[str]
) -> str:
    """The ``-o`` file is the authoritative response; JSONL ``agent_message``
    parts are the fallback when the file is missing/empty (#1187 decision 5)."""
    if result_file_text and result_file_text.strip():
        return result_file_text.strip()
    return "\n".join(response_parts).strip()


# ---------------------------------------------------------------------------
# JSONL event parsing
# ---------------------------------------------------------------------------

# item.type values that represent tool/command activity (vs. agent_message /
# reasoning / todo_list). Confirmed against codex exec_events.rs ThreadItemDetails.
_CODEX_TOOL_ITEM_TYPES = {
    "command_execution",
    "file_change",
    "mcp_tool_call",
    "web_search",
}

_CODEX_TOOL_DISPLAY = {
    "command_execution": "Shell",
    "file_change": "FileChange",
    "mcp_tool_call": "McpTool",
    "web_search": "WebSearch",
}


@dataclass
class _CodexParseState:
    """Mutable accumulators threaded through per-event parsing."""

    execution_log: List[ExecutionLogEntry]
    metadata: ExecutionMetadata
    response_parts: List[str]
    model: Optional[str] = None
    seen_tool_ids: set = field(default_factory=set)


def _tool_display_name(item: dict, item_type: str) -> str:
    if item_type == "mcp_tool_call":
        tool = item.get("tool") or item.get("name")
        server = item.get("server")
        if tool:
            return f"{server}.{tool}" if server else str(tool)
    return _CODEX_TOOL_DISPLAY.get(item_type, item_type)


def _tool_input(item: dict, item_type: str) -> dict:
    if item_type == "command_execution":
        return {"command": item.get("command")}
    if item_type == "web_search":
        return {"query": item.get("query")}
    if item_type == "file_change":
        return {"changes": item.get("changes")}
    if item_type == "mcp_tool_call":
        return {"arguments": item.get("arguments")}
    return {}


def _tool_output(item: dict, item_type: str) -> str:
    for key in ("aggregated_output", "output", "result", "stdout"):
        value = item.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _record_tool_use(state: _CodexParseState, tool_id: str, item: dict, item_type: str) -> None:
    if tool_id in state.seen_tool_ids:
        return
    state.seen_tool_ids.add(tool_id)
    name = _tool_display_name(item, item_type)
    tool_input = _tool_input(item, item_type)
    state.execution_log.append(
        ExecutionLogEntry(
            id=tool_id,
            type="tool_use",
            tool=name,
            input=tool_input,
            timestamp=datetime.now().isoformat(),
        )
    )
    try:
        start_tool_execution(tool_id, name, tool_input)
    except Exception:  # noqa: BLE001 - activity tracking is best-effort
        logger.debug("[Codex] start_tool_execution failed for %s", tool_id, exc_info=True)


def _record_tool_result(state: _CodexParseState, tool_id: str, item: dict, item_type: str) -> None:
    name = _tool_display_name(item, item_type)
    output = _tool_output(item, item_type)
    status = item.get("status")
    exit_code = item.get("exit_code")
    is_error = status == "failed" or (isinstance(exit_code, int) and exit_code != 0)
    state.execution_log.append(
        ExecutionLogEntry(
            id=tool_id,
            type="tool_result",
            tool=name,
            output=output or None,
            success=not is_error,
            timestamp=datetime.now().isoformat(),
        )
    )
    try:
        complete_tool_execution(tool_id, not is_error, output)
    except Exception:  # noqa: BLE001
        logger.debug("[Codex] complete_tool_execution failed for %s", tool_id, exc_info=True)


def _process_codex_event(event: dict, state: _CodexParseState) -> None:
    """Update ``state`` from one parsed Codex JSONL event. Tolerant of unknown
    event/item types and missing fields — the ``-o`` file is authoritative for
    the response, so a best-effort parser here only affects tokens, tool
    activity, and error classification."""
    event_type = event.get("type")

    if event_type == "thread.started":
        state.metadata.session_id = event.get("thread_id") or state.metadata.session_id

    elif event_type == "turn.completed":
        usage = event.get("usage") or {}
        input_tokens = int(usage.get("input_tokens") or 0)
        cached = int(usage.get("cached_input_tokens") or 0)
        # Subset of input_tokens, surcharged on gpt-5.4+ (#2207). Verified
        # present on codex-cli 0.147.0; absent on older payloads -> 0.
        cache_writes = int(usage.get("cache_write_input_tokens") or 0)
        output_tokens = int(usage.get("output_tokens") or 0)
        # reasoning_output_tokens is a subset of output_tokens — do NOT add it.
        state.metadata.input_tokens = input_tokens
        state.metadata.output_tokens = output_tokens
        state.metadata.cache_read_tokens = cached
        state.metadata.cost_usd = calculate_codex_cost(
            input_tokens, cached, output_tokens, state.model, cache_writes
        )

    elif event_type == "turn.failed":
        error = event.get("error") or {}
        state.metadata.error_type = "turn_failed"
        state.metadata.error_message = (
            error.get("message") if isinstance(error, dict) else str(error)
        ) or "Codex turn failed"

    elif event_type == "error":
        state.metadata.error_type = "error"
        state.metadata.error_message = event.get("message") or "Codex error"

    elif event_type in ("item.started", "item.updated", "item.completed"):
        item = event.get("item") or {}
        item_type = item.get("type") or (item.get("details") or {}).get("type")
        if not item_type:
            return
        item_id = item.get("id") or str(uuid.uuid4())

        if item_type == "agent_message":
            if event_type == "item.completed":
                text = item.get("text") or item.get("message") or ""
                if text:
                    state.response_parts.append(text)
        elif item_type in _CODEX_TOOL_ITEM_TYPES:
            if event_type == "item.started":
                _record_tool_use(state, item_id, item, item_type)
            elif event_type == "item.completed":
                _record_tool_use(state, item_id, item, item_type)  # no-op if seen
                _record_tool_result(state, item_id, item, item_type)
        elif item_type == "error":
            state.metadata.error_type = "error"
            state.metadata.error_message = (
                item.get("message") or state.metadata.error_message or "Codex item error"
            )


def parse_codex_jsonl(
    lines: List[str], model: Optional[str] = None
) -> Tuple[str, List[ExecutionLogEntry], ExecutionMetadata, List[Dict]]:
    """Parse a full Codex ``--json`` line stream (unit-test entrypoint).

    Returns ``(response_text, execution_log, metadata, raw_messages)`` where
    ``response_text`` is the JSONL-assembled fallback (the live path overrides
    it with the ``-o`` file)."""
    metadata = ExecutionMetadata()
    # Model-aware, not flat (#2207): the 5.6 family's window is 1.05M, not 272K.
    # The live path already resolves per-model via get_context_window(); this
    # entrypoint hardcoded the legacy constant, so tests and production disagreed
    # about the very value under test.
    metadata.context_window = resolve_context_window(model)
    state = _CodexParseState(execution_log=[], metadata=metadata, response_parts=[], model=model)
    raw_messages: List[Dict] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            raw_messages.append(event)
            _process_codex_event(event, state)
    metadata.tool_count = len([e for e in state.execution_log if e.type == "tool_use"])
    response_text = "\n".join(state.response_parts).strip()
    return response_text, state.execution_log, metadata, raw_messages


# ---------------------------------------------------------------------------
# Error classification (return-code path)
# ---------------------------------------------------------------------------

# AUTH detection is anchored, not bare-substring. Bare "401"/"api key" are
# over-broad — a non-auth failure whose output merely contains "401" (e.g. an
# upstream MCP/tool returning 401) must NOT be read as an auth failure, because
# 503 is the backend's AUTH signal and the dispatch breaker counts AUTH only
# (#1187 decision 3, review I1). Each pattern names an actual auth condition and
# uses word boundaries so it won't fire on an incidental token.
_AUTH_PATTERNS = (
    re.compile(r"\bunauthorized\b", re.IGNORECASE),
    re.compile(r"\b401\s+unauthorized\b", re.IGNORECASE),
    re.compile(r"\b(?:invalid|incorrect|missing|no)[ _]api[ _]key\b", re.IGNORECASE),
    re.compile(r"\bnot\s+authenticated\b", re.IGNORECASE),
    re.compile(r"\bauthentication\s+(?:failed|error)\b", re.IGNORECASE),
)
_RATE_MARKERS = ("429", "rate limit", "rate_limit", "quota", "too many requests")


def _classify_codex_failure(
    return_code: int, stderr: str, metadata: ExecutionMetadata
) -> Tuple[int, str]:
    """Map a non-zero Codex exit (+ stderr + parsed error) to an HTTP status.

    auth → 503, rate-limit → 429, everything else → 500 (runtime-unavailable).
    Crucially a generic runtime failure is 500, NOT 503 — 503 is the backend's
    AUTH signal and the dispatch breaker counts AUTH only (#1187 decision 3)."""
    haystack = " ".join(
        s for s in (stderr or "", metadata.error_message or "") if s
    )
    haystack_lower = haystack.lower()
    if any(marker in haystack_lower for marker in _RATE_MARKERS):
        return 429, f"Codex rate limit: {(stderr or metadata.error_message or '')[:300]}"
    if any(pattern.search(haystack) for pattern in _AUTH_PATTERNS):
        return 503, (
            f"Codex authentication failure: {(stderr or metadata.error_message or '')[:300]}. "
            "Check OPENAI_API_KEY."
        )
    detail = stderr.strip() or metadata.error_message or "see agent logs"
    return 500, f"Codex execution failed (exit code {return_code}): {detail[:300]}"


# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------

class CodexRuntime(AgentRuntime):
    """OpenAI Codex CLI implementation of AgentRuntime."""

    def __init__(self) -> None:
        # Codex thread id for the interactive chat session (continuity). The
        # singleton instance persists across /api/chat calls in a container.
        self._chat_thread_id: Optional[str] = None

    # -- capability declaration (#1187 Phase G) --------------------------------
    @classmethod
    def capabilities(cls) -> RuntimeCapabilities:
        return RuntimeCapabilities(
            chat_continuity=True,        # codex exec resume <thread_id>
            session_tab_resume=False,    # MVP: Session tab stays Claude/Gemini
            mcp_support=True,            # codex mcp add
            cost_reporting="estimated",  # no native cost → derived from tokens
        )

    def is_available(self) -> bool:
        try:
            result = subprocess.run(
                ["codex", "--version"], capture_output=True, text=True, timeout=5
            )
            return result.returncode == 0
        except Exception:
            return False

    def get_default_model(self) -> str:
        return "gpt-5.6-sol"

    def get_context_window(self, model: Optional[str] = None) -> int:
        """Fallback context window for Codex/GPT-5 models (#1521).

        Resolves via the shared catalog; falls back to the default Codex model id
        so a None model still resolves to Codex's 272K window.
        """
        return resolve_context_window(model or self.get_default_model())

    def configure_mcp(self, mcp_servers: Dict) -> bool:
        """Delegate to the shared Codex MCP configuration in trinity_mcp.py."""
        from .trinity_mcp import _configure_codex_mcp_servers

        return _configure_codex_mcp_servers(mcp_servers)

    # -- command construction --------------------------------------------------
    def _build_codex_command(
        self,
        *,
        model: Optional[str],
        sandbox_mode: str,
        result_file: str,
        agent_home: str,
        resume_thread_id: Optional[str],
    ) -> List[str]:
        cmd = ["codex", "exec"]
        # Exec-level flags belong to `codex exec`, NOT to the `resume`
        # sub-subcommand. In codex 0.139.0, `exec resume [OPTIONS] [SESSION_ID]
        # [PROMPT]` has a NARROWER option set and rejects -C/--sandbox/--json/-o
        # ("error: unexpected argument '-C' found", exit 2 — breaks every
        # turn-2+ continuity call). So they MUST be emitted BEFORE `resume`.
        cmd += [
            "--json",
            "--skip-git-repo-check",
            "-C",
            agent_home,
            "--sandbox",
            sandbox_mode,
            "-o",
            result_file,
        ]
        # Normal mode is `danger-full-access` (no inner sandbox; the Trinity
        # container is the boundary — see _resolve_sandbox_mode), which already
        # permits network access, so no `sandbox_workspace_write.network_access`
        # override is needed. Read-only stays `read-only`. We no longer emit
        # `workspace-write` at all.
        if model:
            cmd += ["-m", model]
        # Continuity: `codex exec <flags> resume <thread_id>` replays a prior
        # thread. Emitted AFTER the exec-level flags above (narrower arg set).
        if resume_thread_id:
            cmd += ["resume", resume_thread_id]
        # End-of-options separator (review I3): the caller appends the prompt as
        # the next (positional) token — for a resume it is resume's PROMPT arg —
        # so a prompt starting with "-"/"--" can never be reparsed as a flag
        # (worst case weakening the sandbox).
        cmd.append("--")
        return cmd

    # -- core subprocess execution (stubbed in unit tests) ---------------------
    async def _execute_codex(
        self,
        *,
        prompt: str,
        model: Optional[str],
        system_prompt: Optional[str],
        resume_thread_id: Optional[str],
        timeout_seconds: int,
        allowed_tools: Optional[List[str]],
        execution_id: Optional[str],
        concurrent_reader: bool = False,
    ) -> Tuple[str, List[ExecutionLogEntry], ExecutionMetadata, List[Dict], Optional[str]]:
        execution_id = execution_id or str(uuid.uuid4())

        api_key = _load_openai_api_key()
        codex_home = _ensure_codex_home()

        # #1971: an API key is required only when there is no SUBSCRIPTION
        # credential. A ChatGPT-plan Codex agent has no API key by design, and
        # the old unconditional gate 503'd it before the CLI was ever invoked —
        # so the only way to run one was to set a placeholder key purely to get
        # past this check.
        if not api_key and not _has_subscription_auth(codex_home):
            raise HTTPException(
                status_code=503,
                detail=(
                    "No Codex credentials in agent container: neither an API key "
                    "(inject OPENAI_API_KEY via credentials) nor a subscription "
                    f"auth.json under CODEX_HOME ({codex_home})."
                ),
            )
        # #2208: the CLI authenticates its websocket transport from auth.json,
        # not from the environment, so the key must be materialised BEFORE the
        # first `codex exec` or every turn 401s. Idempotent, subscription-safe,
        # and never raises — see `_materialize_api_key_auth`.
        if api_key:
            await _materialize_api_key_auth(codex_home, api_key)

        result_file = os.path.join(codex_home, f"{_safe_result_token(execution_id)}-last.txt")
        sandbox_mode = _resolve_sandbox_mode()
        _surface_unmapped_guardrails(allowed_tools)
        composed_prompt = _compose_prompt(system_prompt, prompt)

        cmd = self._build_codex_command(
            model=model,
            sandbox_mode=sandbox_mode,
            result_file=result_file,
            agent_home=_AGENT_HOME,
            resume_thread_id=resume_thread_id,
        )
        cmd.append(composed_prompt)

        env = build_execution_env({
            EXECUTION_TAG_NAME: execution_id,
            "CODEX_HOME": codex_home,
        })
        # #1971: `CODEX_API_KEY` is NO LONGER synthesized. Setting it under both
        # names was meant as harmless defence ("some Codex builds also read
        # CODEX_API_KEY"), but its mere PRESENCE flips the CLI into API-key auth
        # mode and makes it discard a valid subscription `auth.json` — so every
        # subscription agent 401'd against api.openai.com, retried 5x and failed
        # the turn. A defensive duplicate that changes behaviour is not
        # defensive.
        #
        # It is still forwarded when the operator genuinely supplied the key
        # UNDER THAT NAME, which is the only case the original rationale
        # actually covers. An operator who set it directly in the container env
        # keeps it via the `**os.environ` spread above — their explicit choice
        # is not second-guessed here.
        if api_key:
            env["OPENAI_API_KEY"] = api_key
            if _api_key_source_name() == "CODEX_API_KEY":
                env["CODEX_API_KEY"] = api_key

        metadata = ExecutionMetadata()
        metadata.context_window = self.get_context_window(model)
        metadata.execution_id = execution_id
        execution_log: List[ExecutionLogEntry] = []
        raw_messages: List[Dict] = []
        response_parts: List[str] = []
        state = _CodexParseState(
            execution_log=execution_log,
            metadata=metadata,
            response_parts=response_parts,
            model=model,
        )
        stderr_lines: List[str] = []

        registry = get_process_registry()
        logger.info(
            "[Codex] exec sandbox=%s resume=%s model=%s execution_id=%s",
            sandbox_mode, bool(resume_thread_id), model or "(default)", execution_id,
        )

        # stdin=DEVNULL: the prompt is a positional arg, so Codex must not block
        # waiting on stdin. start_new_session=True isolates the process group so
        # cleanup signals only Codex's descendants, never sibling executions.
        process = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            start_new_session=True,
            env=env,
        )
        process_pgid = _capture_pgid(process)
        registry.register(
            execution_id, process, metadata={"type": "codex", "pgid": process_pgid}
        )

        import threading

        def read_stdout() -> None:
            try:
                for line in iter(process.stdout.readline, ""):
                    if not line:
                        break
                    try:
                        sanitized = sanitize_subprocess_line(line)
                        try:
                            event = json.loads(sanitized.strip())
                        except json.JSONDecodeError:
                            continue
                        if isinstance(event, dict):
                            event = sanitize_dict(event)
                            raw_messages.append(event)
                            try:
                                registry.publish_log_entry(execution_id, event)
                            except Exception as pub_err:  # noqa: BLE001
                                logger.debug(
                                    "[Codex] publish_log_entry failed (continuing): %s",
                                    pub_err,
                                )
                            _process_codex_event(event, state)
                    except Exception as line_err:  # noqa: BLE001
                        logger.debug(
                            "[Codex] per-line processing error (continuing): %s",
                            line_err,
                        )
            except Exception as exc:  # noqa: BLE001
                logger.error("[Codex] error reading stdout: %s", exc)

        def read_stderr() -> None:
            try:
                for line in iter(process.stderr.readline, ""):
                    if not line:
                        break
                    stderr_lines.append(line)
            except Exception as exc:  # noqa: BLE001
                logger.error("[Codex] error reading stderr: %s", exc)

        def read_subprocess_output() -> Tuple[str, int]:
            stdout_thread = threading.Thread(target=read_stdout, daemon=True)
            stderr_thread = threading.Thread(target=read_stderr, daemon=True)
            stdout_thread.start()
            stderr_thread.start()
            try:
                return_code = process.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                logger.error(
                    "[Codex] execution %s timed out after %ss — killing group",
                    execution_id, timeout_seconds,
                )
                _terminate_process_group(
                    process, graceful_timeout=5, pgid=process_pgid,
                    execution_tag=execution_id,
                )
                _drain_bounded(
                    process, stdout_thread, stderr_thread, grace=3,
                    pgid=process_pgid, execution_tag=execution_id,
                )
                raise
            _drain_bounded(
                process, stdout_thread, stderr_thread, grace=5,
                pgid=process_pgid, execution_tag=execution_id,
            )
            stderr = "".join(stderr_lines)
            return (sanitize_text(stderr) if stderr else stderr), return_code

        # The lock-serialized chat path uses the bounded single-worker executor;
        # the concurrent /api/task path uses the loop's default executor so
        # parallel task readers don't serialize behind one worker (review I2,
        # parity with Claude's headless path). None → default executor.
        reader_executor = None if concurrent_reader else _executor

        loop = asyncio.get_event_loop()
        try:
            try:
                stderr_output, return_code = await asyncio.wait_for(
                    loop.run_in_executor(reader_executor, read_subprocess_output),
                    timeout=timeout_seconds + 60,
                )
            except asyncio.TimeoutError:
                logger.error(
                    "[Codex] outer timeout on %s — killing group as last resort",
                    execution_id,
                )
                await loop.run_in_executor(
                    None,
                    lambda: _terminate_process_group(
                        process, graceful_timeout=2, pgid=process_pgid,
                        execution_tag=execution_id,
                    ),
                )
                await loop.run_in_executor(None, _safe_close_pipes, process)
                raise HTTPException(
                    status_code=504,
                    detail=f"Codex execution timed out after {timeout_seconds} seconds",
                )
            except subprocess.TimeoutExpired:
                raise HTTPException(
                    status_code=504,
                    detail=f"Codex execution timed out after {timeout_seconds} seconds",
                )

            if return_code != 0:
                status_code, detail = _classify_codex_failure(
                    return_code, stderr_output, metadata
                )
                # NOTE: no metadata.status write here — this path raises
                # HTTPException and the local metadata is discarded, so the
                # backend reads the failure from the HTTP status, not metadata.
                logger.error("[Codex] %s", detail)
                raise HTTPException(status_code=status_code, detail=detail)

            # -o file is authoritative; JSONL parts are the fallback.
            result_text = _read_and_consume_result_file(result_file, codex_home)
            response_text = _finalize_codex_response(result_text, response_parts)
            response_text = sanitize_text(response_text)

            tool_use_count = len([e for e in execution_log if e.type == "tool_use"])
            metadata.tool_count = tool_use_count
            if not response_text:
                response_text = (
                    "(Task completed)" if tool_use_count else "(No response from Codex)"
                )
            metadata.status = "success"
            session_id = _resolve_returned_session_id(metadata)
            logger.info(
                "[Codex] done execution_id=%s cost=$%s tokens=%s/%s tools=%s",
                execution_id, metadata.cost_usd, metadata.input_tokens,
                metadata.output_tokens, metadata.tool_count,
            )
            return response_text, execution_log, metadata, raw_messages, session_id
        finally:
            # Read-then-delete in finally — happy + error path (#1187 decision 5).
            _safe_unlink(result_file, codex_home)
            registry.unregister(execution_id)

    # -- public interface ------------------------------------------------------
    async def execute(
        self,
        prompt: str,
        model: Optional[str] = None,
        continue_session: bool = False,
        stream: bool = False,
        system_prompt: Optional[str] = None,
        execution_id: Optional[str] = None,
    ) -> Tuple[str, List[ExecutionLogEntry], ExecutionMetadata, List[Dict]]:
        if not self.is_available():
            raise HTTPException(
                status_code=503,
                detail="Codex CLI is not available in this container",
            )

        resume_thread_id: Optional[str] = None
        if continue_session and agent_state.session_started and self._chat_thread_id:
            resume_thread_id = self._chat_thread_id
        else:
            agent_state.session_started = True
            self._chat_thread_id = None

        guardrails = _load_guardrails()
        timeout_seconds = int(
            guardrails.get("execution_timeout_sec") or _DEFAULT_EXECUTION_TIMEOUT_SEC
        )

        try:
            response, log, metadata, raw, session_id = await self._execute_codex(
                prompt=prompt,
                model=model,
                system_prompt=system_prompt,
                resume_thread_id=resume_thread_id,
                timeout_seconds=timeout_seconds,
                allowed_tools=None,
                execution_id=execution_id,
                concurrent_reader=False,  # chat is lock-serialized → bounded reader
            )
        except HTTPException:
            raise
        except TimeoutError as exc:
            raise HTTPException(status_code=504, detail=str(exc))
        except (BrokenPipeError, ConnectionResetError) as pipe_err:
            logger.info("[Codex] subprocess pipe closed before completion: %s", pipe_err)
            raise HTTPException(
                status_code=502,
                detail="Agent subprocess closed before the chat could complete",
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("[Codex] execution error: %s", exc)
            raise HTTPException(status_code=500, detail=f"Execution error: {exc}")

        # Track thread id for the next continue_session turn.
        if session_id:
            self._chat_thread_id = session_id
            agent_state.session_started = True

        # Update session rollups (mirrors the Gemini path).
        if metadata.cost_usd:
            agent_state.session_total_cost += metadata.cost_usd
        agent_state.session_total_output_tokens += metadata.output_tokens
        if metadata.input_tokens > agent_state.session_context_tokens:
            agent_state.session_context_tokens = metadata.input_tokens
        agent_state.session_context_window = metadata.context_window
        return response, log, metadata, raw

    async def execute_headless(
        self,
        prompt: str,
        model: Optional[str] = None,
        allowed_tools: Optional[List[str]] = None,
        system_prompt: Optional[str] = None,
        timeout_seconds: int = 900,
        max_turns: Optional[int] = None,
        execution_id: Optional[str] = None,
        resume_session_id: Optional[str] = None,
        persist_session: bool = False,
        images: Optional[List[Dict]] = None,
    ) -> Tuple[str, List[ExecutionLogEntry], ExecutionMetadata, Optional[str]]:
        if not self.is_available():
            raise HTTPException(
                status_code=503,
                detail="Codex CLI is not available in this container",
            )
        if images:
            logger.warning("[Codex] images are not supported in the MVP — ignoring")
        if max_turns is not None:
            logger.info(
                "[Codex] max_turns=%s requested; Codex exec has no turn cap CLI "
                "flag — relying on the %ss wall-clock timeout.",
                max_turns, timeout_seconds,
            )

        try:
            response, log, metadata, raw, session_id = await self._execute_codex(
                prompt=prompt,
                model=model,
                system_prompt=system_prompt,
                resume_thread_id=resume_session_id,
                timeout_seconds=timeout_seconds,
                allowed_tools=allowed_tools,
                execution_id=execution_id,
                concurrent_reader=True,  # /api/task runs concurrently → default reader
            )
        except HTTPException:
            raise
        except TimeoutError as exc:
            raise HTTPException(status_code=504, detail=str(exc))
        except (BrokenPipeError, ConnectionResetError) as pipe_err:
            # 502 (not 503) so the SUB-003 auth-switch isn't tripped by an early
            # child exit — parity with the Claude/Gemini headless paths (#474).
            logger.info("[Codex] subprocess pipe closed before completion: %s", pipe_err)
            raise HTTPException(
                status_code=502,
                detail="Agent subprocess closed before task could complete",
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("[Codex] task execution error: %s", exc)
            raise HTTPException(status_code=500, detail=f"Task execution error: {exc}")

        return response, log, metadata, session_id


# Global Codex runtime instance (singleton, mirrors claude/gemini).
_codex_runtime: Optional[CodexRuntime] = None


def get_codex_runtime() -> CodexRuntime:
    global _codex_runtime
    if _codex_runtime is None:
        _codex_runtime = CodexRuntime()
    return _codex_runtime
