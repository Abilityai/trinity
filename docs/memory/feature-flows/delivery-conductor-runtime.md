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
adapter. For synthetic source-event identifiers ending in a decimal number,
even values produce a no-op and odd values propose one `chat` effect.
Re-observing the same wake produces the same action key and byte-identical
decision. Those synthetic effects target the generic
`delivery-conductor-fixture-sink` identifier and are settled with explicitly
simulated results to exercise durability only. A real backend-generated UUID
from a normal model-mediated chat turn instead proposes one far-future self
reminder; the deployed verification below invokes that actual Trinity effect.

The adapter is trusted policy code: an operator reviews and installs it; wakes,
checkpoints, and adapter output remain untrusted data and must pass the
runtime's closed schemas. The runtime starts the fixed adapter with Python's
isolated-path mode and a scrubbed environment, so modules and startup hooks in
the writable workspace are not import candidates. That import isolation does
not sandbox the trusted adapter itself. Mode `0444` only protects against
accidental writes.
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
set -Eeuo pipefail
set +x
PHASE=initialize
trap 'status=$?; echo "fixture-failed-phase=${PHASE} status=${status}" >&2' ERR
RUN_SUFFIX="$(date +%s)-$$"
FIXTURE_PROJECT="conductor-fixture-${RUN_SUFFIX}"
MAIN_NAME="conductor-fixture-main-${RUN_SUFFIX}"
REPLAY_NAME="conductor-fixture-replay-${RUN_SUFFIX}"
ACTUAL_NAME="conductor-fixture-actual-${RUN_SUFFIX}"
MAIN_CONTAINER="agent-${MAIN_NAME}"
REPLAY_CONTAINER="agent-${REPLAY_NAME}"
ACTUAL_CONTAINER="agent-${ACTUAL_NAME}"
TRINITY_ENV_FILE="${TRINITY_ENV_FILE:-.env}"
# Compose is invoked directly here rather than through start.sh, so detect the
# Docker socket's container-visible group for the non-root backend.
DOCKER_GID=$(docker run --rm \
  -v /var/run/docker.sock:/var/run/docker.sock:ro \
  alpine:3.22@sha256:14358309a308569c32bdc37e2e0e9694be33a9d99e68afb0f5ff33cc1f695dce \
  stat -c '%g' /var/run/docker.sock | tr -dc '0-9')
case "${DOCKER_GID}" in ''|*[!0-9]*) exit 64 ;; esac
export DOCKER_GID
COMPOSE=(docker compose --project-name "${FIXTURE_PROJECT}" --env-file "${TRINITY_ENV_FILE}")
MAIN_CREATED=0
REPLAY_CREATED=0
ACTUAL_CREATED=0
SYSTEM_CREATED=1

if docker ps -a --format '{{.Names}}' | grep -Eq '^(trinity-|agent-trinity-system$)'; then
  echo 'run this isolated verification without an existing Trinity stack' >&2
  exit 64
fi
if docker network inspect trinity-agent-network >/dev/null 2>&1; then
  echo 'refusing to reuse an existing Trinity agent network' >&2
  exit 64
fi
if docker volume inspect agent-trinity-system-workspace >/dev/null 2>&1; then
  echo 'refusing to reuse an existing Trinity system-agent workspace' >&2
  exit 64
fi
for resource in "${MAIN_CONTAINER}" "${REPLAY_CONTAINER}" "${ACTUAL_CONTAINER}"; do
  if docker container inspect "${resource}" >/dev/null 2>&1; then
    echo 'refusing to reuse an existing fixture container' >&2
    exit 64
  fi
done
for resource in "${MAIN_CONTAINER}-workspace" "${REPLAY_CONTAINER}-workspace" "${ACTUAL_CONTAINER}-workspace"; do
  if docker volume inspect "${resource}" >/dev/null 2>&1; then
    echo 'refusing to reuse an existing fixture workspace' >&2
    exit 64
  fi
done
CAPTURE_DIR=$(mktemp -d)
chmod 0700 "${CAPTURE_DIR}"

