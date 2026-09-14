"""Workspace asks — the read/answer surface for an agent-initiated ask (ent#364).

**OSS core (ent#428).** This shipped first as the entitled `workspace_asks`
module in the private repo, and that was the wrong edition for the same reason
the Workspace itself (ent#356) and multi-agent rooms (ent#443) were: the
frontend that drives it — `PortalAsks.vue`, `store.fetchAsks()`,
`store.asksAvailable` — ships in **every** build and self-disables on a 404. So
a community install rendered the ask affordance and then refused it, which is an
advert for a missing feature rather than a clean absence. Everything about the
Workspace lives in OSS.

It lives under `client_portal/` rather than as a top-level package because that
is what it is: a client-portal surface, on the client-portal prefix, reading an
OSS table. It owns no table of its own — an ask IS an `operator_queue` row with
an addressee (`addressed_to_email`, ent#364), which is what makes "answering
anywhere clears it everywhere" true by construction instead of by a sync step.
Placing it here also means it inherits `client_portal`'s Dockerfile COPY line
instead of needing its own — the #1033 / ent#356 / ent#443 trap, three times now.

The `/api/enterprise/client-portal/asks` prefix is retained history, exactly like
the `enterprise_`-prefixed portal and room tables: ent#83 published that prefix
as the integration surface and the shipped Vue bundle already calls this URL.
Provenance, not a licensing claim.
"""
