# /cso --diff — feature/978-narrowed-read-marker (abilityai/trinity-enterprise#978, items 1 and 6)

Date 2026-11-24 · cso 1.2.3 · daily gate · diff against `dev` at `65a2fa586` · not a full run.

Scope: the narrowed-read marker (`X-Trinity-Read-Narrowed`), the scope-first reorder of the two by-id execution reads, the second variant of the restricted-turn refusal text, the MCP `narrowed` field, and the route census's gate rules for the `self`, `internal`, `public` and `resource` tiers. No endpoint is added.

## Findings

Nothing open. Three low findings came out of the pre-commit independent review and are fixed on the branch.

| # | Sev | Conf | Category | Finding | State |
|---|---|---|---|---|---|
| 1 | LOW | 9/10 | enumeration-via-marker | The marker code varied with the turn id a caller sent, so it told a turn whether an id was a share turn of its agent (`src/backend/dependencies.py`) | fixed on branch |
| 2 | LOW | 8/10 | header-integrity | A value a handler set for the marker header reached the client when no code was noted (`src/backend/utils/read_narrowing.py`) | fixed on branch |
| 3 | LOW | 8/10 | guard-rule-gap | The new `self` census rule accepted `assert_own_agent` alone, which is a no-op for a person (`tests/unit/test_ent926_agent_route_census.py`) | fixed on branch |

Each has a regression test named in the JSON beside this file.

## Checked and clean

- **Enumeration (Invariant #8):** a run that exists and one that does not answer the same status, body, headers and query count on detail and `/log`; the marker is noted where the view is decided, never where a row is read.
- **Disclosure:** the header carries one of three codes and nothing else: no person, no run id, no count. Only an agent key is marked.
- **Injection:** the code is shape-checked on both sides (`^[a-z_]{1,64}$`) before it is sent or shown; the message an agent reads is fixed platform text.
- **Who is refused:** the refusal-text picker runs after the refusal is decided, only chooses words, and keeps the original text on any error.
- **Failure direction:** stated for each new guard. An unreadable ledger still narrows to nothing and still refuses.
- **Secrets:** no known-prefix hit in added lines; no `.env` tracked.
- **Dependencies, CI, Docker, agent-shipped files:** no file of those kinds changed.
- **Auth guards:** 1,084 tests green (admin gate, human-only routes, auth wiring, enumeration uniformity, source-agent attribution, agent-auth header, settings sink, the vendored-copy parities, the route census, the new marker file).
- **Hidden characters:** none in any changed file.
- **Enterprise-docs guard:** no hit on added doc lines or on the `main.py` seam file.
- **Logging:** one info line per marked response (code, method, path); no query string, no credential.

## Coverage gaps

- Guards that import the assembled app (the runtime route census, the ops-write sweep) could not run on the audit host: a Python package is missing there, on the merge-base too. CI runs them, and CI is the first real import of `main.py` with the new middleware.
- The MCP transport-level visibility tests were cancelled on the audit host, identically on the merge-base. The 2,048-character description cap was checked by measuring the six changed descriptions (longest 1,086).
- An optional module's reader of the narrowing seam was not read (the private submodule is not mounted). It inherits the header through the shared seam.
- No dependency scanner ran, because no dependency changed.
