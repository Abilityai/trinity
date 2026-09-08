# CSO diff audit — abilityai/trinity-enterprise#536 (`feature/536-rich-canvas-vocabulary`)

**Date**: 2026-09-07 · **Mode**: `--diff` (daily gate 8/10) · **Base**: merge-base `88056c3d` on `dev` · **Diff**: 27 files changed (25 modified/added in the feature set plus 2 pre-existing submodule pointer drifts that are NOT part of the change), ~1.8k insertions / ~0.6k deletions (working tree, pre-commit)

## Verdict
**Two findings at the gate, both closed inside the diff before commit.** One is a deliberate posture change whose first mitigation was too narrow (caught by the independent verifier and widened); the other is a pre-existing HIGH the branch resolves. No open findings.

## Attack surface introduced by the diff
- **One new route**: `PATCH /api/agents/{name}/canvas/{canvas_id}` (`routers/canvas.py:140`) — `AuthorizedAgent` + the same `_gate_write` as PUT (agent-scoped self-gate, per-agent rate bucket, Content-Length hint, exact byte cap in the service). `routers/canvas.py` now carries its `# mcp:` header. Panel endpoint `GET …/voice/{sid}/panel` keeps `get_authorized_agent` + `assert_owns_or_admin` and now returns the canvas row instead of an in-memory copy.
- **One new MCP tool**: `patch_canvas` — agent-scoped key only (same `getAgentName` refusal as `set_canvas`); `set_canvas`/`get_canvas` default to canvas `main`.
- **Two new renderer inputs** reaching `v-html`, both through the app's single DOMPurify instance: Mermaid SVG (`CanvasDiagram.vue:22`, `securityLevel:'strict'` + `htmlLabels:false`, rendered in-parent because the prod CSP blocks srcdoc iframes) and — unchanged — the `html` block (`CanvasBlock.vue:32`). New `image` kind binds only an `https:` URL, a `data:image/(png|jpeg|gif|webp);base64` URI ≤ 64 KiB, or an object URL minted from the authenticated `/files/preview` fetch of a workspace-confined path (`canvas_blocks.classify_image_src` — the #979 gate, now shared by the REST and voice writers).
- **CSP change**: `img-src 'self' data: blob:` → `'self' data: blob: https:` in both mirrors (`vite.config.js:38`, `security-headers.conf:31`). See F1.
- **Voice write path**: panel tools now write the agent's default canvas through `canvas_service.write_canvas` with a REQUIRED audience; a canvas stored wider than `VoiceSession.canvas_audience` (default `operator`) is refused with a spoken reason — an operator's call cannot land on a `roster` board.
- Agent-authored chart strings (`label`, `color`) normalised in `canvasUtils.js` (`safeLabel` plain text ≤ 80, `safeColor` hex-or-palette) before any component; block ids charset-validated; duplicate ids, unknown patch ids, over-cap diagrams, refused image sources all named 400s.

