# Feature: Delivery Conductor Runtime

## Overview

A reusable agent template may include a generic delivery conductor for
long-running work. The conductor is agent-owned: it stores its own orchestration
state and provides policy-independent transition mechanics. The template decides
what the pipeline means, which work is eligible, and which business policy
applies. Trinity stays a substrate for scheduling, events, reminders, execution,
and capability tools; it does not become a workflow backend, DAG executor, or
authoritative conductor-state store.

## Wake to tick flow

1. An adapter receives a schedule, event, reminder, or manual signal and
   normalizes it into a stable wake record.
2. The conductor persists the record in its durable inbox before processing it.
   Inbox delivery is at least once, so duplicate wakes and recovery replay are
   normal inputs rather than exceptional cases. The durable inbox, checkpoints,
   and action-reservation ledger may contain only identifiers, hashes,
   revisions, budgets, and sanitized reason codes. Issue bodies, requirements,
   discovery, and evidence logs are forbidden; they may be referenced only by
   an allowed identifier or hash.
3. A worker claims work under a time-bounded lease. Each successful claim uses
   a monotonically advancing fence token; a stale holder cannot acknowledge,
   checkpoint, reserve an action, or otherwise commit after a newer holder.
4. A tick reads the checkpoint and budget, consumes the allowed wake, and makes
   at most one external effect.
5. Before that effect, the conductor persists an action reservation with a
   stable action identity. Recovery observes or replays that reservation rather
   than creating an equivalent new action.
6. The tick writes its fenced checkpoint and either acknowledges the wake or
   leaves it recoverable for a later holder.

## Checkpoints, budgets, and reminders

Each checkpoint contains only generic runtime fields needed for safe progress:
identifiers, hashes, revisions, budgets, and sanitized reason codes. This
binding allowlist covers the acknowledged inbox position, current fence token,
and action-reservation outcome; it excludes issue bodies, requirements,
discovery, and evidence logs. Budget exhaustion prevents another tick; it never
authorizes hidden background work.

A reminder is a persisted wake source, not a parallel transition engine. On
startup or after a missed interval, the conductor reconciles due reminders into
the same durable inbox. The normal lease, reservation, replay, and budget rules
then apply.

## Adapter and executor isolation

Adapters isolate untrusted input from conductor mechanics. An adapter may use
only its configured read-only observation port and must not directly invoke
mutating network or MCP capabilities. Executors isolate capability invocation
from wake parsing. Both communicate over JSON Lines using versioned, closed
schemas and reject messages above **1 MiB**. Schemas contain only declared typed
fields and references: no arbitrary command, URL, environment, credential, or
file-content fields are permitted. Raw payload/evidence storage is outside this
contract.

## Platform projection

The template can publish a read-only state summary through the established
`~/.trinity/pipeline-state/<pipeline_id>/<instance_id>.json` convention.
Trinity's pipeline tools may inspect that projection for operators, but never
write it, use it to advance a transition, recover the conductor, or treat it as
an authoritative platform workflow record.

## Boundaries

- No Trinity-owned conductor database tables, backend transition logic, or DAG
  executor.
- No product-specific workflow states, tracker conventions, credentials, PII,
  or raw payload/evidence in this generic template contract.
- No extension of Trinity reminder semantics: the conductor consumes the
  existing reminder capability as one possible wake source.

## Related

- [Scheduling requirements](../requirements/scheduling.md#342-agent-owned-delivery-conductor-runtime)
- [Agent-Defined Pipelines](../requirements/scheduling.md#34-agent-defined-pipelines-919)
- [Target architecture](../../planning/TARGET_ARCHITECTURE.md#agent-owned-delivery-conductor-runtime)
