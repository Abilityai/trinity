"""
G-04 — No raw credentials in backlog metadata (CANARY-001 / Issue #411; #1077).

`backlog_metadata` (the drain-replay identity/request blob on a queued
`schedule_executions` row) must never contain a raw credential value. It is
persisted plaintext in SQLite/Postgres, surfaced through backlog introspection,
and — the reason this check exists — read by E-04, whose violations persist to
`canary_violations`. A secret that leaks into `backlog_metadata` therefore leaks
into durable, operator-visible state. G-04 is the tripwire that fires the day a
producer path serializes a credential into the backlog blob instead of an opaque
identity token.

## Rides E-04's bytes — near-zero marginal cost

G-04 scans the exact `backlog_metadata` strings E-04 already collects (queued
rows only, via `_collect_executions` → `AgentSnapshot.queued_meta`). No new
source, no new query — one regex sweep per queued row per cycle.

## SECURITY — report the pattern NAME only

The whole point is to catch a leaked secret, so the check must not *re-leak* it.
`observed_state` / `signal_query` report **only** the matched pattern's name
(e.g. `github_pat`), the `execution_id`, and `agent_name` — **never** the matched
secret, the bytes around it, or the raw `backlog_metadata`. One violation per
offending row (we stop at the first matching pattern so the count of matches
isn't disclosed either).

## Why a canary and not a writer unit test

Like E-04, G-04 catches **all** producers that enqueue a backlog row — including
the standalone scheduler's raw-SQL writers (#1082) a backend test never
exercises. That cross-path coverage is why it is an invariant.

Tier A, severity **critical** — a credential in durable, operator-visible state
is a security incident, higher-severity than E-04's wedged-backlog integrity
defect even though it rides the same collected field.
"""

import re
from typing import List

from ..snapshot import Snapshot, ViolationReport


INVARIANT_ID = "G-04"
TIER = "A"
SEVERITY = "critical"

# Credential prefix patterns, kept in ONE module constant. Each entry is
# (pattern_name, compiled_regex). Patterns use a leading `\b` word boundary and
# require at least one key-like char after the prefix so common substrings
# (e.g. "task-" containing "sk-") don't false-fire; only the pattern NAME is
# ever reported (see SECURITY in the module docstring).
_SECRET_PATTERNS = [
    # OpenAI / Anthropic-style secret keys ("sk-...", "sk-ant-...", "sk-proj-...").
    ("openai_style_secret_key", re.compile(r"\bsk-[A-Za-z0-9]")),
    # GitHub tokens: classic PAT (ghp_), OAuth (gho_), server (ghs_), user (ghu_),
    # fine-grained PAT (github_pat_).
    ("github_pat", re.compile(r"\bghp_[A-Za-z0-9]")),
    ("github_oauth_token", re.compile(r"\bgho_[A-Za-z0-9]")),
    ("github_server_token", re.compile(r"\bghs_[A-Za-z0-9]")),
    ("github_user_token", re.compile(r"\bghu_[A-Za-z0-9]")),
    ("github_fine_grained_pat", re.compile(r"\bgithub_pat_[A-Za-z0-9]")),
    # Slack bot / user OAuth tokens.
    ("slack_bot_token", re.compile(r"\bxoxb-[A-Za-z0-9]")),
    ("slack_user_token", re.compile(r"\bxoxp-[A-Za-z0-9]")),
    # AWS access key id (AKIA + 16 uppercase alnum).
    ("aws_access_key_id", re.compile(r"\bAKIA[A-Z0-9]{4}")),
    # Google API key (AIza + 35 url-safe chars).
    ("google_api_key", re.compile(r"\bAIza[A-Za-z0-9_\-]")),
    # Stripe live secret key.
    ("stripe_live_secret_key", re.compile(r"\bsk_live_[A-Za-z0-9]")),
]


def check(snapshot: Snapshot) -> List[ViolationReport]:
    """One violation per queued row whose backlog_metadata matches a secret pattern."""
    violations: List[ViolationReport] = []

    for agent in snapshot.agents:
        for eid in agent.queued_exec_ids:
            meta = agent.queued_meta.get(eid)
            if meta is None:
                # Older-image snapshot — no metadata observed; skip (fail-open).
                continue
            raw = meta.get("backlog_metadata")
            # E-04 owns the NULL case; a non-str (shouldn't happen on a TEXT
            # column) has nothing to scan.
            if not isinstance(raw, str):
                continue

            for pattern_name, pattern in _SECRET_PATTERNS:
                if pattern.search(raw):
                    violations.append(
                        ViolationReport(
                            invariant_id=INVARIANT_ID,
                            tier=TIER,
                            severity=SEVERITY,
                            observed_state={
                                "agent_name": agent.name,
                                "execution_id": eid,
                                # SECURITY: pattern NAME only — never the secret,
                                # surrounding bytes, or raw backlog_metadata.
                                "matched_pattern": pattern_name,
                                "snapshot_time": snapshot.snapshot_time,
                            },
                            signal_query=(
                                f"schedule_executions queued row {eid} "
                                f"(agent={agent.name}) backlog_metadata matched "
                                f"credential pattern '{pattern_name}'"
                            ),
                        )
                    )
                    # One violation per row; stop at the first match so the
                    # number of matches isn't disclosed either.
                    break

    return violations
