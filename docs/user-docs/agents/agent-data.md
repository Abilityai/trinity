# Agent Data & Portability

Declare which directories hold an agent's runtime data, keep that data out of git, and export or import it on demand to move an agent between Trinity instances.

## Concepts

An agent's home directory, `/home/developer`, is a durable named Docker volume. It already survives the events that would otherwise wipe a container's filesystem:

- Container recreate (resource, capability, or mount changes)
- Image upgrade
- Template re-pull (the platform resets tracked files to `origin/main`; it never wipes untracked files)
- Subscription auto-switch

So runtime data an agent writes under `data/` — SQLite databases, datasets, generated artifacts — is **already durable** today. There is no separate data volume to provision and no platform schema change involved.

Two problems remain, and this feature solves both:

- **Keeping data out of git.** Runtime data does not belong in the git-synced template repo, where it would bloat the history. Declaring `data_paths` in `template.yaml` adds those paths to the agent's own `.gitignore` automatically.
- **Portability.** To move an agent to another Trinity instance, you need a way to capture and restore its data. Export/import does this on demand as a tar archive.

## How It Works

### Declaring data paths

Add `data_paths` to the agent's `template.yaml`. Each entry is a glob under `data/`:

```yaml
data_paths:
  - data/*.db
  - data/datasets/**
```

When the agent is created, Trinity:

1. Records the declaration in `~/.trinity/data-paths.yaml`.
2. Appends `data/` plus your declared entries to the agent's own `.gitignore`, so the runtime data is never committed.

The append is idempotent and touches only the agent's own `.gitignore` — not any fleet-wide ignore rules. Declaring no `data_paths` is a no-op.

### Exporting data

Export captures everything under `/home/developer/data` as a tar archive with a `manifest.json` (declared data paths plus agent and version metadata). Run it on demand against a **running** agent.

The platform never mounts the agent workspace to do this — it reads the directory out of the container directly, streams it to a temporary file, and returns it as a download.

### Importing data

Import restores a previously exported archive into the target agent's `data/` directory. Restore is allowlisted to `data/**` and guarded against path traversal — entries that escape `data/` (for example `..` or absolute paths) are skipped and reported, never written. Import returns a count of restored and skipped entries.

To move an agent between instances: export from the source agent, recreate the agent on the destination, then import the archive.

## For Agents

All endpoints are owner/admin only. A per-agent operation lock serialises export and import so two operations on the same agent cannot run at once.

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/agents/{name}/data/export` | POST | Export `data/` as a tar archive. Query `?format=stream` (default download) or `?format=base64` (inline JSON `{tar_base64}`). |
| `/api/agents/{name}/data/import` | POST | Import a tar archive into `data/`. Returns `{restored, skipped}`. Accepts `Idempotency-Key`. |

Status codes:

- `409` — export of a stopped agent (no running container), or a concurrent export/import on the same agent.
- `413` — the data exceeds the export size cap (`AGENT_DATA_EXPORT_MAX_BYTES`); for `?format=base64`, the response directs you to the streaming download instead.

See [Backend API Docs](http://localhost:8000/docs) for full request/response schemas.

### MCP Tools

| Tool | Description |
|------|-------------|
| `export_agent_data({ name })` | Export the agent's `data/` directory. |
| `import_agent_data({ name, tar_base64 })` | Restore a base64-encoded tar archive into the agent's `data/`. |

### Template Fields

| Field | Description |
|-------|-------------|
| `data_paths` | List of globs under `data/` that hold runtime data. Auto-appended to the agent's `.gitignore`; surfaces in export manifests. Default: empty. |

## Limitations

The following are planned (PR2) and not yet available:

- **Scheduled background snapshots with retention.** Export/import is on demand only today.
- **SQLite-quiesce pre-snapshot hook.** A `~/.trinity/pre-snapshot` hook to produce a consistent point-in-time copy of a live SQLite database (currently an export is a plain filesystem read).
- **Rename/purge snapshot cascade.** Snapshot directories are not yet cleaned up when an agent is renamed or purged.
- **System-agent `data_paths`.** System agents are out of scope.

## See Also

- [Agent Files](agent-files.md)
- [Managing Agents](managing-agents.md)
- [GitHub Sync](../integrations/github-sync.md)
- [MCP Server](../integrations/mcp-server.md)