delete_agent() {
  name="$1"
  curl -fsS --config - >/dev/null <<EOF
url = "http://localhost:8000/api/agents/${name}"
request = "DELETE"
header = "Authorization: Bearer ${TOKEN}"
EOF
}

cleanup() {
  exit_status=$?
  set +e
  if [ "${MAIN_CREATED}" = 1 ] && [ -n "${TOKEN:-}" ]; then delete_agent "${MAIN_NAME}"; fi
  if [ "${REPLAY_CREATED}" = 1 ] && [ -n "${TOKEN:-}" ]; then delete_agent "${REPLAY_NAME}"; fi
  if [ "${ACTUAL_CREATED}" = 1 ] && [ -n "${TOKEN:-}" ]; then delete_agent "${ACTUAL_NAME}"; fi
  if [ "${MAIN_CREATED}" = 1 ]; then
    docker container rm -f "${MAIN_CONTAINER}" >/dev/null 2>&1
    docker volume rm "${MAIN_CONTAINER}-workspace" >/dev/null 2>&1
  fi
  if [ "${REPLAY_CREATED}" = 1 ]; then
    docker container rm -f "${REPLAY_CONTAINER}" >/dev/null 2>&1
    docker volume rm "${REPLAY_CONTAINER}-workspace" >/dev/null 2>&1
  fi
  if [ "${ACTUAL_CREATED}" = 1 ]; then
    docker container rm -f "${ACTUAL_CONTAINER}" >/dev/null 2>&1
    docker volume rm "${ACTUAL_CONTAINER}-workspace" >/dev/null 2>&1
  fi
  if [ "${SYSTEM_CREATED}" = 1 ]; then
    docker container rm -f agent-trinity-system >/dev/null 2>&1
    docker volume rm agent-trinity-system-workspace >/dev/null 2>&1
  fi
  "${COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1
  find "${CAPTURE_DIR}" -type f -delete
  rmdir "${CAPTURE_DIR}" >/dev/null 2>&1
  unset TOKEN ADMIN_USERNAME ADMIN_PASSWORD AGENT_AUTH_SECRET
  set -e
  return "${exit_status}"
}
trap cleanup EXIT

# Override any configured development login with a strong, disposable local
# credential. Values stay in the shell/container environment and stdin only.
export ADMIN_USERNAME=admin
export ADMIN_PASSWORD='Aa1!'"$(openssl rand -hex 24)"  # pragma: allowlist secret
export AGENT_AUTH_SECRET="${AGENT_AUTH_SECRET:-$(openssl rand -hex 32)}"
PHASE=stack
"${COMPOSE[@]}" up -d --build
for attempt in $(seq 1 60); do
  if curl -fsS http://localhost:8000/health >/dev/null; then break; fi
  sleep 1
done
curl -fsS http://localhost:8000/health
# -> {"status":"healthy", ...}
echo fixture-stack-healthy

# Provision the disposable local admin from container environment references,
# then close first-time setup. No credential value enters the command line.
"${COMPOSE[@]}" exec -T backend python -c \
  'import os; from database import db; from dependencies import authenticate_user, hash_password; assert db.update_user_password(os.environ["ADMIN_USERNAME"], hash_password(os.environ["ADMIN_PASSWORD"])); db.set_setting("setup_completed", "true"); assert authenticate_user(os.environ["ADMIN_USERNAME"], os.environ["ADMIN_PASSWORD"])' \
  </dev/null
echo fixture-setup-ready

