# CSO diff audit: #3625 pull-queue wording

- **Version**: cso 1.2.3, daily mode, `--diff`
- **Branch**: `feature/3625-pull-queue-wording` (PR #4109)
- **Totals**: 0 critical, 0 high, 0 medium, 0 low

## Scope
9 files: three backend services (`capacity_manager`, `chat_execution_service`, `dispatch_admission_service`), the MCP `chat.ts` receipt lead plus its test, three user docs, and one unit test. The change is response and log text plus a boolean on an internal dataclass.

## Checks
- **Secrets (P2)**: `git log -p` over the branch shows no known credential prefixes. The added log lines carry only agent name and execution id, as before.
- **Agent-context ingress (P1/P7)**: none added. The 202 message and the MCP receipt lead are platform-authored text, so ASI01 framing does not apply.
- **Access control (P9 A01)**: no route, dependency or gate changed. `/task` keeps its existing agent-access dependency.
- **Failure direction (A10)**: `AcquireResult.pull` defaults to `False`, so an unset flag yields the push wording. No guard reads it.
- **Concealed Unicode (P8)**: none in the diff.
- **Enterprise disclosure**: the doc-guard pattern finds nothing in the added doc lines.

## Below the gate
The pull message tells a caller who already has access to the agent that it is a pull pilot. `status: queued` already implied this, and pilot membership is non-secret operator config. Confidence 3/10, not reported.

## Coverage gaps
- Phases 3-6 did not run: no dependency, CI, Docker or webhook files are in the diff.
- No Docker pass was made, because nothing in the diff affects an image.
