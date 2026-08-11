# Dashboard

The main Dashboard at `/` monitors all agents and their activities in real time. Switch between three view modes with the toggle in the top-right — **Timeline**, **Grid**, and **List**. Timeline is the default; your choice persists per browser in `localStorage['trinity-dashboard-view']`. (A previously saved mode that no longer exists — such as the retired `graph` view — falls back to the default.)

The Dashboard is also where you create agents: **Create Agent** sits in the header and is available in every view mode.

> 📺 **Watch:** [The Multi-Agent Platform I Run My Company On](https://youtu.be/8j6q-kABRqc) *(May 2026)* · [all videos](../videos.md)

## Concepts

| Term | Meaning |
|------|---------|
| **View mode** | Timeline, Grid, or List — three renderings of the same fleet. Switching modes never refetches or resets your filters. |
| **Department** | A visual grouping of agents on the Grid canvas, backed by a `dept-<name>` tag. |
| **Reporting line** | An arrow between two tiles on the Grid, backed by a `reports-to-<agent>` tag on the reporting agent. |

## How It Works

### Filtering (all three modes)

Filters live in the Dashboard header and apply to every view mode:

- **Type-to-filter** — Press `/` anywhere on the page and start typing to narrow the fleet by name. Matching is a case-insensitive substring over both the agent slug and its display name. Press `Esc` to clear. This filter is an accelerator, not a saved preference — it is never persisted and clears when you leave the page.
- **Quick tag filter** — Narrow to one or more tags.
- **Owner filter** — Narrow to agents owned by a particular user.
- **Time range** — 1h, 6h, 24h, 7d, or custom (Timeline).

When a type-to-filter query matches nothing, the Dashboard tells you so explicitly rather than showing an empty fleet.

### Timeline View (default)

![Trinity Dashboard — Timeline view showing live executions across oracle and market agents](../../screenshots/dashboard-timeline.png)

1. Execution boxes per agent, arranged chronologically.
2. Color-coded by trigger type: Manual (green), MCP (pink), Scheduled (purple), Agent-Triggered (cyan), Paid (yellow), Public (teal).
3. Each row shows the agent's completion rate, total cost, and parallel slot count.
4. Live streaming: running executions show progress in real time with a "Live" indicator.
5. **Active only** toggle hides agents with no recent activity.
6. **Jump to Now** snaps the view to the current time.

Agent-to-agent collaboration is surfaced here — via the Agent-Triggered trigger type — rather than as a live node graph.

### Grid View

![Trinity Dashboard — Grid view showing the fleet as a canvas of agent tiles with activity sparklines, success rates, cost, and inline Run/Auto toggles](../../screenshots/dashboard-grid.png)

A magnetic tile canvas — the fleet as a grid of agent cards. Each agent is a five-zone tile showing its avatar, runtime badge, and inline **Running** and **Autonomy** toggles, plus live status chips (git sync health, pending operator-queue items).

1. Drag a tile to move it; drop it onto another tile to **swap** positions. The layout snaps to an unbounded lattice and is saved per user.
2. **Tidy** re-packs the tiles into a compact arrangement without losing your ordering.
3. **Reset** restores the default auto-generated layout.
4. Pan by dragging the background; zoom with the scroll wheel or pinch. Tiles are keyboard-navigable.
5. Tile metrics hydrate lazily as they scroll into view, so large fleets stay responsive.

#### Org overlay — departments and reporting lines

The Grid can render an organizational layer on top of the same lattice, so a fleet reads like an org chart instead of a flat pile of tiles.

- **Departments** are drawn as labelled zones around their member tiles. Each department gets its own color, and members carry a matching ribbon on their tile.
- **Reporting lines** are drawn as arrows between tiles. The arrow points from the reporting agent to the one it reports to.
- **Assign by drag** — drop a tile into a zone to move it into that department, or use **New department** mode to create one and assign members.
- **Draw a line** — drag from a tile's connect port onto another tile to create a reporting line. A live pill previews the relationship before you drop.
- **Move a department** — drag its zone header to relocate the whole group.
- Every org change surfaces a canvas toast with **Undo**.

Both are stored as ordinary agent tags — `dept-<name>` for departments, `reports-to-<agent>` on the *reporting* agent for lines — so nothing new is persisted and you can inspect or bulk-edit them from the tag surfaces. Zones are derived from where tiles already sit; they never constrain your layout. If an agent is renamed, its reporting references follow; if it is permanently purged, dangling references are cleaned up.

Org tags are **human-only**: agent-scoped API keys cannot add or remove them.

### List View

The former standalone Agents page, folded into the Dashboard as a third mode (`/agents` now redirects here).

1. One row per agent: name, status, tags, runtime, read-only state, and last activity.
2. **Run** and **Autonomy** toggles inline on each row.
3. Sort by name, status, or activity; filter by name and status. These two filters persist per browser.
4. Select multiple rows for bulk tag operations.
5. Three responsive layouts — the row list reflows down to mobile widths.
6. System agents pin to the top and hide the Run toggle.

Tag and owner filtering use the shared Dashboard header controls; **Clear all** clears both the row-level and header-level filters at once.

### Tag Clouds

Agents are grouped visually by tags on the Dashboard. Click a tag cloud to filter the view to that group.

### Activity Feed

A real-time WebSocket-driven activity stream showing agent collaborations, task starts/completions, schedule executions, and errors.

### Fleet stats bar

The header carries live fleet telemetry (agent counts, running executions, cost). On narrow viewports it degrades gracefully — dropping the least important readouts — rather than clipping.

## For Agents

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/agents` | GET | List all agents (carries `tags`, `read_only_enabled`, and `display_label` per row) |
| `/api/agents/context-stats` | GET | Context and activity state for all agents |
| `/api/agents/autonomy-status` | GET | Autonomy status for all agents |
| `/api/activities/timeline` | GET | Cross-agent activity timeline (filterable) |
| `/api/agents/{name}/tags` | PUT | Set an agent's full tag list — including `dept-*` and `reports-to-*` org tags. Rejected for agent-scoped keys. |
| `/api/telemetry/host` | GET | Host CPU/memory/disk |

**API Endpoints**: See [Backend API Docs](http://localhost:8000/docs) for full schemas.

## Limitations

- The org overlay renders departments as hulls around wherever tiles already sit — it does not auto-arrange your fleet into an org tree (though **Tidy** groups by department when the overlay is active).
- Reporting lines to an agent that is not currently placed on the canvas are skipped rather than drawn to an off-screen point.
- The live agent-to-agent node graph was retired; collaboration is visible through the Timeline instead.

## See Also

- [Managing Agents](../agents/managing-agents.md)
- [Tags and Organization](../sharing-and-access/tags-and-organization.md) — the tag model behind departments and reporting lines
- [Scheduling](../automation/scheduling.md)
- [Operations Page](operating-room.md) — Operator queue, health, and fleet executions
- [Executions](executions.md) — Fleet execution list and completion metrics
