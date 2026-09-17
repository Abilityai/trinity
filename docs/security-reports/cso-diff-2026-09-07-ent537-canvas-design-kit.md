# /cso --diff — feature/537-canvas-design-kit (2026-09-07)

**Scope**: the 24 source files this branch changes (canvas design kit, canvas-mode sanitiser, starter layouts, prompt/MCP teaching). Daily mode, 8/10 confidence gate, one fresh-context verifier per surviving finding.

## Attack surface delta
- No new endpoints, no dependency, workflow or Docker changes. `PUT …/canvas/{id}` gains `template` (Pydantic `Literal`, named 400 on the service path); blocks gain `slot` (regex on the model, re-checked in `validate_blocks` for the Pydantic-less voice path).
- New render paths: `sanitizeCanvasHtml` / `renderCanvasMarkdown` (one DOMPurify instance, per-call `canvasKit` config flag read from the hook's third argument) and `sanitizeSvg` — the ONE named entry point that keeps `<style>`, for mermaid's own id-scoped stylesheet.

## Hardening shipped on this branch
1. **`<style>` element forbidden on every markdown/html path.** DOMPurify's default tag list admits it and a body `<style>` is document-global, so before this branch an agent message, report or canvas block could restyle the whole page — including a customer's Workspace on a `roster` canvas. Found by the independent engineering review; fixed app-wide; pinned by spec.
2. **Canvas-mode allowlist**: exact `KIT_CLASSES` membership on `class`, `width`/`max-width` only on `style` with bounded values (no parentheses, `!`, quotes or backslashes reachable), `id` dropped.
3. **Kit root overflow containment** (finding 1, below).
4. **Mermaid `secure` keys** (finding 2 residual, below).

## Findings
| # | Sev | Conf | Status | Category | Finding | File |
|---|---|---|---|---|---|---|
| 1 | LOW | 9/10 | VERIFIED → fixed | Layout containment | An admitted `width: 9999px` could widen a customer's Workspace page | `CanvasKit.vue` |
| 2 | INFO | 9/10 | REFUTED (+ hardened) | CSS injection | `themeCSS` init directive reaching the page through the SVG `<style>` | `CanvasDiagram.vue` |

**1 — exploit**: an html block with `style="width: 9999px"` on a roster canvas → horizontal scroll on every rostered customer's page. **Fix**: `.canvas-kit { overflow-x: auto }` (principle 7), pinned.

**2 — verification**: mermaid 11.16.1 honours `%%{init}%%` under `strict`, but `compileCSS` (`mermaid.core.mjs:1145-1203`) namespaces every rule under `#<diagram-id>`, so `body{display:none}` matches nothing. Residual: `@keyframes` stay global (could redefine an app animation name — visual only). **Hardening**: `MERMAID_CONFIG.secure = ['themeCSS','fontFamily','altFontFamily']` deletes the keys from any directive; pinned.

## Excluded / not findings
- Inline `<svg>` inside a canvas html block loses `class`/non-width `style` in canvas mode — behaviour change, not a vulnerability; `image`/`diagram` are the supported routes.
- Tailwind utilities remain reachable from chat/report markdown — pre-existing, deferred to a follow-up issue (an app-wide class allowlist needs the decorator's own classes admitted).
- Phase 8: the new `canvas` library skill (trinity-pm clone) scanned — no network calls, no credential access, no injection phrases.

## Totals
CRITICAL 0 · HIGH 0 · MEDIUM 0 · LOW 1 (fixed) · INFO 1 (refuted, hardened)