## Findings
| # | Sev | Conf | Status | Category | Finding | Phase | File:Line |
|---|-----|------|--------|----------|---------|-------|-----------|
| F1 | MEDIUM | 9/10 | VERIFIED → MITIGATED IN DIFF | Information disclosure (tracking pixel) | `img-src https:` is page-global; the first mitigation (`no-referrer` on the canvas `<img>` only) left `html`/`markdown` blocks and every other `renderMarkdown` surface able to load an agent-chosen https image with the deployment origin as Referer; the classifier also accepted `http://` while its refusal text said https-only | P7/P9-A05 | `src/frontend/vite.config.js:38`, `security-headers.conf:31`, `utils/markdown.js:55,73`, `services/canvas_blocks.py:61,88` |
| F2 | HIGH (pre-existing since ent#438) | 9/10 | VERIFIED → RESOLVED IN DIFF | XSS / HTML injection | `TrendLineChart.vue` built its hover tooltip by string concatenation of series `label`/`color` into `innerHTML`; `canvasUtils.chartSeries` passed agent-authored values verbatim, and the canvas mounted it on the operator tab AND the roster-visible Workspace | P7/P9-A03 | `origin/dev:src/frontend/src/components/TrendLineChart.vue:67-76`, `origin/dev:canvasUtils.js:126-128` |

### F1 — exploit scenario and closure
1. A prompt-injected (or simply careless) agent writes a `markdown`/`html` block containing `<img src="https://attacker.example/p.gif">` or `<div style="background:url(https://attacker.example/p.gif)">`, or an `image` block with `http://…`.
2. Under the widened CSP the browser fetches it on every render of the canvas — on Agent Detail for the operator, and on the Workspace for every rostered client when the canvas is `roster`.
3. The attacker's host learns viewer IP, user agent, view timing/count, and (via `Referrer-Policy: strict-origin-when-cross-origin`) the deployment origin. The agent itself gains no new exfiltration channel — it already has outbound network — the new information is about the **viewer**.

Closure (in this diff): `utils/sanitizeHooks.js::hardenMediaAttributes` runs inside the one DOMPurify `afterSanitizeAttributes` hook, so **every** sanitised `<img>` app-wide gets `referrerpolicy="no-referrer"` + `loading="lazy"`, and any inline `style` referencing `url(`/`image-set(`/`@import` is removed (a CSS background is the same pixel through another attribute). `classify_image_src` and `canvasUtils.imageSource` admit `https:` only, so the validator and its refusal text agree and `http://` is refused at write. Residual (accepted, recorded in the flow doc): an https host of the agent's choosing still sees the viewer's IP and view time. Bounded to https, no referrer, no cookies (cross-origin image request), and no data the agent could not already send about itself. Follow-up option, not filed: a Settings-surfaced image-host allowlist.

### F2 — exploit scenario and closure
1. Pre-branch, `set_canvas` with `{"kind":"chart","payload":{"labels":["2026-09-01"],"series":[{"label":"<img src=x onerror=alert(1)>","data":[1]}]}}`.
2. Any viewer hovering the chart triggers `tip.innerHTML = html` with the label interpolated raw. Under the prod CSP (`script-src 'self'`, no `unsafe-inline`) the handler is blocked, so prod impact was HTML/CSS injection inside the tooltip; under the Vite dev CSP (`'unsafe-inline'`) it executes. The `color` value was interpolated into a `style` attribute the same way.
3. Closure: the tooltip is rebuilt with `replaceChildren`/`createElement`/`textContent`; `swatch.style.background` is a CSSOM assignment fed by `safeColor` (hex triplet or palette); labels pass `safeLabel`. `StackedBarChart.vue` and the new `CanvasPieChart.vue` bind agent data only via interpolation, `:style="{backgroundColor}"`, `:fill` and SVG `<title>{{ }}</title>`, all from the normalised `chartModel`. Pinned by `canvasUtils.spec.js` ("the trend tooltip is built with DOM APIs, never innerHTML over series data").

## Verification performed
- Secrets: known-prefix scan (AWS, OpenAI/Anthropic, GitHub token/PAT, Slack) over the merge-base diff and every new file — 0 matches. No `.env`, workflow, Dockerfile or compose change.
- Enterprise-docs guard pattern over the changed public docs — the only `enterprise` tokens are `trinity-enterprise#N` issue refs and the `/api/enterprise/client-portal/` route (excluded by the guard's negative lookahead).
- Independent fresh-context verifier (one Agent, refute-first) on F1 and F2: F1 mitigation list REFUTED in part (http accepted; page-global gap) → widened as above; F2 VERIFIED pre-existing and VERIFIED closed. Verifier's nuance on F2's prod impact adopted verbatim.
- Auth boundaries: `test_ent438_agent_canvas.py` (self-gate on PUT/PATCH/DELETE, reads not self-gated, audience allowlist, portal narrowing in the query, behavioural never-widens pin), `test_ent536_canvas_vocabulary.py` (gated PATCH route, one write path, `audience_within`, image gate accept/refuse matrix incl. http/svg/data-over-cap/traversal/sibling-prefix/control chars/protocol-relative, patch merge refusals), `test_voice_tools.py` (voice write refused onto a wider canvas; roster session never widens an operator canvas; failed write reported not claimed), `test_voice_auth.py` (panel ownership gate), `test_1400_csp_blob_preview.py` (blob: still in every directive) — green. Frontend: `canvasUtils.spec.js` + `sanitizeHooks.spec.js` (hook registered inside the one DOMPurify hook; CSP mirrors agree) — full suite green. MCP: `canvas.test.ts` — suite green, `tsc` clean.
- Live: seeded canvas on the rebuilt local backend; every refusal returned its named 400; Playwright console showed the CSP block before the change and a loaded https image after; two mermaid diagrams render separately after the render queue fix.
- Supply chain / CI / Docker: no changes (mermaid was already a dependency; it is now loaded lazily).

## Observations (below the gate)
| # | Sev | Conf | Observation |
|---|-----|------|-------------|
| O1 | INFO | 4/10 | `patch_canvas` is read-modify-write across two transactions (last-writer-wins); the db layer already documents one writer per canvas and the tool description states LWW. Hard exclusion #6 (no concrete exploit). |
| O2 | INFO | 3/10 | `image` blocks with a workspace path fetch through `/files/preview`, which requires the agent to be running; an external portal client gets an honest placeholder (the route is JWT-gated). No disclosure. |
| O3 | INFO | 3/10 | Voice `_execute_panel_tool` now does one canvas read + one write synchronously per tool call on the event loop (was one write). Bounded by the model's tool-call rate; excluded as DoS. |

## STRIDE (diff scope)
- **Backend canvas route**: spoofing — none new (same principals); tampering — PATCH cannot append or touch title/audience, unknown ids refused; repudiation — provenance stamp `updated_by_execution_id` restamped on patch; disclosure — audience unchanged, voice write cannot widen; DoS — excluded; elevation — none (`AuthorizedAgent` + self-gate on every write).
- **Frontend renderer**: F1/F2 above; every agent string reaches the DOM through DOMPurify, interpolation, `textContent`, or a validated hex in a CSSOM property.
- **Voice**: the session's `canvas_audience` bounds the write; refusal is spoken, not silent.

## Data classification (diff)
- Canvas blocks — CONFIDENTIAL (agent output; `roster` canvases are client-visible by the agent's explicit choice). Stored as before in `agent_canvases.blocks`; no new column.
- Image sources — INTERNAL (an https URL or a workspace path); inline `data:` rasters ≤ 64 KiB are stored in the row.

_Trend_: prior diff audit `cso-diff-2026-09-06-ent475-rail-rehome` (0 findings). This audit: 2 findings, both closed in-diff; 0 open. Not comparable surfaces.
