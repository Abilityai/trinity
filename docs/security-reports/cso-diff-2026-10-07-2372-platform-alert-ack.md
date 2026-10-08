# CSO diff audit — #2372 platform-alert acknowledgement

**Mode:** `--diff`, daily (8/10 gate). Branch `feature/2372-ack-platform-alerts` vs `dev`, diffed from merge-base `a51549772`.
**Result:** 0 findings.

## Scope
Changed files: `services/ask_service.py`, `services/operator_queue_service.py`, `db/operator_queue.py`, `database.py`, `db/migrations.py`, Alembic `0093_platform_alert_responded_heal`, plus tests and docs. The diff adds no endpoint, dependency, workflow, Dockerfile, compose change, credential or template.

## Checks
| Area | Check | Verdict |
|---|---|---|
| A01 / ASI03 | Who may answer is unchanged: `reject_non_person_principal` (route) and `may_end` (gate approvals) run before the compare-and-set. The new `terminal` flag only changes the status the answer lands in. | clean |
| ASI03 | Can an agent get `terminal` applied to its own ask? `terminal` depends on `is_platform_minted`. File ingestion refuses reserved ids (`operator_queue_service.py:2024`), and native `ask_operator` refuses them too (`ask_service.py:690` `reserved_request_id`). | clean |
| #1631 guard | A reserved-prefix file entry that is no longer pending is now skipped without the WARNING. Only a pending entry can pre-create and suppress a platform row, and that case still WARNs and `continue`s. The skip happens before the acknowledged branch and the open-row reconcile, so a non-pending entry has no effect on any row. A missing `status` defaults to `pending`, so the uncertain case takes the warning path. | clean (the only change is to log output, which is excluded as log spoofing) |
| A05 | Heal SQL is parameterized (`:now`, `:id`). Prefix matching is done in Python, which avoids the `LIKE` wildcard in `val_`. The UPDATE is a compare-and-set on `status='responded'`. | clean |
| Data safety | The heal flips only rows that are `responded` and carry a reserved prefix. The write-back has excluded those rows since ent#499, so no agent was waiting on their delivery. Downgrade is a no-op. | clean |
| A10 | `terminal` defaults to `False`, so on failure the answer keeps the existing behaviour. The guard's failure direction is stated in its comment. | clean |
| Phase 2 | Added lines scanned for secret prefixes and email addresses: none. | clean |
| Phase 8 | Invisible/bidi Unicode grep over every changed and new file: none. | clean |

## Hypotheses
none

## Coverage gaps
- No Docker pass. A diff run is never a full run, and the diff touches no image.
- The heal was not executed on PostgreSQL locally. CI `pg-migrations` covers it.
- Independent verifier not spawned: there were no findings to verify.
