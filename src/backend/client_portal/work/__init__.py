"""Workspace work — what the agents in a chat are doing for you (trinity-enterprise#525).

The visual half of ent#457: the live execution card under the message that
started a job, and the rail's **Work** tab (ent#472 / ent#474) — *Now*,
*Earlier* and *Waiting on you*. The report-back contract itself (ent#457 AC 3,
abilityai/trinity#2386) lives in `services/channel_completion_report.py`; this
package only READS the ledger those executions land in.

It lives under `client_portal/` beside `asks/` for the same reasons that
package gives: it is a client-portal surface on the client-portal prefix, it
owns no table (an item IS a `schedule_executions` row, projected), and it
inherits `client_portal`'s Dockerfile COPY line (the #1033 / ent#356 / ent#443
trap). Not in `service.py` — that file is the codebase's third-largest
churn × complexity hotspot, and a sibling module is the asks precedent.

**Platform-authenticated door only.** ent#78's auth-path invariant, restated by
the 2026-09-06 ruling: the Workspace audience is the instance's internal users,
and an internal-only surface is unreachable via the verified-email path. A
portal-token principal gets a uniform 404 from the router before any read.

**OSS core by decision (ent#525): deliberately ungated** — no
`requires_entitlement`, logic stays in the OSS tree. Recorded explicitly
because CLAUDE.md's default for an enterprise-tracker feature is *gated unless
ruled otherwise*, so the ruling must never be inferred later from the mere
fact that it merged; it inherits ent#356's move of the whole client-portal
surface into OSS core and ent#474/#475's rail.
"""
