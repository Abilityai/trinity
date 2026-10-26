# /cso --diff — abilityai/trinity#2328 slice S2 (per-task turn cap tightens only)

- Date: 2026-10-26 · Mode: daily, diff · Branch: `AndriiPasternak31/issue-2328-s2` → `dev`
- Changed: `docker/base-image/agent_server/services/headless_executor.py` (cap resolution + 422 wording), `docker/base-image/agent_server/services/runtime_adapter.py` (docstring), `tests/unit/test_2328_turn_cap_tighten_only.py`, `tests/registry.json`, GUARD-003 in `docs/memory/requirements/security.md`, two feature flows + the index row, one learnings fragment.

## Result

No findings. No coverage gaps.

## What was checked

- Attack surface: no new endpoint, WebSocket channel, MCP tool or agent-context ingress. The change narrows an existing input: a per-task `max_turns` sent to `POST /task` by any caller with access to the agent can no longer raise the owner's `max_turns_task` guardrail (ASI03 privilege, LLM10 consumption). Resolution is `min(per-task, guardrail)` for a value >= 1; absent, 0 or negative falls back to the guardrail.
- A10 failure direction: the resolver fails toward the guardrail (stated in `_resolve_task_max_turns`'s docstring); the guardrail read itself is unchanged (`_load_guardrails`: runtime file, else image baseline, else `{}` → `_DEFAULT_MAX_TURNS_TASK`), and the fresh-install shapes (baseline only, nothing readable, corrupt runtime file) are pinned by tests.
- Injection: the value is a pydantic `Optional[int]` on the agent's request model and enters argv as `str(int)` in a list element, never a shell string.
- Auth-classifier input (the #1849 class): the new per-task 422 text is matched by the backend's `is_auth_failure`, whose indicators include bare "401"/"403". The guardrail's owner-set value is kept out of the wording (fixed in this slice at review, before this scan) and pinned by `test_the_per_task_422_is_not_read_as_an_auth_failure[50|401|403]`, which runs the real backend predicate.
- Secrets (Phase 2, diff range): gitleaks over the branch commits, no leaks; known-prefix grep over added lines, no hits.
- Agent-shipped content (Phase 8 grep): invisible/bidi Unicode over every changed file, no hits.
- Enterprise-disclosure guard pattern over added public-doc lines: no hits.
- Phases 3-6, 8 (beyond the grep), 10, 11: no diff overlap (no dependency, workflow, Dockerfile, compose, integration, template or skill change).

## Residual (not a finding)

- Rollout: the rule reaches an agent only after a base-image rebuild and a cold start; until then an agent keeps "caller value wins". Today only pull-pilot agents receive a per-task value at all (the push path drops it until slice S1), so the exposure in that window is the pre-existing one.
- Codex and Gemini have no turn-cap flag and still log the value only; the wall-clock timeout bounds them, as before.
