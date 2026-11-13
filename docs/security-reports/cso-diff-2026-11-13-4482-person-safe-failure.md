# CSO diff audit — #4482 person-safe failure text

- Date: 2026-11-13 · Mode: daily (8/10 gate), `--diff` · Base: `6bbff0d87` · PARTIAL (no Docker; none needed)
- Scope: `services/channel_completion_report.py`, `client_portal/work/service.py`, `client_portal/service.py`, tests, feature-flow docs

## Result

No findings at or above the gate. The change narrows disclosure: the subscription walk trail, subscription names and operator guidance no longer reach Workspace threads, Slack/Telegram completion reports, the Work card, or the own-turn switch message.

## Checks

| Area | Check | Result |
|---|---|---|
| Secrets (P2) | Credential-prefix and IP/email grep over the added lines | Test placeholders only; the test token is assembled at runtime |
| Disclosure (LLM02, A01) | Every non-success completion report and the Work card take `person_safe_failure` | Yes; success text still runs `_sanitized_detail` |
| Failure direction (A10) | `person_safe_failure` on an import or classifier error | Fails to the generic sentence, never raw text; stated in code; pinned by test |
| Output handling (LLM05) | Refusal text passed through | Sanitized and capped (2800 report, 200 Work card) |
| Auth (A01) | New or changed routes | None |
| Guard tests | `test_2320`, `test_186` | 72 passed |

## Hypotheses (not counted)

- The refusal pass-through matches marker substrings anywhere in the error. An agent-server error containing a marker phrase is shown in full, sanitized and capped. The matcher is the one public links already use. Confidence 4/10.

## Coverage gaps

- Docker not running: no image-internal checks. The diff touches no image.
- No dependency scanner run. The diff changes no dependency.
