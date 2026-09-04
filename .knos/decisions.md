# Decisions and current work

<!-- Written by `knos export`. Commit this file. -->

<!--
Reading this file needs nothing installed: it is plain markdown, and a fresh
clone picks it up as-is. The live claim/withhold server is a separate, optional
step - `pip install knos` (Python 3.10+), which the MCP entry launches as
`python -m knos.mcp`. Without it, everything below still reads normally.
-->


A second clone reads this on its first question — it is one of the decision
records knos looks for. Nothing here is private: secrets and private paths
never reach it.


## Decisions

- **public repository** — This is a public repo. No credentials, API keys, internal URLs or PII, even in comments. Use placeholders and review `git diff` before every commit.  _(AGENTS.md, CLAUDE.md)_
- **enterprise designs stay private** — Public docs describe the open-core seam only, never the catalog of gated modules. New enterprise design goes in `trinity-enterprise/docs/`, not here. A CI grep-guard flags regressions.  _(CLAUDE.md)_
- **long MCP calls go async** — Claude Code enforces a 60-second timeout on MCP HTTP tool calls, so long tasks call `chat_with_agent` with `async=true, parallel=true` and poll `get_execution_result`.  _(AGENTS.md)_
- **key scope** — Agent-scoped keys see only their permitted agents; user-scoped keys see the owner's agents.  _(AGENTS.md)_
- **new top-level backend module needs a Dockerfile entry** — `docker/backend/Dockerfile` copies top-level `src/backend/*.py` by explicit name, so a new top-level module must be added to the COPY list or it is silently dropped from the image and crashes on deploy.  _(CONTRIBUTING.md)_
- **issues are claimed before work starts** — `/claim` assigns an issue, refuses if it is already assigned, and refuses anything still labelled `status-incubating`.  _(.github/workflows/claim.yml)_

## Being worked on right now

_Nothing claimed._

---
<sub>knos export. Claims lapse after 30 minutes.</sub>
