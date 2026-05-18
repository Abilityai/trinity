# Sync and Network Alert Runbook

Use this runbook for `sync_failing` operator-queue items and monitoring alerts that report an agent as network-unreachable.

## Evidence Collection

Public repository safety comes first. Do not paste secrets, access tokens, API keys, real user data, private prompts, private file contents, or sensitive logs into issues, pull requests, docs, screenshots, or chat transcripts. When sharing evidence, redact values and use placeholders such as `AGENT_NAME`, `your-token`, `user@example.com`, and `your-domain.com`.

Record the alert source before changing anything:

- Agent name and alert type: `sync_failing`, network-unreachable, high CPU, or backup-health.
- Timestamp, current status, and whether the alert is new or stale.
- Whether the agent is actively executing work in the UI on `/executions` or through `GET /api/agents/AGENT_NAME/executions`.
- Latest health data from `GET /api/monitoring/agents/AGENT_NAME`.
- Fleet or container CPU from `GET /api/telemetry/containers` or `docker stats --no-stream agent-AGENT_NAME`.
- Recent schedule executions for the agent, especially entries with `triggered_by: "schedule"` or repeated failures.

For `sync_failing`, collect git-specific evidence:

```bash
docker exec agent-AGENT_NAME git -C /home/developer status --short
docker exec agent-AGENT_NAME git -C /home/developer status --branch --porcelain=v2
docker exec agent-AGENT_NAME test -e /home/developer/.git/index.lock
docker exec agent-AGENT_NAME stat -c '%y %n' /home/developer/.git/index.lock
docker exec agent-AGENT_NAME sh -lc "ps -eo pid,etime,comm | grep '[g]it'"
```

If the agent is an older workspace, repeat git checks against `/home/developer/workspace` only after `/home/developer` is not the git worktree.

For network-unreachable alerts, collect reachability evidence:

```bash
docker ps --filter name=agent-AGENT_NAME
docker inspect --format '{{.State.Status}} restart_count={{.RestartCount}} oom={{.State.OOMKilled}} networks={{range $name, $_ := .NetworkSettings.Networks}}{{$name}} {{end}}' agent-AGENT_NAME
docker exec agent-AGENT_NAME curl -fsS http://127.0.0.1:8000/health
curl -fsS -H "Authorization: Bearer ${TRINITY_TOKEN}" http://localhost:8000/api/monitoring/agents/AGENT_NAME
```

If CPU is high, identify whether the load is tied to active or repeated schedule executions before restarting anything:

```bash
curl -fsS -H "Authorization: Bearer ${TRINITY_TOKEN}" http://localhost:8000/api/telemetry/containers
curl -fsS -H "Authorization: Bearer ${TRINITY_TOKEN}" http://localhost:8000/api/agents/AGENT_NAME/executions
```

## Safe Remediation

Remediate the smallest confirmed cause. Do not acknowledge or dismiss the alert until verification shows the cause is gone.

### Stale Git Lock

Only treat `.git/index.lock` as stale when all of these are true:

- The lock file exists.
- No live git process is operating in the same worktree.
- The lock file age is longer than the configured stale threshold or clearly predates the current incident.
- There is no active execution that could be running git.

Safe path:

1. Confirm no live git process with `ps -eo pid,etime,comm | grep '[g]it'` inside the agent container.
2. Confirm the worktree path with `git -C /home/developer rev-parse --show-toplevel`.
3. Preserve evidence of the lock age without copying sensitive file contents.
4. Remove only the lock file, not the repository or working tree.
5. Trigger or wait for the next sync attempt.

Do not remove a lock while a git process is alive. Back off and let the process finish, or stop the owning execution if it is hung and safe to terminate.

### Network-Unreachable Agent

Use this order:

1. Confirm the container exists and is expected to be running.
2. Check local health inside the container at `http://127.0.0.1:8000/health`.
3. Compare backend monitoring data with direct container evidence.
4. Check Docker network membership and recent restart count.
5. Check CPU and memory. High CPU can make a healthy agent look unreachable.
6. Check recent schedule executions for repeated or long-running jobs.

Safe path:

- If the container is stopped and the agent should be online, start the agent through the UI or the normal lifecycle command.
- If local health works inside the container but backend monitoring cannot reach it, investigate Docker networking before restarting the agent.
- If high CPU correlates with a non-critical schedule, pause or disable that schedule first, then let the agent recover.
- If the agent process is wedged, no important execution is running, and local health fails, restart only `agent-AGENT_NAME`.

Restart criteria:

- Restart a single agent only after evidence shows the container process is unhealthy, unreachable, or pinned, and no active execution should be preserved.
- Restart the backend only when multiple agents show the same monitoring failure and direct container health suggests the agents themselves are reachable.
- Do not restart the full fleet for a single-agent `sync_failing` or network-unreachable alert.

### Schedule Execution Load

When schedule executions are part of the incident:

1. Identify the schedule or task that produced the recent execution series.
2. Check whether failures repeat across retries or across every schedule tick.
3. Pause the schedule if it is saturating CPU, creating stuck executions, or repeatedly triggering network-unreachable alerts.
4. Keep manual and unrelated schedules running when possible.
5. Re-enable the schedule only after a clean manual execution or a corrected task definition.

## Verification

Verify with fresh evidence after remediation:

- `GET /api/monitoring/agents/AGENT_NAME` shows network reachable, expected Docker status, acceptable CPU, and no current network-unreachable issue.
- `GET /api/agents/sync-health` shows `last_sync_status: "success"` or `consecutive_failures: 0` for the affected agent.
- `GET /api/agents/AGENT_NAME/executions` shows no unexpected long-running or repeatedly failing schedule executions.
- `GET /api/monitoring/cleanup-status` shows cleanup service running and no new incident-related orphan or stale execution spike.
- The Operating Room item is acknowledged only after the live state is clean, not just after a restart.

If verification fails, return to evidence collection. Do not add code changes until repo source has been tied to the observed runtime failure.

## Backblaze and Backup-Health Alerts

Backblaze-related alerts are separate from the resolved git-lock or network-unreachable path above. If an old alert mentions `/host-backblaze` or `backup-health`, first verify whether the runtime actually mounts and uses that path.

Repo triage steps:

1. Search repo-managed docs and config for `Backblaze`, `backblaze`, `/host-backblaze`, `host-backblaze`, and `backup-health`.
2. If no repo source defines the mount or alert, do not invent a code fix in Trinity.
3. Inspect the live runtime configuration, container mounts, and host mount state before acknowledging the alert.
4. Treat unresolved mount configuration as an environment issue until the runtime source of truth is identified.

Do not mark old backup-health alerts as resolved just because git sync and network alerts were fixed. The backup-health path must be verified independently.

## See Also

- [Monitoring](monitoring.md)
- [Operating Room](operating-room.md)
- [Executions](executions.md)
