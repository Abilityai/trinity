# Agent Canvas — a durable surface an agent renders onto (trinity-enterprise#438, widened by #536, designed by #537)

> **One idea**: a **report** is a thing published once and accumulated; a
> **canvas** is one surface the agent keeps *current*. Same output, opposite
> lifetime. The composite primary key `(agent_name, canvas_id)` is what makes
> that difference structural rather than a convention someone has to remember.
>
> **ent#536 adds the second idea**: one block vocabulary, written the same way
> by the agent (`set_canvas` / `patch_canvas`) and by its voice mode (the panel
> tools), onto one default canvas, rendered by one component wherever a canvas
> is shown. The agent provides data; the platform draws it. No JavaScript from
> the agent, ever.

## Why

"Workspace" meant two things. There was **the** Workspace (`/workspace`) where
people work with their agents, and a per-agent workspace
(`/agents/:name/workspace`) — a Gemini voice orb beside a canvas panel, marked
BETA, gated behind `VOICE_ENABLED && GEMINI_API_KEY && WORKSPACE_ENABLED`.
Users had to know which one they were in, and capability differed between them
for no reason a user could state.

The more valuable half was the canvas. Agents produce results that are not chat
messages — tables, charts, rendered reports, dashboards — and had nowhere to
put them: they were flattened into chat text or became files someone had to go
find. The only canvas that existed was `VoiceSession.panel_state`: in memory,
writable only by the Gemini Live voice tools, on one page, and gone when the
session ended.

