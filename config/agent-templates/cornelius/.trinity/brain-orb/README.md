# Brain Orb convention hooks

**Canonical source.** These hooks live in `cornelius-internal` and are published
to the public [`abilityai/cornelius`](https://github.com/Abilityai/cornelius)
template via `/sync-to-public`. Do not edit the public copies directly.

Trinity's agent-server (`docker/base-image/agent_server/routers/brain_orb.py`)
brokers the orb page's requests to these executables. The agent owns all
generation, scope state, and write semantics (Trinity Invariant #8); Trinity
gates upstream (owner-only writes, enum-restricted verbs, flag kill-switches,
timeouts, output caps).

**Contract version: 1** — every hook response carries `"contract_version": 1`.
Requires a Trinity base image with the Phase-4 brain-orb routes (agent-server
≥ the #61 arc, 2026-07).

| File | Exec | Surface | stdin → stdout |
|------|------|---------|----------------|
| `scopes` | GET `/api/brain-orb/scopes` | scope panel | — → `{active, available[{token,label,core?,family?,parent?,kind?}]}` |
| `scope` | POST `/api/brain-orb/scope` (owner) | mount/unmount | `{tokens:[...]}` (or `{mount,unmount}`) → `{ok, active, nodes, edges}`; re-exports `data.json` |
| `search` | POST `/api/brain-orb/tool` | read-only KB search | `{query, limit?}` → `{results:[{id,title,content}], backend}` |
| `action` | GET `/actions`, POST `/action`, POST `/refresh` (owner, write-flag) | KB writes | `{action: list\|capture\|link\|capture_transcript\|process_transcript\|refresh, ...}` → verb-specific |
| `_common.py` | — | shared helpers | root/scope discovery, state file, exporter runner |
| `state.json` | — | runtime (gitignored) | persisted active scope set |
| `voice-postprocess.json` | — | runtime (gitignored) | `{enabled, prompt}` written by the Brain tab (#73) |

Rules every hook obeys:

- **stdout is exactly one JSON document** — the agent-server json-parses the
  whole stream; progress goes to stderr (or is captured from subprocesses).
- **Script-relative paths only** (`ROOT = parents[2]`) — the same files run in
  an agent container (`/home/developer`) and a local checkout (tests).
- **Fail loud, fail safe** — unknown scope tokens are errors (never a silent
  narrowing); exporter failures leave the previous `data.json` untouched;
  request bodies never form filesystem paths (the search scope comes from
  `state.json`, transcript paths are realpath-confined to `Brain/`).
- **Writes land in the vault** (`Brain/00-Inbox/`) so they git-sync with the
  knowledge base and the exporter sees them natively.

The rendered graph itself is produced by
`resources/agent-visualization/export_data.py` → `data.json` (gitignored; the
public template ships a committed `data.seed.json` that `.trinity/setup.sh`
copies on first boot).
