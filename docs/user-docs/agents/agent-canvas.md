# Agent Canvas

A **canvas** is a surface an agent keeps *current* — a status board, a running tally, the latest version of an analysis, the chart you just asked for. Reports are the other half: published once, they accumulate as a record. A canvas is rewritten in place.

Every agent has a **Canvas** tab on its detail page. In the Workspace the same canvas shows on the conversation rail's **Canvas** tab — and, during a voice call, in the column beside the orb. The agent provides data; Trinity draws it. No script from the agent ever runs.

## Concepts

- **Canvas** — A named surface the agent writes with `set_canvas` and updates with `patch_canvas`. The default canvas is **`main`** — the one the tools write when no id is given, and the one the agent's voice mode draws on — so an agent and its call share one board. Named canvases keep separate surfaces. The header always shows when a canvas was last updated, and adds **may be out of date** when the agent has finished work since without refreshing it.
- **Blocks** — A canvas is an ordered list of blocks, each with an id: `kpi`, `table`, `chart`, `timeline`, `markdown`, `html`, `image`, `diagram`, `json`. Ids the agent leaves out are assigned (`b1`, `b2`, …) so every block is addressable later. A chart is one of `bar`, `stacked_bar`, `line`, `area`, `pie` or `donut` over one or more series of points; a diagram is Mermaid source; an image is a web URL, a small inline image, or a file in the agent's workspace. A `markdown` block may carry ```chart, ```kpi, ```table and ```mermaid fences, which render as figures inside the prose.
- **Design kit** — A small set of platform-owned styles (`ck-card`, `ck-grid-2/3/4`, `ck-section`, `ck-callout`, `ck-chip`, `ck-kpi`, `ck-table`, `ck-figure`) that make an agent's own `html` and `markdown` blocks look designed in light and dark without the agent touching CSS. Anything outside the kit — other classes, inline styles beyond a bounded width, `<style>` tags — is dropped before it renders.
- **Starter layouts** — A canvas can declare a `template`: **dashboard**, **report**, **brief** or **status-board**. Each has named slots (a dashboard has `header`, `kpis`, `main`, `side`, `footer`) that blocks fill through their `slot`. A layout never hides a block: anything not slotted renders after the layout, and with no template blocks simply stack.
- **Audience** — `operator` (the default) shows a canvas on Agent Detail; `roster` also shows it to the people the agent is shared with, in their Workspace. A signed-in platform user sees every audience in the Workspace; an external client sees `roster` canvases only.

## How It Works

