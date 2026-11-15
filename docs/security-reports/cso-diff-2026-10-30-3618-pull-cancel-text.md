# CSO diff audit: #3618 pull cancel text

cso v1.2.3 · daily mode · `--diff` · 2026-10-30

## Scope
- `pull_worker.py`: a terminated pull turn sets `content` to "Execution terminated by user" when the body is not `success`.
- `pull_coordination_service.handle_task_result`: a CANCELLED row stores the constant "Execution terminated by user" as `error`. `response` keeps the scrubbed, sanitized `content`.
- Two unit tests and one learnings fragment.

## Checks
| Area | Result |
|---|---|
| Secrets (diff) | Prefix and keyword grep over added lines: no hits. |
| Invisible Unicode (diff + fragment) | No hits. |
| Supply chain / CI / Docker / compose | Not touched by the diff. |
| Auth (A01, ASI03, ASI10) | Result route auth is unchanged (`internal.py`: internal secret or the agent's own key owning the execution). Row status is decided before the new branch. An `auth` result labelled `cancelled` still becomes FAILED. |
| Injection / output (A05, LLM05) | The new `error` value is a constant. `content` still passes the ent#279 identity scrub and `sanitize_response` before storage. |
| Failure direction (A10) | No new guard and no exception handling added. |
| Agent-context ingress (ASI01) | None added. |

## Findings
None.

## Dropped candidate
An agent can label any result `cancelled`, which now forces the cancel text into `error`. Before this change, the agent's own `content` landed there. The agent already controls the label and the text, and only `auth` is guarded at this boundary. That guard is unchanged.

## Coverage gaps
None for the diff scope. No Docker pass was needed: no image, pin or compose file changed.
