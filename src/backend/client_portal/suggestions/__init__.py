"""Workspace suggestions — what you can do with this agent, and what is waiting (trinity-enterprise#465).

A short list computed for ONE viewer and ONE agent from signals the platform
already holds: questions and decisions waiting on the viewer, the health of the
agent's schedules, how long since the viewer last talked to it, and the
playbooks it exposes that the viewer has never started. Each item is an
actionable object — Accept (prefill / open / deep link) or Dismiss — and shows
the signal it came from. Requirement: `docs/memory/requirements/core-agent.md`
§5.39; flow: `docs/memory/feature-flows/workspace-suggestions.md`.

It lives under `client_portal/` beside `work/` and `asks/` for the reasons
those packages give: a client-portal surface on the client-portal prefix that
inherits `client_portal`'s Dockerfile COPY line (the #1033 trap), and not in
`service.py`, the codebase's top churn × complexity hotspot.

**Platform-authenticated door only.** ent#78's auth-path invariant: usage- and
schedule-derived data is internal, so a verified-email portal token gets a
uniform 404 from the router before any read. `configure` items additionally
require owner or admin — the audience that may act on a schedule.

**OSS core by decision** — no `requires_entitlement`. The Workspace is OSS core
(ent#356), and ent#465 is a Workspace capability.
"""
