"""Evaluation reads: what each kind of reader may see of a row (trinity-enterprise#781).

Pure: no SQL and no HTTP (Invariant #1). `routers/evaluations.py` resolves who
is reading (person or machine, operator of the row's agent or not) and calls
`redact_evaluator` for every reader that is not a human operator.

* `redact_evaluator` — the grader-identity ALLOWLIST. A known non-person
  evaluator name passes through; every other `kind:rest` reads as its kind when
  that kind is an identity-free token (`[a-z0-9_-]+`); a value with no kind, or
  with a kind that is not such a token, reads `unknown`. A fourth
  person-writing kind is therefore anonymised on the day it appears, rather
  than on the day someone adds its prefix to a list.

Stored rows are never rewritten: the full evaluator stays in the database and
is still served to a human owner or admin of the agent.

This is the dev-side slice of the evaluation integration branch's module of the
same name. The function below is copied from it verbatim, with ONE deliberate
addition marked "DEV-ONLY GUARD": a kind is shown only when it is an
identity-free token, so a stored value with an address before the colon reads
`unknown` rather than leaking the address as its "kind". The two copies
therefore agree on every input except a malformed kind, where this one reads
`unknown` and the integration copy returns the kind as stored. Whoever merges
that branch into dev must keep the guard (or carry it into that branch's copy).
That branch takes the two Tier-0 names from the Tier-0 writers in
`db/evaluations.py`, which dev does not have yet, so they are spelled out here.
"""
from __future__ import annotations

import re
from typing import Any

# Known non-person evaluator names: shown as stored. Everything else is
# reduced to its kind — a fourth kind is anonymised on the day it appears.
NON_PERSON_EVALUATORS = frozenset({"platform:tier0", "platform:tier0:pair"})
TIER1_EVALUATOR_PREFIX = "platform:tier1:"
TIER1_KIND = "platform:tier1"
# DEV-ONLY GUARD (ent#781 security review) — a kind is shown only when it is an
# identity-free token. Not on the integration branch yet; a merge must keep it.
_IDENTITY_FREE_KIND = re.compile(r"[a-z0-9_-]+")


def redact_evaluator(evaluator: Any) -> str:
    """The grader as a non-operator may see it (ent#781).

    An ALLOWLIST: a known non-person name passes through; a Tier-1 row reads
    `platform:tier1` (the model name is withheld too, TD-8); any other
    `kind:rest` reads `kind`; anything without a kind reads `unknown`.
    """
    name = evaluator if isinstance(evaluator, str) else ""
    if name in NON_PERSON_EVALUATORS:
        return name
    if name.startswith(TIER1_EVALUATOR_PREFIX):
        return TIER1_KIND
    kind, sep, _ = name.partition(":")
    # --- DEV-ONLY GUARD (ent#781 security review): begin -------------------------
    # The kind is shown verbatim, so it must not itself be the person: a stored
    # `grader@example.com:admin` would otherwise read `grader@example.com`. Only
    # a lowercase token (letters, digits, `_`, `-`) is a kind; anything else —
    # an address, whitespace, uppercase, a dotted handle — reads `unknown`.
    if sep and kind and not _IDENTITY_FREE_KIND.fullmatch(kind):
        return "unknown"
    # --- DEV-ONLY GUARD: end -------------------------------------------------------
    if sep and kind:
        return kind
    return "unknown"
