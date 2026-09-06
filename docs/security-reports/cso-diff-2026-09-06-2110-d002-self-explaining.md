# CSO diff audit — abilityai/trinity#2110 (`vybe/issue-2110`)

**Date**: 2026-09-06 · **Mode**: `--diff` (daily gate 8/10) · **Base**: merge-base `a5ac3cf3` on `dev` · **Diff**: 13 files changed, 688 insertions(+), 30 deletions(-) at `b02e3c39` (the four implementation commits; review-stage commits appended afterwards)

## Verdict
**No findings at the daily gate.** Four INFO observations recorded below. Backend-strings + tests + docs change: no route, model, migration, MCP tool, Docker, compose, CI, frontend source or dependency change.

## Attack surface introduced by the diff
- **No new server surface.** `GET /api/agents/{name}/compatibility` (`AuthorizedAgentByName`), MCP `get_agent_compatibility_report` (agent-scoped keys pass `getPermittedAgents`) and the `agent_compatibility_results` upsert are unchanged; only the *content* of two existing check messages changed.
- **The one data flow that matters**: agent-authored `dashboard.yaml` (untrusted; the agent controls it; the collector caps 256 KiB/file; `load_hardened_yaml` with `AliasPolicy.REJECT`) → `_dashboard` (keeps dict widgets only) → `c_d002`/`c_d003`/`c_d004`/`c_d005` → `message` / `detail` → `_check_dict` copies `message` verbatim (`services/compatibility/__init__.py:39-60`) → three sinks:
  1. **UI** — `CompatibilityPanel.vue:196` `{{ c.message }}` (Vue text interpolation, auto-escaped). The only `v-html` in the file is `renderMarkdown(c.explanation)` at `:204`, AI output through the existing markdown/DOMPurify path — untouched.
  2. **JSON** — `CompatibilityReport(**report)` (`message: str`, `detail: Optional[Dict]`), proxied whole by the MCP server.
  3. **DB** — `db/compatibility.py:57` `json.dumps(checks, default=str)` → `insert(t).values(**payload)` / `update(...).values(**payload)` (SQLAlchemy Core, bound parameters; no string SQL).
- **Bounds at the origin, per value**: `_clip` = `str()` → 512-char pre-slice → drop every `not ch.isprintable()` (covers ESC, U+202E/U+200B, newlines) → whitespace-collapse → 40 chars + `…` → `(blank)` placeholder; the clipped form is the counting key, so `message` and `detail` carry the same bounded string. Message names ≤ 5 types (`_named`), `detail.types` ≤ 25. `{t!r}` (Python `repr`) is a second belt: anything non-printable that survived would be rendered as an escape sequence, not emitted raw.
- **No format-string surface**: f-strings interpolate data values; no `str.format`/`%` is applied to an attacker-controlled template. D-003's interpolated pair is `(t, m)` where `t` is guaranteed to be one of the seven `req` keys (otherwise `req.get(t, [])` yields nothing) and `m` is a constant field name.
- **Net reduction, not addition**: before this change `detail.types` carried the raw, uncapped, unclipped set of offending type strings into the same three sinks; now the same names arrive clipped and capped in both `message` and `detail`.

## Verification performed
- Secrets: known-prefix scan (AWS, OpenAI/Anthropic, GitHub token and PAT, Slack, PEM) over `git log -p a5ac3cf3..HEAD` — 0 matches. PII/internal-URL scan over added lines — 0 matches. No `.env`, workflow, compose or config file touched.
- Enterprise-docs guard: `.github/workflows/enterprise-docs-guard.yml`'s exact PCRE pattern run over the changed docs and over the workflow's full CI scope — 0 hits.
- Private consumers of the changed strings: the enterprise submodule at the recorded pointer (`90f2f2c`, read-only mount in the main clone) grepped for word-bounded `D-002`, `D-003`, `unsupported dashboard`, `missing required fields`, `checks_json`, and any import of `services.compatibility`/`static_checks` — 0 consumers (the single `checks_json` hit is the evaluations table in a planning doc). Both message **prefixes** are byte-identical to `origin/dev` regardless.
- `default=str` scope: the only change in `db/compatibility.py`; the only `json.dumps` writer of `agent_compatibility_results.checks_json` (`db/evaluations.py` owns a same-named column on another table, untouched). The objects it can see are YAML natives (`datetime.date`/`datetime`) — `dashboard.yaml` is not a secret-bearing file (the collector ships `.env`/`.mcp.json` existence-only), so stringifying a value from it discloses nothing new.
- Auth / enumeration: no handler changed; `tests/unit/test_186_enumeration_uniformity.py`-class behaviour is untouched by construction.
- Tests: `tests/unit/test_compatibility_checks.py` 111 passed (+13, incl. the hostile-name clip test with a real newline, ESC and U+202E inside a YAML double-quoted scalar, the YAML-`date` label/color tests, the persistence round-trip and the sink-belt test); `tests/unit/test_2110_widget_type_parity.py` 16 passed; three `pytest-randomly` seeds 127 passed. Mutation proof: appending `"chart"` to `_WIDGET_TYPES` → 8 red / 8 green.
- Supply chain / CI / Docker / frontend source: no changes (`git diff --quiet origin/dev...HEAD -- src/frontend` is clean, so every frontend ratchet equals the merge-base's).

## Observations (below the gate)
| # | Sev | Conf | Observation |
|---|-----|------|-------------|
| O1 | INFO | 4/10 | `default=str` at the sink can mask a *future* producer that puts a non-JSON-native object into `detail` (it would persist as its `str()` rather than fail). Accepted in the plan (TASTE T3): producers are bounded at the origin and the belt exists so one check can never drop the whole report's persistence — which is what happened at HEAD with a `date` label (and `build_report`'s best-effort `except Exception` made that a silent no-persist, not a 500). |
| O2 | INFO | 3/10 | Agent-authored widget-type names now appear in `message`, which an MCP client (an operator's LLM) reads as a tool result. Bounded to ≤ 5 names × 40 printable chars; the same names were previously delivered *unbounded* via `detail.types`, so the injection surface shrank. Agent-authored file content reaching the report is the established design of this surface. |
| O3 | INFO | 3/10 | `_clip` collapses whitespace, so a quoted `type: "metric "` (raw membership fails, correctly — the agent server strips it too) renders as `'metric' ×1` while `metric` is in the supported list. Confusing in a corner case, not exploitable; a deliberate design choice (clipped form = key). |
| O4 | INFO | 2/10 | The parity guard's `_literal_assign` rejects rebinding, `AugAssign` and nine mutator calls on the allowlist name, but not `valid_types[:] = …` / `valid_types[0] = …` or mutation through an aliased parameter; a starred element raises a bare `ValueError` (still a loud red). Test hygiene, not a security property. |

## STRIDE (diff scope)
- **Backend**: spoofing — none (no new principal or token); tampering — the only writer is the existing parameterized upsert; repudiation — n/a (no new mutation); disclosure — O2 (net reduction; per-agent access control unchanged); DoS — excluded by rule, and bounded anyway (`_clip` pre-slice, O(widgets) counting, collector caps upstream); elevation — none.

## Data classification (diff)
- `agent_compatibility_results.checks_json` — INTERNAL/CONFIDENTIAL as before (per-agent workspace findings; type names, colours, labels — never file content of secret-bearing files).
- D-002/D-003 `message` — same class; now carries bounded agent-authored identifiers that `detail` already carried.

_Trend_: third diff audit on this branch line's day (prior: ent#475 rail rehome, 2026-09-06; ent#437 telemetry, 2026-09-03); not comparable (different surface). No persistent findings.
