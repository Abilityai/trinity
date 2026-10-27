# /cso --diff — abilityai/trinity#2328 slice S2 (per-task turn cap tightens only)

- Date: 2026-10-26 · Mode: daily, diff · Branch: `AndriiPasternak31/issue-2328-s2` → `dev`
- Changed: `docker/base-image/agent_server/services/headless_executor.py` (cap resolution + 422 wording), `docker/base-image/agent_server/services/runtime_adapter.py` (docstring), `tests/unit/test_2328_turn_cap_tighten_only.py`, `tests/registry.json`, GUARD-003 in `docs/memory/requirements/security.md`, two feature flows + the index row, one learnings fragment.

## Result

No findings. No coverage gaps.

## What was checked

- Attack surface: no new endpoint, WebSocket channel, MCP tool or agent-context ingress. The change narrows an existing input: a per-task `max_turns` sent to `POST /task` by any caller with access to the agent can no longer raise the owner's `max_turns_task` guardrail (ASI03 privilege, LLM10 consumption). Resolution is `min(per-task, guardrail)` for an integer value >= 1; absent, 0, negative or not an integer (bool included) falls back to the guardrail.
- A10 failure direction: the resolver fails toward the guardrail (stated in `_resolve_task_max_turns`'s docstring), including for a non-integer or bool value, which the pull worker can hand over raw from the envelope's JSON (type guard added at review; pinned for `"7"`, `3.5`, `True`); the guardrail read itself is unchanged (`_load_guardrails`: runtime file, else image baseline, else `{}` → `_DEFAULT_MAX_TURNS_TASK`), and the fresh-install shapes (baseline only, nothing readable, corrupt runtime file) are pinned by tests.
- Injection: the value is a pydantic `Optional[int]` on the agent's request model and enters argv as `str(int)` in a list element, never a shell string.
- Auth-classifier input (the #1849 class): the 422 detail is matched by the backend's `is_auth_failure`, whose indicators include bare "401"/"403". This slice's own wording carries neither the guardrail nor the per-task value, pinned by `test_the_per_task_422_wording_adds_no_number_the_auth_classifier_reads` with a number-free CLI message. That does **not** make the 422 classifier-safe: the CLI's error text in the same detail carries the turn count, so at a cap of 401 or 403 the push classifier (`classify_switch_failure`, and `is_auth_failure` in the `except` branch of `task_execution_service`) reads it as auth and switches subscription. See Residual.
- Secrets (Phase 2, diff range): gitleaks over the branch commits, no leaks; known-prefix grep over added lines, no hits.
- Agent-shipped content (Phase 8 grep): invisible/bidi Unicode over every changed file, no hits.
- Enterprise-disclosure guard pattern over added public-doc lines: no hits.
- Phases 3-6, 8 (beyond the grep), 10, 11: no diff overlap (no dependency, workflow, Dockerfile, compose, integration, template or skill change).

## Residual (not a finding)

- Rollout: the rule reaches an agent only after a base-image rebuild and a cold start; until then an agent keeps "caller value wins". Today only pull-pilot agents receive a per-task value at all (the push path drops it until slice S1), so the exposure in that window is the pre-existing one.
- Turn-limit 422 at a cap of 401/403 reads as an auth failure on push (SUB-003 switch plus a retry). Pre-existing on the guardrail-bound path; with a per-task value it is reachable only once the push path forwards one (slice S1). Not reachable on pull, whose switch decision is an error-code allowlist and which reports a 422 as `max_turns`. Pinned as current behaviour by `test_known_exposure_a_turn_limit_422_at_401_or_403_reads_as_auth_on_push` (runs the real `classify_switch_failure`). Fix tracked as a backend follow-up: a turn-limit 422 is never a credential failure.
- Codex and Gemini have no turn-cap flag and still log the value only; the wall-clock timeout bounds them, as before.
