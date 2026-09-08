# Agent Canvas

A **canvas** is a surface an agent keeps *current* — a status board, a running tally, the latest version of an analysis, the chart you just asked for. Reports are the other half: published once, they accumulate as a record. A canvas is rewritten in place.

Every agent has a **Canvas** tab on its detail page. A canvas the agent marks as **shared** also appears on the agent's Workspace page and in the conversation rail for the people the agent works with.

## Concepts

- **Canvas** — A named surface (`main` by default) the agent writes with `set_canvas` and updates with `patch_canvas`. The header always shows when it was last updated, and adds **may be out of date** when the agent has finished work since without refreshing it.
- **Blocks** — A canvas is an ordered list of blocks: `kpi`, `table`, `chart`, `timeline`, `markdown`, `html`, `image`, `diagram`, `json`. Data blocks are drawn by Trinity from the data the agent provides — the agent never ships scripts, and none run.
- **Design kit** — A small set of platform-owned styles (`ck-card`, `ck-grid-2/3/4`, `ck-section`, `ck-callout`, `ck-chip`, `ck-kpi`, `ck-table`, `ck-figure`) that make an agent's own `html` and `markdown` blocks look designed in light and dark without the agent touching CSS. Anything outside the kit — other classes, inline styles beyond a bounded width, `<style>` tags — is dropped before it renders.
- **Starter layouts** — A canvas can declare a `template`: **dashboard**, **report**, **brief** or **status-board**. Each has named slots (a dashboard has `header`, `kpis`, `main`, `side`, `footer`) that blocks fill. A layout never hides a block: anything not slotted renders after the layout, and with no template blocks simply stack.

## How It Works

1. Ask the agent in chat: "Put a dashboard of this week's pipeline on your canvas."
2. The agent writes its canvas — a layout, KPI tiles, a chart, a short callout — and tells you.
3. Open the agent's **Canvas** tab (or, for a shared canvas, the Workspace page or the rail's Canvas tab). Narrow screens and the rail collapse layouts to one column.
4. Ask for changes the same way. The agent updates only the blocks that changed; the header timestamp moves.

## Layouts at a Glance

| Template | Slots, in order |
|---|---|
| `dashboard` | header · kpis · main + side · footer |
| `report` | header · summary · body · figures · appendix |
| `brief` | header · key-points + body |
| `status-board` | header · status · issues + next · log |

## Tips

- **Prefer data blocks.** Ask for "a KPI row" or "a table of open items" and the agent uses the `kpi` / `table` kinds, which render with the same look as the kit and stay live-updatable.
- **Shared or private.** A canvas is private to the operator unless the agent publishes it to its roster. Ask the agent to "share this canvas with the team" and it appears on the Workspace page.
- **The `canvas` skill.** Agents with the `canvas` skill from the skills library have the full reference and worked examples; any agent gets the essentials from its platform prompt — including when to *retire* a canvas rather than add another.
- **Markdown and raw HTML.** In a `markdown` block, kit markup must not contain blank lines (Markdown treats a blank line as the end of the HTML). For a fully custom layout the agent uses an `html` block.

## Sharing a canvas, and saving it as a PDF

**Share** produces a link to the canvas. You choose who it reaches, and the
narrower option is preselected:

- **People who already have access** (the default) — opening the link requires
  signing in, and only people who can already see the agent will see the canvas.
- **Anyone with the link** — no sign-in at all. The dialog says so plainly,
  because it is the option that reaches further than the canvas did before.

A shared canvas **stays current**: whoever opens the link sees it as the agent
updates it, not a copy from when you shared it. The page says this, and shows
the last-updated time and the "may be out of date" mark. **Revoke** turns a link
off; anyone holding it is told it was turned off rather than getting a broken
page. Creating and revoking links is recorded in the audit log.

**PDF** is available from every canvas surface. It uses your browser's own
print-to-PDF, so what you get matches what you see: the same typography, charts
and diagrams, one clean column, and no block split across a page break. The PDF
carries the canvas title, the agent and the date, and is always the light
rendering even if you work in dark mode. If your browser cannot do it, the
button says so and tells you to use Print → Save as PDF.

## Removing canvases

An agent that uses its canvas well accumulates them — one per report, per topic,
per run. You can clear the pile from either the **Canvas tab** on Agent Detail or
the **Canvas tab in the Workspace rail**.

- **Delete one**, or switch on **Manage** to select several and delete them in
  one action. You are asked to confirm once, and the confirmation names how many
  will go. Deleting is recorded in the audit log.
- **Only the agent's owner (or an admin) can delete or pin.** If you can see an
  agent's canvases but do not own the agent, there are no delete controls —
  rather than buttons that would refuse.
- **Pin** the ones you use daily and they stay at the top as the list grows. A
  pin is yours, not the agent's: the agent cannot pin its own canvas, and
  rewriting a canvas does not un-pin it.
- **Search** appears once there are more than six, matching the title or the id.
  Each row in Manage shows how old it is and whether it may be out of date.
- **Deleting the default canvas is fine.** The agent recreates it the next time
  it writes; you lose the contents, not the surface.

**There is a limit.** Each agent can hold 100 canvases. At the limit the agent
can still *update* everything it has, but creating a *new* one is refused with a
message telling it to retire one first — nothing is ever deleted automatically to
make room. If you see an agent bumping into this, it is usually writing a new
canvas per run instead of keeping one per topic current; asking it to reuse a
canvas, or clearing the finished ones, fixes it.

## Security

All canvas content is sanitised before it renders. Scripts never execute, `<style>` tags are removed everywhere, and on a canvas only the design kit's classes and a bounded `width` / `max-width` survive — so a canvas cannot restyle the page around it, on your screen or a customer's.

## Related

- [Agent Chat](agent-chat.md) — where you ask for a canvas
- [Workspace](../sharing-and-access/workspace.md) — where a shared canvas appears for clients
- [Voice Chat](../advanced/voice-chat.md) — the voice panel draws on the same canvas
