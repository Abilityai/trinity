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
- **The `canvas` skill.** Agents with the `canvas` skill from the skills library have the full reference and worked examples; any agent gets the essentials from its platform prompt.
- **Markdown and raw HTML.** In a `markdown` block, kit markup must not contain blank lines (Markdown treats a blank line as the end of the HTML). For a fully custom layout the agent uses an `html` block.

## Security

All canvas content is sanitised before it renders. Scripts never execute, `<style>` tags are removed everywhere, and on a canvas only the design kit's classes and a bounded `width` / `max-width` survive — so a canvas cannot restyle the page around it, on your screen or a customer's.

## Related

- [Agent Chat](agent-chat.md) — where you ask for a canvas
- [Workspace](../sharing-and-access/workspace.md) — where a shared canvas appears for clients
- [Voice Chat](../advanced/voice-chat.md) — the voice panel draws on the same canvas