After ent#438 the canvas existed but was thin (ent#536's context, verified on
`origin/dev@1fd289cf`): `chart` drew only a trend line over a UTC-day axis, there
was no `image` or `diagram` kind, the voice path had lost diagram rendering
when the old page went (a ```mermaid fence rendered as a code block), the voice
tools and the MCP tools spoke different vocabularies into different canvases,
and nothing taught the text agent the canvas existed.

**OSS-core by decision.** Deliberately ungated — no `requires_entitlement`,
logic in the OSS tree. Recorded because the default for an enterprise-tracker
feature is *gated unless ruled otherwise*, so the ruling must never be inferred
later from the mere fact that it merged (the ent#326 / ent#384 / ent#392
discipline). Rationale, on operator instruction: the Workspace and everything
around it is OSS.

## The merge (ent#438 AC 1)

`/agents/:name/workspace` is **deleted** and its route is a query-preserving
redirect to `/workspace?agent=<name>`.

It is only safe to delete because **ent#440 already put voice conversation
inside the Workspace**, so once the canvas moved the page had no capability of
its own left. Same shape as the ent#358 Session-surface retirement and the
ent#381 Sessions-page retirement: the surface goes, every old link still lands
somewhere true.

Two knock-on edits, both of which would otherwise leave dead behaviour:

- `AgentHeader`'s Workspace button now opens `/workspace?agent=`, is no longer
  gated on `workspaceAvailable`, and is no longer disabled while the agent is
  stopped. The Workspace reports availability itself (#2196), and a dead button
  is a worse answer than a page that says why.
- `ChatPanel`'s voice overlay starts its session with `workspaceMode: true`.
  **This is load-bearing, not a bonus**: the retired page was the only caller
  that passed it, so bridging the voice panel to the canvas while leaving it
  unreachable would have been dead code wearing a fix's name.

## Storage

```sql
CREATE TABLE agent_canvases (
    agent_name TEXT NOT NULL,
    canvas_id  TEXT NOT NULL,
    title      TEXT,
    blocks     TEXT NOT NULL,     -- JSON [{id, kind, title?, payload}]
    audience   TEXT NOT NULL DEFAULT 'operator',   -- 'operator' | 'roster'
    schema_version INTEGER DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    updated_by_execution_id TEXT,
    template   TEXT,              -- ent#537: 'dashboard'|'report'|'brief'|'status-board'; NULL = stacked
    PRIMARY KEY (agent_name, canvas_id)
);
CREATE INDEX idx_agent_canvases_agent ON agent_canvases(agent_name, updated_at DESC);
```

- `created_at` is preserved across updates — it is the age of the **surface**;
  `updated_at` is what moves.
- Registered in `AGENT_REFS` (CASCADE). The rename half is load-bearing rather
  than tidy: `agent_name` is *half the primary key*, so an unregistered table
  would leave a renamed agent's canvas addressed to a name nothing resolves,
  and the agent's next write would silently mint a **second** canvas under the
  new name while the old one stayed visible.
- **No retention window.** The table is bounded by the composite key — one row
  per surface, replaced on write — unlike the append-only tables
  `RETENTION_OPS_KEYS` governs. It is deliberately absent from that set.
- Dual-track migration: `db/migrations.py::agent_canvases_table` +
  Alembic `0050_agent_canvases`. ent#536 changes **no DDL**: block ids and the
  new kinds live inside the `blocks` JSON, so there is no migration and no
  Alembic revision for it. ent#537 adds the nullable `template` column
  (`agent_canvases_template` + Alembic `0054_agent_canvases_template`, no
  backfill); the per-block `slot` lives in `blocks` because it travels with the
  block through `patch_canvas`. `_SUMMARY_COLUMNS` appends `template` LAST —
  `_row_to_summary` reads by position, and `_row_to_full` takes the blocks
  index from the column list rather than a literal.

## The default canvas (ent#536)

`DEFAULT_CANVAS_ID = "main"` (`models.py`; mirrored in `canvas.ts` and
`canvasUtils.js`, parity-pinned). It is the canvas the MCP tools write and
read when no `canvas_id` is given, **and** the canvas the voice panel tools
draw on — one surface for the agent and its voice mode, which is what makes
"rendered the same everywhere" more than a slogan. Named canvases remain for
everything else.

## The block vocabulary (ent#536)

A canvas is an ordered list of `{id, kind, slot?, title?, payload}`. **Every stored
block carries an id**: `set_canvas` assigns `b1..bN` (by position, never
shadowing an id the agent declared) to id-less blocks, so anything written is
addressable by `patch_canvas` and `get_canvas` shows what to address. Ids share
the canvas-id charset; duplicates within a write are a named 400.

| kind | payload | renderer |
|---|---|---|
| `table` `kpi` `markdown` `timeline` `json` | as the report contract (`{columns,rows}`, `{tiles}`, `{markdown}`, `{events}`, raw) | delegated to the shared `components/reports/` dispatch |
| `chart` | the **metric series shape**, below | `CanvasChart` → `TrendLineChart` (line/area) · `StackedBarChart` (bar/stacked_bar) · `CanvasPieChart` (pie/donut) |
| `html` | `{html}` | DOMPurify via `utils/markdown.js::sanitizeCanvasHtml` (H-005 + the ent#537 kit allowlist) |
| `image` | `{src, caption?, alt?}` → stored as `{src, src_kind, …}` | `CanvasImage` |
| `diagram` | `{mermaid}` (≤ 20,000 chars) | `CanvasDiagram` (lazy mermaid, strict, sanitised) |

The five shared kinds **delegate, never fork** — those renderer keys are
CI-pinned as the canonical contract (`test_1535_report_prompt_guidance.py`).
The report `display_hint` enum is deliberately **not** widened: a canvas is a
superset of a report's rendering, not a change to what a report is. An unknown
kind resolves to `json`, never to nothing — a silently dropped block would leave
the surface looking complete while missing content. Every canvas kind whose
payload cannot make its kind (a chart with no points, an image with a refused
source) also lands on `json`: the reader still sees the data.

### `chart` — the metric series shape (ruled 2026-09-07, aligned with #478/#479)

```json
{ "type": "bar | stacked_bar | line | area | pie | donut",
  "series": [ { "label": "Leads", "unit": "new", "color": "#6366f1",
                "stale": false, "last_point_at": "2026-09-02T09:00:00Z",
                "points": [ { "ts": "2026-09-01", "value": 14 } ] } ] }
```

One series per line, stack segment or slice; a point is `{ts, value}` and
nothing else — a literal projection of a `metric_points(metric, ts, value)` row
with the registry's `label`/`unit`, so a declared metric later becomes a payload
*source* (#538 resolves `{metric, window, agg}` into exactly this) rather than a
new kind. Categories are **series**, as they are dims in the store: a "leads by
region" bar is four series with one point each. The axis kind is derived —
every `ts` parses as a date → a time axis (day, or day + time when labels are
closer than a day); otherwise `ts` is a category name shown verbatim. Pie and
donut take each series' last non-null point; bar/stacked_bar render one bar per
series when every series holds one point, else one column per label with the
series stacked. Per-series `stale`/`last_point_at` mirror #479's read; the
chart shows "as of" and a stale mark as additions. The pre-ent#536
`{labels, series[{label,data}]}` shape is still accepted and normalised, so
canvases written since ent#438 keep rendering.

Everything an agent authors on a chart is normalised in `canvasUtils.js`
before a component sees it: a `color` must be a hex triplet or it becomes the
palette entry for its position (`utils/canvasPalette.js` — hex in JS by the
`executionBuckets.js` precedent; a uPlot stroke of `undefined` draws nothing,
which reads as "no data"), a `label` is plain text capped at 80 characters.
The trend tooltip that used to concatenate label and colour into `innerHTML`
now builds with DOM APIs — on a `roster` canvas those strings reach a customer's
browser.

### `image`

`src` is an `https?://` URL (≤ 2 KiB, no whitespace or control characters), an
inline `data:image/(png|jpeg|gif|webp);base64,…` under
`CANVAS_IMAGE_INLINE_MAX_BYTES` (64 KiB), or a path to a file in the agent's
workspace — relative, `~/`, or absolute under `/home/developer`, `..`-free,
sibling-prefix (`/home/developer-evil`) refused (the #979 gate, now
`services/canvas_blocks.py::classify_image_src` and shared by both writers).
Every other scheme (`file:`, `ftp:`, `javascript:`, protocol-relative `//`)
and `svg+xml` (a document, not a raster) is a named 400 at write. The write
normalises the payload to `{src, src_kind}`; `CanvasImage` trusts the kind and
rechecks the prefix, because a stored block outlives the validator. A web URL
or data URI binds directly; a workspace path is fetched through the
authenticated `/files/preview` route (`agentsStore.getFilePreviewBlob` — a bare
`<img src>` there would 401) into an object URL that is revoked on change and
unmount. **CSP note (a stated posture change):** the frontend CSP had
`img-src 'self' data: blob:` since #190, so a web-URL image was blocked at the
browser on every deployment — #979's "web URLs render directly" never held in
production. ent#536 widens `img-src` to include `https:` in BOTH mirrored
definitions (`vite.config.js`, `security-headers.conf`); the cost is the
tracking-pixel class (an agent-chosen host learns a viewer's IP, user agent
and time of viewing). The directive is page-global, so the mitigation is too
(the `/cso --diff` verifier caught that an `image`-block-only mitigation left
`html`/`markdown` blocks and every other `renderMarkdown` surface open):
`utils/sanitizeHooks.js::hardenMediaAttributes` runs inside the app's one
DOMPurify `afterSanitizeAttributes` hook and gives **every** sanitised `<img>`
`referrerpolicy="no-referrer"` (the deployment origin never travels) and
removes any inline `style` that references `url(`/`image-set(`/`@import` (a
CSS background is the same pixel through another attribute). The canvas
`<img>` carries `no-referrer` explicitly as well. The write gate admits
`https:` only — `http://` is refused at write rather than left to the browser,
so the refusal text and the validator say the same thing. Where that route is unreachable (an external Workspace client, a
stopped agent) the block says so instead of rendering a broken image.

### `diagram`

Mermaid, rendered in-parent — the production CSP (`script-src 'self'`) blocks
inline scripts in a srcdoc iframe and CORP blocks the bundle from its opaque
origin. `import('mermaid')` is lazy (its own chunk; only a canvas with a diagram
pays ~1.5 MB). `MERMAID_CONFIG` (`canvasUtils.js`, spec-pinned) is
`securityLevel: 'strict'` **with `htmlLabels: false`** — load-bearing, not
style: mermaid 11 keeps HTML labels on under `strict` and DOMPurify forbids the
`<foreignObject>` they live in, so without it every flowchart node renders as an
empty box after sanitisation. The SVG goes through the app's one DOMPurify
instance (`sanitizeSvg` — the one entry point that keeps the diagram's own
id-scoped `<style>`) before `v-html`. Initialisation is global to mermaid, so
it is done once per theme at module level; render ids come from a module
counter (per-instance counters collide on `<marker id>` and arrowheads vanish);
renders are **serialised** through a module-level promise chain, because
`mermaid.render` shares parser/layout state and two diagrams mounting at once
bleed nodes into each other's SVG (seen live on the seed canvas, not
hypothesised); a parse error shows a contained, **height-bounded** error box
with the source, and the scratch node mermaid leaves behind is removed.
**"Module level" means a plain `<script>` block, not `<script setup>`
(#2583):** the SFC compiler moves every top-level binding of a setup block
into `setup()`, which silently made the counter and the chain per-instance —
and because mermaid begins every `render(id)` by deleting any element already
carrying that id from the live document, three diagrams sharing
`canvas-mmd-1` meant each render removed the previous diagram's SVG from the
page: a canvas of eleven diagrams showed eight empty blocks. A diagram wider
than its column keeps its natural width and scrolls inside its wrapper rather
than scaling down to a strip (`keepLegibleWidth`).

### Rich fences in `markdown`

A `markdown` block may carry ```chart / ```kpi / ```table fences (JSON inside)
and ```mermaid fences. `canvasUtils.splitRichFences` (pure, spec-pinned)
extracts **only** a column-0 opener of exactly three backticks plus one of
those languages, closed by a column-0 ``` line, and only when the body is
usable (JSON in the shape the renderer reads, or non-empty Mermaid source).
Everything else — `~~~`, four-backtick, indented, blockquoted or unterminated
fences, an info string with extras, JSON that will not parse or will not make a
chart — stays in the prose byte-for-byte and renders as the code block it is;
any other fence is copied through to its own close untouched, so a ```chart
shown inside a ````markdown example is never extracted. `CanvasMarkdown` renders
prose through the report markdown renderer and figures through the **same
leaves** the standalone kinds use (it never imports `CanvasBlock`; fences do not
nest). A markdown block with no renderable fence takes the ent#438 path
byte-for-byte. The extracted JSON is handed to components as data — never
joined back into HTML — so nothing here widens the DOMPurify policy.

## The design kit (ent#537)

An agent's canvas looks designed without the agent touching CSS. Operator
direction: "learn from how we do the microsite reports and explainers —
efficient and quick, but good looking." What makes those cheap is ONE
stylesheet: the author composes against known classes and skeletons, and the
figures come from data. Here the figures are ent#536's kinds and fences; the
kit dresses the page around them.

**Home.** `components/canvas/CanvasKit.vue` — the `.canvas-kit` wrapper every
`CanvasPanel` renders blocks inside, plus an UNSCOPED `<style>` whose every
selector sits under `.canvas-kit`. Unscoped because Vue's scoped CSS stamps
data attributes on compiled template nodes only and `v-html` children never get
them — the prefix IS the scope. An SFC rather than a `.css` file because the
raw-colour ratchet walks `.vue`/`.js` and counts literals inside `<style>`
blocks; every colour is a `theme()` token with a `.dark` override (the
`ScanlineReveal.vue` precedent), so the kit ships at a raw-colour count of
zero and `/audit-design-system` sees it on every run. Collapse is keyed on the
kit's own inline size (`@container`), never the viewport — the Portal rail is
~300px wide on a desktop screen.

**Vocabulary** (`utils/canvasKit.js::KIT_CLASSES`, the one list the prompt is
pinned against): `ck-card` (+ `-title`, `-meta`, `-body`) · `ck-grid-2/3/4`,
`ck-span-2`, `ck-span-full`, `ck-stack`, `ck-row` · `ck-section` (+ `-title`,
`-sub`) · `ck-kpi` (+ `-label`, `-value`, `-unit`, `-delta` with
`ck-up|ck-down|ck-flat`) · `ck-table` (+ `ck-table-wrap`, `ck-num`) ·
`ck-callout` and `ck-chip` with the tones `ck-info|ck-success|ck-warning|
ck-danger|ck-neutral` · `ck-figure` + `ck-caption` · `ck-muted`, `ck-small`,
`ck-mono`, `ck-right`, `ck-center`. `ck-kpi` and `ck-table` are the v-html
twins of `ReportKpiTiles.vue` / `ReportTable.vue` (same tokens) — the agent is
taught to prefer the `kpi` / `table` kinds for data and reach for the classes
only inside custom `html`. The kit is the one sanctioned exception to
primitives-first (agent markup cannot mount a component), recorded in
`design-system.md`.

**The sanitiser admits the kit and nothing else — on the canvas.** `html`
blocks go through `sanitizeCanvasHtml`, markdown prose through
`renderCanvasMarkdown` (`CanvasProse.vue`, the report renderer's twin): the
app's ONE DOMPurify instance with a per-call `canvasKit: true` config flag,
which the existing `afterSanitizeAttributes` hook reads from its **third
argument** (DOMPurify hands every hook the whole config), so there is no
module state to leak on a throw. In canvas mode `restrictToCanvasKit` keeps
only exact `KIT_CLASSES` members on `class` (the attribute goes when none
survive), keeps only `width` / `max-width` on `style` with a bounded value
(`%` ≤ 100, `px` ≤ 9999 — `url(`, `calc(`, `var(`, `!important` are
unreachable by shape), and `id` is forbidden. Canvas-scoped rather than
app-wide because chat and report markdown depend on the `code-block*` classes
the decorator injects before sanitising (#2515).

**`<style>` is forbidden everywhere.** DOMPurify's default tag list admits the
`<style>` ELEMENT, and a body `<style>` is document-global — so before #537 an
agent message, a report or a canvas block could ship
`<style>.x{position:fixed;inset:0}</style>` and restyle the whole page,
including a customer's Workspace on a `roster` canvas. No attribute allowlist
closes that; `BASE_CONFIG = { FORBID_TAGS: ['style'] }` on every markdown/html
path does. The one exception is by name: `sanitizeSvg`, which `CanvasDiagram`
uses, keeps the default list because mermaid emits the diagram's own id-scoped
stylesheet inside the `<svg>`.

**Typography.** The wrappers keep `.prose prose-sm` so plain `<h2>` / `<p>` /
`<ul>` keep their typography; typography's rules are `:where()`-wrapped
(0,1,0) and lose to `.canvas-kit .ck-*` (0,2,0), and the kit resets the
properties it owns (margins, table chrome, tile numerals). CommonMark rule for
kit markup inside a `markdown` block: no blank line inside a kit element, or
marked ends the HTML block and wraps what follows in `<p>` — use an `html`
block for anything pretty-printed.

## Starter layouts (ent#537)

A canvas may declare `template` ∈ `dashboard` | `report` | `brief` |
`status-board` — a nullable column on the row, because a layout is a property
of the surface like `audience`. Each names the slots a block fills through its
`slot` key (`CANVAS_LAYOUT_SLOTS` in `models.py`, mirrored in `canvas.ts` and
`canvasLayouts.js::LAYOUTS`, parity-pinned):

| template | slots (in render order) | side-by-side slot |
|---|---|---|
| `dashboard` | header · kpis · main (2/3) + side (1/3) · footer | kpis |
| `report` | header · summary · body (72ch measure) · figures · appendix | figures |
| `brief` | header · key-points (1/3) + body (2/3, 72ch) | — |
| `status-board` | header · status · issues (2/3) + next (1/3) · log | status |

**A layout never hides a block** (`canvasLayouts.js::placeBlocks`, pure): a
block with no slot, or naming a slot the layout does not know, renders after
the layout in the stacked list; a layout with nothing slotted degrades to the
stacked list; empty regions are not rendered. So an unknown SLOT is not
refused (losing content to a typo is the worse failure) while an unknown
TEMPLATE is refused by name (`canvas_service.validate_template`; the Pydantic
`Literal` gives 422 on REST) — silently stacking would teach the agent the
wrong name. `renderableBlocks` carries `slot` explicitly (that rebuild is a
field allowlist; a key it does not name never reaches the layout). Every
writer carries the template: `set_canvas` sets it, `patch_canvas` and the
voice panel verbs pass the stored value through. The layout CSS
(`.ck-layout-<template>`, `.ck-slot-<slot>`) is app-emitted and deliberately
absent from `KIT_CLASSES`, so an agent cannot fake a region; under 640px of
container width a layout becomes one column in slot order.

Rejected alternative, recorded: a per-block `span: full|half|third` hint with
no migration. The operator asked for layouts by name; revisit if a fifth
layout is requested.

## Audience — how "never widens" is made structural (ent#438 AC 8)

`audience` defaults to `operator` and is a **validated column, never a key
inside `blocks`** (the ent#364 rule): `blocks` is agent-authored, so an audience
buried there would let a prompt-injected agent choose its own readers.

- `operator` → Agent Detail only.
- `roster` → additionally the agent's Workspace page, for anyone already
  rostered on that agent.

`normalize_audience` is an **allowlist**: an unrecognised stored value reads as
`operator`. The Workspace read narrows **in the query**
(`db.list_agent_canvases(agent, audience='roster')`), not afterwards. Both gates
are needed and neither is redundant: the roster gate answers *may this person
reach this agent*, the audience narrowing answers *did the agent mean this for
them*.

**Audience is a property of the write, not only the canvas (ent#536).** A
writer carries the widest audience it may publish at; `write_canvas` takes
`audience` as a REQUIRED parameter and `canvas_service.audience_within(stored,
writer)` answers whether a writer may land on a stored canvas: `operator` ⊂
`roster`, never the other way. See the voice section for where that bites.

## Staleness — derived, not a clock (ent#438 AC 7)

`stale` is `last_completed_execution_at(agent) > canvas.updated_at` — the agent
finished a run and did not refresh this surface.

An age threshold was rejected: a canvas has no inherent freshness expectation,
so a clock either cries wolf on a monthly summary or stays silent on a
minute-by-minute one. "The agent has run since" is a fact about *this* canvas,
needs no configuration, and is checkable against `updated_by_execution_id`.

`db.last_completed_execution_at` is a `MAX` over the whole column rather than a
bounded scan of recent rows, deliberately: a head full of `queued`/`running`
rows would push the newest COMPLETED row out of a window and report a stale
canvas as current — the failure this AC exists to prevent.

**Fail-quiet is available here and only here.** Missing evidence reads as "not
stale", because the mark is an *addition* to an always-rendered `updated_at`,
never a replacement for it. Marking on no evidence would train the reader to
ignore the mark. It is derived once per agent, not once per canvas.

## Write path — one, for both writers (ent#536)

`canvas_service.write_canvas(agent, canvas_id, blocks, *, title, audience,
execution_id)` is the ONLY path that writes `agent_canvases`: id validation,
block validation (ids, per-kind rules, image normalisation, the diagram cap),
the byte cap, execution resolution, upsert. The REST/MCP write and the voice
panel tools both call it, so a block renders identically whoever wrote it and a
cap the router enforces cannot be bypassed by the voice path. The pure block
rules live in `services/canvas_blocks.py` (importing nothing that touches the
DB — so `gemini_voice`, whose unit suite stubs `config` down to a handful of
names, can import them at module level).

`PUT /api/agents/{name}/canvas/{canvas_id}` — PUT because the operation is
idempotent on the key, which is the surface's whole contract.

`PATCH /api/agents/{name}/canvas/{canvas_id}` (ent#536) — body
`{blocks:[{id, kind, title?, payload}], execution_id?}`; every block must carry
an id; each replaces the stored block with that id **in place**, order kept; an
unknown id is refused **by name** (`unknown block id(s): b9 — set_canvas writes
the full state`), a canvas written before ids existed is refused with the fix
spelled out, and a stored duplicate id is replaced wherever it occurs. Title and
audience are untouched — a content edit, not a republish; the provenance stamp
becomes this write's. The merged list goes back through `write_canvas`, so a
patch can no more exceed a cap or smuggle a bad image source than a full write
can. Read-modify-write under the last-writer-wins contract the db layer already
documents (one writer per canvas — the agent itself); the tool description says
so. There is still **no append** (the #438 rule): the agent names what changes.

Both writes are **self-gated** (`AuthorizedAgent` proves the key's *owner* can
reach the path agent; it does not stop an agent-scoped key writing as a
*sibling* the same owner shares — the #918 rule, and a disclosure surface too
because a `roster` canvas is client-visible), share one per-agent rate bucket,
carry the Content-Length hint before the exact byte check, and cap at 50 blocks
/ 512 KiB (plus the per-kind caps above). `execution_id` is resolved through
`resolve_and_validate_execution` (MEM-001); a foreign id **degrades to None
rather than refusing the write** — provenance, not authorization. Reads are
**not** self-gated: an operator reads as a user-scoped principal with no
`agent_name`, and the `{self} ∪ permitted` narrowing for agent keys lives at the
MCP layer.

## The voice panel IS the default canvas (ent#438 FR-7 → ent#536)

There is no in-session `panel_state` any more — the canvas row is the state,
the live poll (`GET /api/agents/{name}/voice/{session_id}/panel`) returns the
agent's `main` canvas (or an empty canvas shape, 200, during teardown), and the
Canvas tab shows the same blocks after the call.

Each panel verb is a block edit, mapped by the pure
`canvas_blocks.map_panel_tool` and written through `write_canvas`. The voice
panel owns the `voice*` block ids on `main` and nothing else:

| voice tool | block edit |
|---|---|
| `show_markdown(content, title?)` | replace every `voice*` block with one `voice` **markdown** block where the first stood |
| `show_diagram(diagram, title?)` | … one `voice` **diagram** block (`{mermaid}`, capped) |
| `show_image(src, title?, caption?)` | … one `voice` **image** block, through the shared confinement gate |
| `update_panel(html, title?)` | … one `voice` **html** block |
| `append_to_panel(html)` | grow the trailing `voice*` html block, else start `voice-N` |
| `clear_panel()` | remove the `voice*` blocks only |

`title` is the **block** title, never the canvas title. Blocks the agent wrote
with `set_canvas` survive a call untouched — that is what makes one shared
default canvas safe (both independent reviewers hit the alternative, "replace
the whole canvas", as the 6-month regret). A refused verb (a bad image source,
empty or over-cap diagram source) writes nothing and returns the reason for
the model to voice.

**Audience.** `VoiceSession.canvas_audience` (default `operator`) is the widest
audience the session may write at. A Workspace call (ent#534) writes at
`operator` too — an internal user's call is an operator surface; the person in
the call sees what was drawn because a **platform principal reads every
audience in the Workspace** (below), not because the call published wider. The
plan review found the alternative (`roster`) would let a call that CREATES
`main` publish its drawings to every external client on the roster. A canvas stored WIDER than the
session's audience is **refused** with a tool result the model can speak — an
operator's Agent Detail call must never land on a customer's Workspace because
the agent had published its board there — and a canvas stored narrower keeps
its stored audience, so a call never widens either. A new `main` takes the
session's audience.

Fail-soft on the store, honest in the result: a write that raises is logged and
the tool result says the canvas could not be saved, so the model does not
describe a drawing nobody can see.

## The regular agent is taught (ent#536, ent#537)

ent#537 adds ONE compact worked example to `### Your Canvas`
(`template="dashboard"` with slotted blocks and a kit card), the four layouts
with their slots, and the kit's class list — the platform prompt is the only
channel a fresh agent has, and the issue's test of done is a fresh agent
producing a designed dashboard without coaching. The context cap
(`test_ent536_canvas_prompt_guidance.py::MAX_BLOCK_CHARS`) is raised
2,700 → 3,400 deliberately. The full reference and three worked examples live
in the `canvas` **library skill** (`abilityai/trinity-skills`, category
`visual-communication`), which opens with the same example the prompt
teaches; the voice panel's HTML rule names the same classes.

`### Your Canvas` in `PLATFORM_INSTRUCTIONS` (the sibling of `### Publishing
Reports`): when to use the canvas rather than a report, `set_canvas` /
`patch_canvas` / `get_canvas`, every kind with a one-line payload example, the
fences, the default canvas, the ceilings (interpolated from `models.py`, never
typed twice), and the no-JavaScript rule. Registered in `_KNOWN_SECTION_HEADINGS`
and `_MINIMAL_DROP_SECTIONS` — tool guidance, dropped at the MINIMAL tier like
the report block. CI-pinned by `test_ent536_canvas_prompt_guidance.py` against
the MCP `BLOCK_KINDS` enum and the frontend rules, exactly as `test_1535` pins
the report block. The Codex orientation lists the canvas tools by bare name.

## The render bar — the gallery (#2583)

The canvas is measured, not eyeballed. `e2e/helpers/canvas-gallery.js` is
seventeen canvases / ~180 blocks covering every kind, every chart type, the
rich fences, the kit classes, the four layouts and the adversarial shapes an
agent will eventually emit (200-row tables, 60-char tokens, twelve series,
20,000-char Mermaid, a `width: 9999px` card, CJK/RTL, a non-string payload),
seeded through the **real** `PUT` route so what is asserted is what an agent
can actually write. `e2e/canvas-gallery.spec.js` renders each on Agent
Detail (1280 / 1920) and the Workspace rail (a fixed 24rem column), and
`e2e/canvas-gallery-voice.spec.js` on the Workspace **voice column** (the
60% share during a call, 1280 / 1920 — only the call plumbing is mocked:
the start request, the audio socket and the panel read; the real column
mounts on the real page and switches boards the way a call does, on a
panel-tool `tool_result` frame), light and dark, and asserts per block: the page and the kit never scroll horizontally,
the block sits inside the kit, a drawing kind (diagram / image / chart) drew
its figure or its named fallback, nothing spills past the block as a box or
as text unless a scroll container between them owns it, and stacked siblings
do not overlap. Screenshots land in `e2e/canvas-gallery-shots/`.

What it pinned, each now structural rather than per-case:

- **A block never vanishes.** `CanvasBlock` is an error boundary
  (`onErrorCaptured`): a leaf that throws — `marked()` on a non-string
  `markdown`, a timeline with a `null` event — shows the payload as JSON in
  place instead of unmounting the section. `markdown` reaches `CanvasProse`
  only as a string.
- **Bounded viewports (principle 28).** `table` and `timeline` get a 420px
  scroll box on the canvas (the kit's `ck-table-wrap` bound); a Mermaid error
  box is bounded too. The delegated KPI grid re-flows by the **kit's**
  container width (`.report-kpi-grid` in `CanvasKit.vue`), not the viewport's
  `sm:`/`lg:` — the 24rem rail is `lg:` on a desktop screen.
- **Long tokens wrap, tables scroll.** Canvas prose / html wrappers and KPI
  tiles use `overflow-wrap: anywhere`; table cells inside prose reset it and
  the table becomes its own horizontal viewport, so a wide markdown table or
  a bare `<table>` from the voice panel never pushes the column.
- **An admitted pixel width is bounded.** `filterInlineStyle` emits
  `min(<px>, 100%)` — `width: 9999px` on a card used to scroll the whole
  canvas 10,000px sideways.
- **Charts say what they show.** Line/area charts carry a legend when they
  have more than one series; a flat series (all zero, one point) gets a
  padded y range instead of a degenerate one; isolated points (a sparse
  series, a single point) are drawn as markers (`points.filter`); long
  category labels space the axis (`axisLabelSpace`) instead of overprinting;
  a pie legend wraps under the pie in a narrow figure cell.
- **Switching canvases is guarded.** `CanvasPanel.select` drops a fetch a
  later selection superseded — the header used to name one canvas while the
  slower fetch's blocks belonged to another.
- **The call columns are flex shares, not row percentages.** `<main>`
  `sm:flex-[2_1_0%]` and the voice canvas `sm:flex-[3_1_0%]` — the old
  `w-[40%]` + `w-[60%]` beside the 18rem sidebar summed to 100% + 18rem and
  the shell's `overflow-hidden` clipped the canvas column off the right edge
  with no scrollbar (#2581's second bullet; 296px at 1280, measured).
- **Every block is its own container.** `[data-canvas-block]` is
  `container-name: block`; the kit grids and the delegated KPI grid collapse
  on the *block's* width, so a `kpi` block in a layout's 160px grid slot goes
  to one column instead of four 45px tiles. Layout collapse stays keyed on
  the kit (`canvas`).

Findings the write route owns (a 422 / 400 by name, so they cannot be
seeded): an unknown `kind`, a diagram with blank source.

## Surfaces

| Surface | Route | Sees |
|---|---|---|
| Agent Detail → **Canvas** tab | `GET /api/agents/{name}/canvas[/{id}]` | every canvas |
| Workspace agent page → **Canvas** tab | `GET /api/enterprise/client-portal/agents/{name}/canvas[/{id}]` | portal-token client: `audience='roster'` only · platform principal: every audience (ent#534, `agent_page.canvas_audience_for`) |
| Workspace conversation rail → **Canvas** tab (ent#475) | the same routes, the same `CanvasPanel` per participating agent | the same rule as the row above |
| Workspace voice call → right column (ent#534) | `GET /api/agents/{name}/voice/{session_id}/panel`, refetched on each panel `tool_result` frame + a 3 s safety poll (`PortalVoiceCanvas.vue`) | the agent's `main` canvas, whatever its audience — the person in the call sees what the call draws |

**Why a platform principal reads every audience here (ent#534).** They can already open Agent Detail for any agent on their Workspace roster and read every canvas there, so the `roster` narrowing hid nothing from them — it only made the board the orb drew during a call (`main`, `operator` by default) vanish from the rail the moment the call ended. The narrowing stays for portal-token clients, keyed on `principal.is_platform` and passed as an explicit argument (the fail-closed default is `roster`). **Deliberately asymmetric with Reports** on the same page, which stay addressed-to-me for everyone: a report is *sent*, a canvas is the agent's *surface*.
| MCP | `set_canvas` · `patch_canvas` · `get_canvas` · `list_canvases` · `clear_canvas` | its own |

**Rendering parity** is one component: `CanvasPanel` → `CanvasBlock` →
{`CanvasChart`, `CanvasDiagram`, `CanvasImage`, `CanvasMarkdown`,
`ReportRenderer`, sanitised html}. No surface renders a canvas any other way,
and `CanvasPanel` passes the canvas's `agent_name` down only so an image block
can fetch a workspace file. Empty states differ by viewer (AC 6) because the
next action does: an operator can make an agent write a canvas, a client cannot.

Deferred still: a platform-wide `canvas_updated` WebSocket trigger (ids only,
the #918 rule). ent#534's canvas column did not need it — the voice call IS a
WebSocket, and the bridge already emits a `tool_result` frame for every panel
verb, so the column refetches on those and keeps only a slow safety poll. The
rail's Canvas tab outside a call still refreshes on its own triggers.

## Key files

| Layer | File |
|---|---|
| DDL | `db/schema.py`, `db/tables.py`, `db/migrations.py`, `migrations/versions/{0050_agent_canvases,0054_agent_canvases_template}.py` |
| DB | `db/canvas.py` (`CanvasOperations`, `normalize_audience`) |
| Block rules (pure) | `services/canvas_blocks.py` (`classify_image_src`, `validate_blocks`, `patch_blocks`, `map_panel_tool`) |
| Service | `services/canvas_service.py` (`write_canvas`, `patch_canvas`, `validate_template`, `audience_within`, derived staleness) |
| Router | `routers/canvas.py` (`# mcp: canvas.ts …` header; PUT / PATCH / GET / DELETE) |
| Workspace | `client_portal/agent_page.py::canvases`/`canvas_detail`, `client_portal/router.py` |
| Voice | `services/gemini_voice.py::_execute_panel_tool`, `routers/voice.py::get_voice_panel` |
| Prompt | `services/platform_prompt_service.py` (`### Your Canvas`) |
| Frontend | `components/canvas/{canvasUtils.js, canvasLayouts.js, CanvasPanel.vue, CanvasKit.vue, CanvasBlock.vue, CanvasProse.vue, CanvasChart.vue, CanvasPieChart.vue, CanvasDiagram.vue, CanvasImage.vue, CanvasMarkdown.vue}`, `utils/{canvasKit.js, canvasPalette.js, sanitizeHooks.js, markdown.js}`, `components/{TrendLineChart,StackedBarChart}.vue` (`labelFormat`) |
| MCP | `src/mcp-server/src/tools/canvas.ts`, `client.ts` |
| Tests | `tests/unit/test_ent438_agent_canvas.py`, `test_ent536_canvas_vocabulary.py`, `test_ent536_canvas_prompt_guidance.py`, `test_ent537_canvas_design_kit.py`, `test_voice_tools.py`, `src/frontend/tests/unit/{canvasUtils,canvasKit,canvasLayouts,sanitizeHooks}.spec.js`, `src/mcp-server/src/tools/canvas.test.ts` |

## Change Log

- **2026-09-07 (trinity-enterprise#537)** — the design kit (`CanvasKit.vue`, `utils/canvasKit.js`), the canvas-mode sanitiser (`sanitizeCanvasHtml` / `renderCanvasMarkdown`, per-call config flag, kit-class + bounded width allowlist, `id` dropped), `<style>` forbidden on every markdown/html path with `sanitizeSvg` split out by name, starter layouts (`template` column + Alembic 0054, per-block `slot`, `canvasLayouts.js`), the prompt's worked example and the `canvas` library skill.
| Date | Author | Change |
|------|--------|--------|
| 2026-09-02 | claude | Initial — canvas surface, workspace merge, voice-panel bridge (ent#438) |
| 2026-09-06 | claude | Conversation-side placement in the Workspace rail; `CanvasPanel` re-reads blocks when the selected canvas's `updated_at` moves (ent#475) |
| 2026-09-07 | claude | The render bar (#2583): the canvas gallery e2e (17 canvases, Agent Detail + rail + voice column, both themes, measured), the flex-share call columns, per-block containers, the per-block error boundary, bounded table/timeline/diagram-error viewports, container-keyed KPI grid, long-token wrapping, `min(px, 100%)` inline widths, chart legend/flat-range/isolated-point/axis-spacing fixes, the stale-fetch guard on canvas switching, and the `<script setup>` module-state fix that made diagrams vanish |
| 2026-09-07 | claude | One rich block vocabulary: `image` + `diagram` kinds, `chart` widened to six types on the metric series shape, rich fences in markdown, block ids + `patch_canvas`, the `main` default canvas, voice tools as block edits through the one write path with the write-side audience rule, `### Your Canvas` prompt guidance (ent#536) |