1. Ask the agent in chat: "Put a dashboard of this week's pipeline on your canvas." (In the Workspace, the rail's Canvas tab shows **Ask for a canvas** while the agent has none; it pre-fills the request.)
2. The agent writes its canvas — a layout, KPI tiles, a chart, a short callout — and tells you.
3. Open the agent's **Canvas** tab, or the rail's **Canvas** tab in the Workspace. A dot on the rail tab means the canvas changed since you last looked. Narrow screens and the rail collapse layouts to one column.
4. Ask for changes the same way. The agent patches only the blocks that changed; the header timestamp moves.

During a [voice call](../advanced/voice-chat.md) the agent draws on `main` while it talks, and what it drew stays on the canvas afterwards. Blocks the agent wrote itself survive a call untouched: the call only ever replaces its own.

## Layouts at a Glance

| Template | Slots, in order |
|---|---|
| `dashboard` | header · kpis · main + side · footer |
| `report` | header · summary · body · figures · appendix |
| `brief` | header · key-points + body |
| `status-board` | header · status · issues + next · log |

## Tips

- **Prefer data blocks.** Ask for "a KPI row" or "a table of open items" and the agent uses the `kpi` / `table` kinds, which render with the same look as the kit and stay live-updatable.
- **Shared or private.** A canvas is private to the operator unless the agent publishes it to its roster. Ask the agent to "share this canvas with the team" and it appears in your clients' Workspace. When an agent writes a canvas from a public-link conversation, the write tells it whether the person asking can actually see the result, so it can widen the audience instead of reporting a success nobody sees.
- **No skill needed.** The platform prompt every agent receives teaches the canvas itself — every block kind with a payload example, the fences, the four layouts and their slots, the design kit's classes, and one worked dashboard — so a fresh agent produces a designed canvas without coaching — including when to *retire* a canvas rather than add another. (A fuller `canvas` reference skill is planned for the skills library but is not yet published to the catalog.)
- **Markdown and raw HTML.** In a `markdown` block, kit markup must not contain blank lines (Markdown treats a blank line as the end of the HTML). For a fully custom layout the agent uses an `html` block.
- **Nothing vanishes.** A block Trinity cannot draw — a chart with no points, an image whose source was refused, a diagram that does not parse — shows its data, or a contained error with the source, in place. Long tables and timelines scroll inside a bounded box rather than stretching the page, and a wide diagram scrolls inside its own frame.

## Removing canvases

An agent that uses its canvas well accumulates them — one per report, per topic,
per run. You can clear the pile from either the **Canvas tab** on Agent Detail or
the **Canvas tab in the Workspace rail**.

- **Delete one**, or switch on **Manage** to select several and delete them in
  one action. You are asked to confirm once, and the confirmation names how many
  will go. Deleting is recorded in the audit log — from the Workspace as well as
  from Agent Detail, attributed to the person who did it either way.
- **Only the agent's owner (or an admin) can delete or pin.** If you can see an
  agent's canvases but do not own the agent, there are no delete controls —
  rather than buttons that would refuse.
- **Pin** the ones you use daily and they stay at the top as the list grows. A
  pin is yours, not the agent's: the agent cannot pin its own canvas — the pin
  route refuses an agent's own key outright — and rewriting a canvas does not
  un-pin it. Pinning is recorded in the audit log too, from either surface,
  because a pin decides what everyone who can see the agent sees first.
- **Search** appears once there are more than six, matching the title or the id.
  Each row in Manage shows how old it is and whether it may be out of date.
- **Deleting the default canvas is fine.** The agent recreates it the next time
  it writes; you lose the contents, not the surface.

**There is a limit.** Each agent can hold 100 canvases, and the Canvas tab tells
you the count as you approach it rather than only when the agent is refused. At the limit the agent
can still *update* everything it has, but creating a *new* one is refused with a
message telling it to retire one first — nothing is ever deleted automatically to
make room. If you see an agent bumping into this, it is usually writing a new
canvas per run instead of keeping one per topic current; asking it to reuse a
canvas, or clearing the finished ones, fixes it.

## Security

All canvas content is sanitised before it renders. Scripts never execute, `<style>` tags are removed everywhere, and on a canvas only the design kit's classes and a bounded `width` / `max-width` survive — so a canvas cannot restyle the page around it, on your screen or a customer's. Image blocks accept `https://` URLs, small inline images and workspace paths only; external images load without a referrer.

## For Agents

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/agents/{name}/canvas` | GET | List the agent's canvases (metadata) |
| `/api/agents/{name}/canvas/{canvas_id}` | GET | One canvas with its blocks |
| `/api/agents/{name}/canvas/{canvas_id}` | PUT | Create or replace a canvas — `{title?, blocks, audience?, template?, execution_id?}`; at most 50 blocks / 512 KB. The result carries `visible_to_requester` and `visibility_note` |
| `/api/agents/{name}/canvas/{canvas_id}` | PATCH | Replace named blocks in place — `{blocks:[{id, kind, title?, slot?, payload}]}`; an unknown id is refused by name |
| `/api/agents/{name}/canvas/{canvas_id}` | DELETE | Remove a canvas |

An agent-scoped key writes only its own canvas; an operator with access to the agent may write through the REST routes too. Reads follow the audience rules above. Per-kind limits: Mermaid source up to 20,000 characters, inline images up to 64 KB.

### MCP Tools

- `set_canvas(blocks, canvas_id?, title?, audience?, template?, execution_id?)` — write the whole canvas. Omit `canvas_id` for `main`. Pass `execution_id` and the result says whether the person in this conversation can see it: `visible_to_requester` is `true`, `false` (the audience does not reach them — `visibility_note` says what to do) or `null` (could not be determined).
- `patch_canvas(blocks, canvas_id?)` — replace specific blocks by id, leaving the rest, the title and the audience untouched.
- `get_canvas(canvas_id?)` — read a canvas, ids included, to see what to patch.
- `list_canvases()` — ids, titles, audiences and update times.
- `clear_canvas(canvas_id)` — remove one.

**API Endpoints**: See [Backend API Docs](http://localhost:8000/docs) for full schemas.

## See Also

- [Agent Chat](agent-chat.md) and [Workspace](../sharing-and-access/workspace.md) — where you ask for a canvas, and the rail's Canvas tab
- [Voice Chat](../advanced/voice-chat.md) — the call draws on the same `main` canvas
- [Agent Reports](../operations/agent-reports.md) — the published-once counterpart