PHASE=authenticate
TOKEN_RESPONSE=$(curl -fsS --config - <<EOF
url = "http://localhost:8000/api/token"
request = "POST"
data-urlencode = "username=${ADMIN_USERNAME}"
data-urlencode = "password=${ADMIN_PASSWORD}"
EOF
)
TOKEN=$(printf '%s' "${TOKEN_RESPONSE}" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')
unset TOKEN_RESPONSE ADMIN_USERNAME ADMIN_PASSWORD
echo fixture-auth-ready

create_agent() {
  name="$1"
  for attempt in $(seq 1 60); do
    if response=$(curl -fsS --config - <<EOF
url = "http://localhost:8000/api/agents"
request = "POST"
header = "Authorization: Bearer ${TOKEN}"
header = "Content-Type: application/json"
data = "{\"name\":\"${name}\",\"template\":\"local:delivery-conductor\"}"
EOF
    ); then
      printf '%s' "${response}" | python3 -c \
        'import json,sys; value=json.load(sys.stdin); assert isinstance(value, dict)'
      unset response
      return 0
    fi
    # A committed POST may lose its response. Accept only this exact agent via
    # the authenticated native API; otherwise wait for startup readiness.
    if response=$(curl -fsS --config - <<EOF
url = "http://localhost:8000/api/agents/${name}"
header = "Authorization: Bearer ${TOKEN}"
EOF
    ); then
      printf '%s' "${response}" | python3 -c \
        'import json,sys; value=json.load(sys.stdin); assert isinstance(value, dict)'
      unset response
      return 0
    fi
    unset response
    sleep 2
  done
  return 1
}

PHASE=create-agents
MAIN_CREATED=1
create_agent "${MAIN_NAME}"
REPLAY_CREATED=1
create_agent "${REPLAY_NAME}"
ACTUAL_CREATED=1
create_agent "${ACTUAL_NAME}"
echo fixture-agents-created

FIXTURE_PATH=config/agent-templates/delivery-conductor/examples/fixture-adapter.py
FIXTURE_SHA256=$(shasum -a 256 "${FIXTURE_PATH}" | cut -d' ' -f1)
install_fixture() {
  container="$1"
  for attempt in $(seq 1 30); do
    if docker exec "${container}" \
      test -f /home/developer/examples/fixture-adapter.py >/dev/null 2>&1; then break; fi
    sleep 1
  done
  docker exec -u root -e "FIXTURE_SHA256=${FIXTURE_SHA256}" \
    "${container}" sh -ceu '
    test ! -e /home/developer/adapter.py
    actual=$(sha256sum /home/developer/examples/fixture-adapter.py | cut -d" " -f1)
    test "${actual}" = "${FIXTURE_SHA256}"
    install -o root -g root -m 0444 \
      /home/developer/examples/fixture-adapter.py /home/developer/adapter.py'
}

verify_fixture() {
  container="$1"
  actual=$(docker exec "${container}" \
    sha256sum /home/developer/adapter.py | cut -d' ' -f1)
  test "${actual}" = "${FIXTURE_SHA256}"
}
PHASE=install-adapters
install_fixture "${MAIN_CONTAINER}"
install_fixture "${REPLAY_CONTAINER}"
install_fixture "${ACTUAL_CONTAINER}"
verify_fixture "${MAIN_CONTAINER}"
verify_fixture "${REPLAY_CONTAINER}"
verify_fixture "${ACTUAL_CONTAINER}"
echo fixture-adapters-verified
PHASE=scenarios
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

First run one actual effect through a normal model-mediated agent turn. The
fixture recognizes only Trinity's backend-generated UUID execution identity for
this path and proposes a far-future `set_reminder` effect. The agent must call
the exact returned tool and arguments once and record only its sanitized result.
The subsequent two native create requests replay the exact tool-side payload;
Trinity's reminder idempotency gate must return the original reminder rather
than creating another effect.

```bash
PHASE=actual-model-effect
python3 - "${CAPTURE_DIR}/actual-chat-request.json" <<'PY'
import json, pathlib, sys
pathlib.Path(sys.argv[1]).write_text(json.dumps({
    "message": (
        "Execute exactly one delivery-conductor turn using only the trusted "
        "Execution Context and the installed template protocol. Invoke the "
        "single returned effect if allowed, record its sanitized result, then stop."
    )
}, separators=(",", ":")))
PY
chmod 0600 "${CAPTURE_DIR}/actual-chat-request.json"
ACTUAL_CHAT_STATUS=
for attempt in $(seq 1 60); do
  if ACTUAL_CHAT_STATUS=$(curl -sS --max-time 300 --config - \
    --output "${CAPTURE_DIR}/actual-chat-response.json" \
    --write-out '%{http_code}' \
    --data-binary "@${CAPTURE_DIR}/actual-chat-request.json" <<EOF
url = "http://localhost:8000/api/agents/${ACTUAL_NAME}/chat"
request = "POST"
header = "Authorization: Bearer ${TOKEN}"
header = "Content-Type: application/json"
header = "Idempotency-Key: fixture-actual-${RUN_SUFFIX}"
EOF
  ); then
    if [ "${ACTUAL_CHAT_STATUS}" = 200 ]; then break; fi
    case "${ACTUAL_CHAT_STATUS}" in 409|429|503) ;; *) exit 22 ;; esac
  fi
  sleep 2
done
test "${ACTUAL_CHAT_STATUS}" = 200
unset ACTUAL_CHAT_STATUS

# Reconstruct only the exact sanitized effect arguments from the agent-owned
# ledger. A completed reminder reservation proves the model called the normal
# Trinity tool and then correlated record-result before its turn returned.
docker exec -i -u developer -w /home/developer "${ACTUAL_CONTAINER}" \
  python - >"${CAPTURE_DIR}/actual-reminder-arguments.json" <<'PY'
import hashlib, json, sqlite3
with sqlite3.connect('data/delivery-conductor/control.sqlite3') as db:
    rows = db.execute(
        "SELECT action_key, payload_json, status FROM action_journal "
        "WHERE capability_name = 'reminders'"
    ).fetchall()
    assert len(rows) == 1, rows
    action_key, payload_json, status = rows[0]
    assert status == 'completed', rows[0]
    receipt = db.execute(
        'SELECT reminder_due_at_utc FROM conductor_cli_receipts '
        'WHERE action_key = ?', (action_key,)
    ).fetchone()
    assert receipt is not None and receipt[0]
references = json.loads(payload_json)
message = json.dumps({
    'action_key': action_key,
    'payload_sha256': hashlib.sha256(payload_json.encode()).hexdigest(),
    'references': references,
}, ensure_ascii=True, separators=(',', ':'), sort_keys=True, allow_nan=False)
print(json.dumps({'fire_at': receipt[0], 'message': message}, separators=(',', ':')))
PY

# The effect must already exist before any operator replay. This check prevents
# the replay request itself from manufacturing the evidence it is meant to test.
curl -fsS --config - >"${CAPTURE_DIR}/actual-reminders-before-replay.json" <<EOF
url = "http://localhost:8000/api/agents/${ACTUAL_NAME}/reminders"
header = "Authorization: Bearer ${TOKEN}"
EOF
python3 - \
  "${CAPTURE_DIR}/actual-reminder-arguments.json" \
  "${CAPTURE_DIR}/actual-reminders-before-replay.json" \
  >"${CAPTURE_DIR}/actual-reminder-original-id" <<'PY'
import json, pathlib, sys
from datetime import datetime, timezone

def instant(value):
    parsed = datetime.fromisoformat(value[:-1] + '+00:00' if value.endswith('Z') else value)
    return parsed.astimezone(timezone.utc)

arguments, reminders = (
    json.loads(pathlib.Path(path).read_text()) for path in sys.argv[1:]
)
matching = [item for item in reminders
            if item['message'] == arguments['message']
            and instant(item['fire_at']) == instant(arguments['fire_at'])]
assert len(matching) == 1, matching
print(matching[0]['id'])
PY

for replay in 1 2; do
  curl -fsS --config - \
    --data-binary "@${CAPTURE_DIR}/actual-reminder-arguments.json" \
    >"${CAPTURE_DIR}/actual-reminder-replay-${replay}.json" <<EOF
url = "http://localhost:8000/api/agents/${ACTUAL_NAME}/reminders"
request = "POST"
header = "Authorization: Bearer ${TOKEN}"
header = "Content-Type: application/json"
EOF
done
curl -fsS --config - >"${CAPTURE_DIR}/actual-reminders.json" <<EOF
url = "http://localhost:8000/api/agents/${ACTUAL_NAME}/reminders"
header = "Authorization: Bearer ${TOKEN}"
EOF
python3 - \
  "${CAPTURE_DIR}/actual-reminder-arguments.json" \
  "${CAPTURE_DIR}/actual-reminder-replay-1.json" \
  "${CAPTURE_DIR}/actual-reminder-replay-2.json" \
  "${CAPTURE_DIR}/actual-reminders.json" \
  "${CAPTURE_DIR}/actual-reminder-original-id" <<'PY'
import json, pathlib, sys
from datetime import datetime, timezone

def instant(value):
    parsed = datetime.fromisoformat(value[:-1] + '+00:00' if value.endswith('Z') else value)
    return parsed.astimezone(timezone.utc)

arguments, first, second, reminders = (
    json.loads(pathlib.Path(path).read_text()) for path in sys.argv[1:5]
)
original_id = pathlib.Path(sys.argv[5]).read_text().strip()
assert original_id
assert first['id'] == second['id'] == original_id
matching = [item for item in reminders
            if item['message'] == arguments['message']
            and instant(item['fire_at']) == instant(arguments['fire_at'])]
assert len(matching) == 1, matching
assert matching[0]['id'] == first['id']
print(json.dumps({
    'actual_effect': 'set_reminder',
    'duplicate_effects': 0,
    'recorded_result': 'completed',
    'tool_replay_same_id': True,
}, sort_keys=True))
PY
```

The commands below are a separate operator-only deterministic durability
harness for the deployed artifact. They intentionally do not invoke their
synthetic `chat` effects. A normal agent turn receives `TRINITY_EXECUTION_ID`
and the execution context from Trinity; the model must never synthesize or
override them.

```bash
assert_status() {
  file="$1"
  expected="$2"
  python3 - "${file}" "${expected}" <<'PY'
import json, pathlib, sys
value = json.loads(pathlib.Path(sys.argv[1]).read_text())
assert value["schema_version"] == 1
assert value["status"] == sys.argv[2], value
print(json.dumps({"reason_code": value["reason_code"], "status": value["status"]}, sort_keys=True))
PY
}

assert_blocked_no_effect() {
  file="$1"
  python3 - "${file}" <<'PY'
import json, pathlib, sys
value = json.loads(pathlib.Path(sys.argv[1]).read_text())
assert value["schema_version"] == 1
assert value["operation"] == "prepare"
assert value["status"] == "blocked"
assert value["reason_code"] == "breaker-open"
for key in ("effect_tool", "effect_arguments", "action", "correlation", "reminder"):
    assert value[key] is None, key
print(json.dumps({"no_effect": True, "reason_code": value["reason_code"], "status": value["status"]}, sort_keys=True))
PY
}

prepare_tick() {
  container="$1"
  execution_id="$2"
  triggered_by="$3"
  event_type="$4"
  event_id="$5"
  output_file="$6"
  request=$(python3 - "${execution_id}" "${triggered_by}" "${event_type}" "${event_id}" <<'PY'
import json, re, sys
execution_id, triggered_by, event_type, event_id = sys.argv[1:]
assert re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}", execution_id)
assert triggered_by in {"manual", "reminder", "schedule", "event"}
event_type = None if event_type == "-" else event_type
event_id = None if event_id == "-" else event_id
assert (event_type, event_id) in {(None, None), ("agent.task.completed", "worker-1")}
print(json.dumps({
    "event_id": event_id,
    "event_type": event_type,
    "execution_id": execution_id,
    "reminder_message": None,
    "schema_version": 1,
    "triggered_by": triggered_by,
}, separators=(",", ":"), sort_keys=True))
PY
  )
  verify_fixture "${container}"
  printf '%s\n' "${request}" | docker exec -i -u developer \
    -w /home/developer -e "TRINITY_EXECUTION_ID=${execution_id}" \
    "${container}" ./bin/conductor-tick >"${output_file}"
  unset request
}

record_fixture_result() {
  container="$1"
  prepare_file="$2"
  result_status="$3"
  expected_status="$4"
  output_file="$5"
  request=$(python3 - "${prepare_file}" "${result_status}" <<'PY'
import hashlib, json, pathlib, sys
prepared = json.loads(pathlib.Path(sys.argv[1]).read_text())
result_status = sys.argv[2]
assert prepared["status"] in {"action-ready", "reminder-ready"}
assert prepared["effect_tool"] in {
    "mcp__trinity__chat_with_agent",
    "mcp__trinity__set_reminder",
}
arguments = prepared["effect_arguments"]
assert isinstance(arguments, dict)
expected = (
    {"agent_name", "message"}
    if prepared["effect_tool"] == "mcp__trinity__chat_with_agent"
    else {"fire_at", "message"}
)
assert set(arguments) == expected
correlation = prepared["correlation"]
assert set(correlation) == {"action_key", "fence_token"}
assert prepared["action"]["action_key"] == correlation["action_key"]
assert isinstance(correlation["fence_token"], int) and correlation["fence_token"] > 0
assert result_status in {"completed", "ambiguous"}
simulated = json.dumps({
    "effect_arguments": arguments,
    "effect_tool": prepared["effect_tool"],
    "simulated": True,
}, separators=(",", ":"), sort_keys=True).encode()
print(json.dumps({
    "action_key": correlation["action_key"],
    "fence_token": correlation["fence_token"],
    "operation": "record-result",
    "reason_code": (
        "fixture-simulated-success"
        if result_status == "completed"
        else "fixture-result-unknown"
    ),
    "result_sha256": hashlib.sha256(simulated).hexdigest(),
    "schema_version": 1,
    "status": result_status,
}, separators=(",", ":"), sort_keys=True))
PY
  )
  printf '%s\n' "${request}" | docker exec -i -u developer \
    -w /home/developer "${container}" \
    ./bin/conductor-tick record-result >"${output_file}"
  unset request
  assert_status "${output_file}" "${expected_status}"
}

# Main agent: every prepared effect is correlated and terminal before advancing.
prepare_tick "${MAIN_CONTAINER}" direct-0 manual - - "${CAPTURE_DIR}/direct.json"
assert_status "${CAPTURE_DIR}/direct.json" noop
prepare_tick "${MAIN_CONTAINER}" direct-0 manual - - "${CAPTURE_DIR}/duplicate.json"
assert_status "${CAPTURE_DIR}/duplicate.json" not-claimed
prepare_tick "${MAIN_CONTAINER}" reminder-0 reminder - - "${CAPTURE_DIR}/reminder.json"
assert_status "${CAPTURE_DIR}/reminder.json" noop

prepare_tick "${MAIN_CONTAINER}" hourly-1 schedule - - "${CAPTURE_DIR}/hourly.json"
assert_status "${CAPTURE_DIR}/hourly.json" action-ready
record_fixture_result "${MAIN_CONTAINER}" "${CAPTURE_DIR}/hourly.json" \
  completed completed "${CAPTURE_DIR}/hourly-result.json"

prepare_tick "${MAIN_CONTAINER}" worker-execution-1 event \
  agent.task.completed worker-1 "${CAPTURE_DIR}/worker.json"
assert_status "${CAPTURE_DIR}/worker.json" action-ready
record_fixture_result "${MAIN_CONTAINER}" "${CAPTURE_DIR}/worker.json" \
  completed completed "${CAPTURE_DIR}/worker-result.json"

# Two more odd source-event IDs deterministically propose unique effects under
# distinct fixture signatures. Settle each captured correlation before the next
# wake so the shared issue usage reaches exactly four units.
prepare_tick "${MAIN_CONTAINER}" direct-3 manual - - "${CAPTURE_DIR}/budget-manual.json"
assert_status "${CAPTURE_DIR}/budget-manual.json" action-ready
record_fixture_result "${MAIN_CONTAINER}" "${CAPTURE_DIR}/budget-manual.json" \
  completed completed "${CAPTURE_DIR}/budget-manual-result.json"

prepare_tick "${MAIN_CONTAINER}" reminder-3 reminder - - "${CAPTURE_DIR}/budget-reminder.json"
assert_status "${CAPTURE_DIR}/budget-reminder.json" action-ready
record_fixture_result "${MAIN_CONTAINER}" "${CAPTURE_DIR}/budget-reminder.json" \
  completed completed "${CAPTURE_DIR}/budget-reminder-result.json"

# A fresh odd schedule wake observes the reconciled four-unit issue ceiling.
# It returns no executable handoff; the durable checks below prove why.
prepare_tick "${MAIN_CONTAINER}" hourly-3 schedule - - "${CAPTURE_DIR}/budget-blocked.json"
assert_blocked_no_effect "${CAPTURE_DIR}/budget-blocked.json"

# Replay agent: restart with the first correlation intentionally unresolved.
prepare_tick "${REPLAY_CONTAINER}" restart-1 manual - - "${CAPTURE_DIR}/restart-before.json"
assert_status "${CAPTURE_DIR}/restart-before.json" action-ready
docker restart "${REPLAY_CONTAINER}" >/dev/null
sleep 301
prepare_tick "${REPLAY_CONTAINER}" restart-1 manual - - "${CAPTURE_DIR}/restart-after.json"
assert_status "${CAPTURE_DIR}/restart-after.json" action-ready
python3 - "${CAPTURE_DIR}/restart-before.json" "${CAPTURE_DIR}/restart-after.json" <<'PY'
import json, pathlib, sys
before, after = (json.loads(pathlib.Path(path).read_text()) for path in sys.argv[1:])
assert before["correlation"]["action_key"] == after["correlation"]["action_key"]
assert before["correlation"]["fence_token"] < after["correlation"]["fence_token"]
print(json.dumps({"fence_increased": True, "same_action_key": True}, sort_keys=True))
PY
record_fixture_result "${REPLAY_CONTAINER}" "${CAPTURE_DIR}/restart-after.json" \
  ambiguous investigate "${CAPTURE_DIR}/restart-result.json"
```

An ambiguous result is terminal for that attempt and schedules investigation;
the future investigation reminder is not an immediate second effect. The
recovery replay shown above occurs **before** any ambiguous result is recorded:
only an unresolved reservation may expose the same idempotent action key after
the old lease expires. Once ambiguity is durably recorded, that attempt is not
replayed. Duplicate wakes return `not-claimed`. On the main agent, four
synthetically settled reservations consume exactly the fixture's four-unit
issue ceiling; the fresh fifth proposal returns `blocked`/`breaker-open` with
every effect and correlation field null. The deterministic helper does not
contact the fake target: it validates one exact tool/argument bundle, hashes
that explicitly simulated outcome, and records only the captured correlation.
The separate normal chat procedure above is the observable proof of one actual
Trinity effect and tool-side replay deduplication.

### Observable checks

Inspect only the conductor's sanitized rows. The action query must return zero
duplicate keys. Fence tokens from the append-only `action_events` table must be
monotonic in primary-key insertion order, rather than sorted by the value being
tested. A replay may reserve its investigation reminder at the current fence,
while the explicit restart comparison above proves the source action's fence
increased. Main-agent ledger usage must be exactly `[4, 4, 4]`: this reaches the
adapter's issue ceiling of four (not its daily ceiling of eight), opens the
durable breaker with `issue-cost-budget-exhausted`, and authorizes no fifth
effect.

```bash
inspect_agent() {
  container="$1"
  expected_usage="$2"
  expected_breaker_state="$3"
  expected_breaker_reason="$4"
  verify_fixture "${container}"
  docker exec -i -u developer -w /home/developer "${container}" \
    python - "${expected_usage}" "${expected_breaker_state}" \
    "${expected_breaker_reason}" <<'PY'
import json, sqlite3, sys
expected_usage = tuple(int(value) for value in sys.argv[1].split(','))
expected_breaker = (sys.argv[2], sys.argv[3])
with sqlite3.connect('data/delivery-conductor/control.sqlite3') as db:
    fences = [row[0] for row in db.execute(
        'SELECT fence_token FROM action_events ORDER BY id')]
    duplicate_keys = db.execute(
        'SELECT action_key, COUNT(*) FROM action_journal '
        'GROUP BY action_key HAVING COUNT(*) > 1').fetchall()
    usage = db.execute(
        'SELECT COALESCE(SUM(run_units),0), '
        'COALESCE(SUM(issue_units),0), COALESCE(SUM(daily_units),0) '
        'FROM budget_usage').fetchone()
    breaker = db.execute(
        'SELECT current.state, current.reason_code '
        'FROM run_checkpoint AS checkpoint '
        'JOIN conductor_wake_scope AS scope '
        'ON scope.wake_id = checkpoint.acknowledged_wake_id '
        'JOIN conductor_breaker_current AS current '
        'ON current.run_id = scope.run_id '
        'AND current.issue_id = scope.issue_id '
        'AND current.signature = scope.signature '
        'WHERE checkpoint.singleton = 1').fetchone()
    checkpoint = db.execute(
        'SELECT action_key, reason_code FROM run_checkpoint WHERE singleton = 1'
    ).fetchone()
    unsettled_prior = db.execute(
        'SELECT action_key FROM action_journal '
        'WHERE status = \'reserved\' AND action_key <> COALESCE(?, \'\')',
        (checkpoint[0],),
    ).fetchall()
monotonic = all(a <= b for a, b in zip(fences, fences[1:]))
assert duplicate_keys == []
assert monotonic
assert usage == expected_usage, (usage, expected_usage)
assert breaker == expected_breaker, (breaker, expected_breaker)
assert unsettled_prior == [], unsettled_prior
print(json.dumps({
    'breaker': breaker,
    'duplicate_action_keys': duplicate_keys,
    'event_fences_monotonic': monotonic,
    'prior_actions_settled': True,
    'usage': usage,
}, sort_keys=True))
PY
  docker exec -u developer -w /home/developer "${container}" \
    python -m json.tool \
    .trinity/pipeline-state/delivery-conductor/current.json
}

inspect_agent "${MAIN_CONTAINER}" 4,4,4 open issue-cost-budget-exhausted
inspect_agent "${REPLAY_CONTAINER}" 1,1,1 open attempt-budget-exhausted
```

The projection contains schema version, controller/checkpoint state, hashes,
budgets, breaker state, identifiers, and sanitized reason codes. It contains no
raw wake message, effect result, discovery, evidence, PII, or credential.

The template requests no GitHub, push, deploy, or administrative credential.
Trinity still injects its own platform model/MCP/agent-auth variables, which are
outside the adapter contract. Verify the source-mode boundary without printing
any value:

```bash
for container in "${MAIN_CONTAINER}" "${REPLAY_CONTAINER}" "${ACTUAL_CONTAINER}"; do
  docker exec "${container}" sh -lc '
    test ! -e /home/developer/.env &&
    ! env | cut -d= -f1 | grep -Eq \
      "^(GH_TOKEN|GITHUB_TOKEN|GITLAB_TOKEN|AWS_ACCESS_KEY_ID|DEPLOY_TOKEN|ADMIN_PASSWORD)$"'
done
# -> exit 0 with no output

# Delete through the native API first, then remove only the exact resources
# created by this disposable run. Disable the trap after successful cleanup.
cleanup
trap - EXIT
test -z "$(docker ps -aq --filter "label=com.docker.compose.project=${FIXTURE_PROJECT}")"
test -z "$(docker volume ls -q --filter "label=com.docker.compose.project=${FIXTURE_PROJECT}")"
for resource in "${MAIN_CONTAINER}" "${REPLAY_CONTAINER}" "${ACTUAL_CONTAINER}" \
  agent-trinity-system; do
  ! docker container inspect "${resource}" >/dev/null 2>&1
done
for resource in "${MAIN_CONTAINER}-workspace" "${REPLAY_CONTAINER}-workspace" \
  "${ACTUAL_CONTAINER}-workspace" agent-trinity-system-workspace; do
  ! docker volume inspect "${resource}" >/dev/null 2>&1
done
! docker network inspect trinity-agent-network >/dev/null 2>&1
echo fixture-cleanup-verified
# -> fixture-cleanup-verified
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
