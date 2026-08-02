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

## Fixture adapter verification

The template ships a deterministic example at
`examples/fixture-adapter.py`. It is verification policy, not a production
adapter. For source-event identifiers ending in a decimal number, even values
produce a no-op and odd values propose one `chat` effect. Re-observing the same
wake produces the same action key and byte-identical decision. The effect
targets the generic `delivery-conductor-fixture-sink` identifier; operators may
record a sanitized fake result without creating or contacting that target.

The adapter is trusted policy code: an operator reviews and installs it; wakes,
checkpoints, and adapter output remain untrusted data and must pass the
runtime's closed schemas. Mode `0444` only protects against accidental writes.
This disposable local procedure verifies the reviewed file's hash immediately
before each tick, but the agent workspace is still owned by `developer`; file
mode alone is not an OS-enforced trust boundary. A production adapter must
instead be baked into an operator-owned image or read-only-mounted outside the
writable workspace. The fixture reads one capped JSON line from stdin and
writes one capped JSON line to stdout. It does not read files or environment
variables, open a network client, invoke MCP, or receive a command, URL, path,
environment, or credential field.

### Generic local deployment

Start Trinity from the checkout being verified. A configured development
instance may use its normal `.env`; do not copy that file into the template or
agent workspace.

```bash
set -euo pipefail
set +x
docker compose up -d --build
curl -fsS http://localhost:8000/health
# -> {"status":"healthy", ...}

TOKEN_RESPONSE=$(python3 -c '
import os, urllib.parse
print(urllib.parse.urlencode({"username": "admin", "password": os.environ["ADMIN_PASSWORD"]}))
' | curl -fsS -X POST --data-binary @- http://localhost:8000/api/token
)
TOKEN=$(printf '%s' "${TOKEN_RESPONSE}" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')
unset TOKEN_RESPONSE ADMIN_PASSWORD

FIXTURE_NAME="conductor-fixture-$(date +%s)-$$"
FIXTURE_CONTAINER="agent-${FIXTURE_NAME}"
if docker container inspect "${FIXTURE_CONTAINER}" >/dev/null 2>&1; then
  echo 'refusing to reuse an existing fixture container' >&2
  exit 64
fi

curl -fsS --config - <<EOF
url = "http://localhost:8000/api/agents"
request = "POST"
header = "Authorization: Bearer ${TOKEN}"
header = "Content-Type: application/json"
data = "{\"name\":\"${FIXTURE_NAME}\",\"template\":\"local:delivery-conductor\"}"
EOF
unset TOKEN

FIXTURE_PATH=config/agent-templates/delivery-conductor/examples/fixture-adapter.py
FIXTURE_SHA256=$(shasum -a 256 "${FIXTURE_PATH}" | cut -d' ' -f1)
docker exec "${FIXTURE_CONTAINER}" test ! -e /home/developer/adapter.py
docker cp "${FIXTURE_PATH}" "${FIXTURE_CONTAINER}:/tmp/fixture-adapter.py"
docker exec -u root "${FIXTURE_CONTAINER}" sh -ceu '
  test ! -e /home/developer/adapter.py
  install -o root -g root -m 0444 /tmp/fixture-adapter.py /home/developer/adapter.py
  rm /tmp/fixture-adapter.py'

verify_fixture() {
  actual=$(docker exec "${FIXTURE_CONTAINER}" \
    sha256sum /home/developer/adapter.py | cut -d' ' -f1)
  test "${actual}" = "${FIXTURE_SHA256}"
}
verify_fixture
```

`local:delivery-conductor` is hidden from the starter catalog but remains
resolvable by exact ID through Trinity's native local-template path. The unique
name, fail-fast shell settings, absent-file check, and hash guard prevent this
verification run from silently reusing or overwriting an existing agent or
adapter. Installing `adapter.py` is an explicit trusted operator action. The
model-facing instructions forbid the model from creating, changing, or
selecting another adapter, but that instruction does not replace production
filesystem isolation.

### Scenario commands

These commands are an operator-only deterministic harness for the deployed
artifact. A normal agent turn receives `TRINITY_EXECUTION_ID` and the execution
context from Trinity; the model must never synthesize or override them.

```bash
run_tick() {
  execution_id="$1"
  triggered_by="$2"
  event_type="$3"
  event_id="$4"
  case "${execution_id}" in
    ''|*[!A-Za-z0-9._:-]*) return 64 ;;
  esac
  case "${triggered_by}:${event_type}:${event_id}" in
    'manual:null:null'|'reminder:null:null'|'schedule:null:null'|
      'event:"agent.task.completed":"worker-1"') ;;
    *) return 64 ;;
  esac
  verify_fixture
  docker exec -i -u developer -w /home/developer \
    -e "TRINITY_EXECUTION_ID=${execution_id}" "${FIXTURE_CONTAINER}" \
    ./bin/conductor-tick <<EOF
{"event_id":${event_id},"event_type":${event_type},"execution_id":"${execution_id}","reminder_message":null,"schema_version":1,"triggered_by":"${triggered_by}"}
EOF
}

# Direct no-op, then its at-least-once duplicate.
run_tick direct-0 manual null null
run_tick direct-0 manual null null

# Ordinary reminder no-op and hourly schedule effect.
run_tick reminder-0 reminder null null
run_tick hourly-1 schedule null null

# Worker completion effect. The backend event ID, not the execution ID,
# distinguishes worker-completion wakes.
run_tick worker-execution-1 event '"agent.task.completed"' '"worker-1"'

# Crash/restart: prepare the odd direct wake, restart before record-result, then
# re-run after the five-minute lease. The second prepare has a higher fence and
# the same action key. Tests may inject a future trusted clock instead of waiting.
run_tick restart-1 manual null null
docker restart "${FIXTURE_CONTAINER}"
sleep 301
run_tick restart-1 manual null null
```

Every `action-ready` or `reminder-ready` response contains one `effect_tool`,
one closed `effect_arguments` object, and one correlation. Record only a
sanitized result, using the returned action key and fence:

```bash
verify_fixture
docker exec -i -u developer -w /home/developer "${FIXTURE_CONTAINER}" \
  ./bin/conductor-tick record-result <<'EOF'
{"action_key":"ACTION_KEY_FROM_PREPARE","fence_token":1,"operation":"record-result","reason_code":"fixture-result-unknown","result_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","schema_version":1,"status":"ambiguous"}
EOF
```

An ambiguous result is terminal for that attempt and schedules investigation;
it is never an immediate second effect. Crash recovery may expose the same
idempotent action key only after the old lease expires and fresh adapter
observation returns the identical action. Duplicate wakes return `not-claimed`,
and exhausted ceilings return `blocked` with no effect tool.

### Observable checks

Inspect only the conductor's sanitized rows. The action query must return zero
duplicate keys, journal fence tokens must be strictly increasing, and each
unique proposed effect has one `action_journal` row. `budget_usage` reaches the
fixture ceilings without authorizing another effect.

```bash
verify_fixture
docker exec -i -u developer -w /home/developer "${FIXTURE_CONTAINER}" \
  python - <<'PY'
import json, sqlite3
with sqlite3.connect('data/delivery-conductor/control.sqlite3') as db:
    fences = [row[0] for row in db.execute(
        'SELECT fence_token FROM action_journal ORDER BY fence_token')]
    duplicate_keys = db.execute(
        'SELECT action_key, COUNT(*) FROM action_journal '
        'GROUP BY action_key HAVING COUNT(*) > 1').fetchall()
    usage = db.execute(
        'SELECT COALESCE(SUM(run_units),0), '
        'COALESCE(SUM(issue_units),0), COALESCE(SUM(daily_units),0) '
        'FROM budget_usage').fetchone()
print(json.dumps({
    'duplicate_action_keys': duplicate_keys,
    'fences_strictly_increase': all(a < b for a, b in zip(fences, fences[1:])),
    'usage': usage,
}, sort_keys=True))
PY

docker exec -u developer -w /home/developer "${FIXTURE_CONTAINER}" \
  python -m json.tool \
  .trinity/pipeline-state/delivery-conductor/current.json
```

The projection contains schema version, controller/checkpoint state, hashes,
budgets, breaker state, identifiers, and sanitized reason codes. It contains no
raw wake message, effect result, discovery, evidence, PII, or credential.

The template requests no GitHub, push, deploy, or administrative credential.
Trinity still injects its own platform model/MCP/agent-auth variables, which are
outside the adapter contract. Verify the source-mode boundary without printing
any value:

```bash
docker exec "${FIXTURE_CONTAINER}" sh -lc '
  test ! -e /home/developer/.env &&
  ! env | cut -d= -f1 | grep -Eq \
    "^(GH_TOKEN|GITHUB_TOKEN|GITLAB_TOKEN|AWS_ACCESS_KEY_ID|DEPLOY_TOKEN|ADMIN_PASSWORD)$"'
# -> exit 0 with no output
```

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
