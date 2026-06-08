"""Live-stack integration test for #678 acceptance criteria.

Acceptance #1: failure row carries cost + context (salvaged from the 502
  dict body's partial metadata) instead of null when the reader-race
  signature fires and the retry ALSO fails.

Acceptance #2: retry_count=1 appears on the row when the reader-race
  signature fires and the retry succeeds.

Runs against the live trinity.db inside trinity-backend. Mocks only
`agent_post_with_retry` so the real DB write path, capacity manager,
activity service, and audit log fire as they would in production.
"""
import asyncio
import sys
from unittest.mock import AsyncMock, patch

sys.path.insert(0, "/app")

import httpx
from database import db
from services.task_execution_service import get_task_execution_service
from models import TaskExecutionStatus


class _MockResp:
    """Minimal httpx-shaped response mock."""
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body
        self.text = ""

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            req = httpx.Request("POST", "http://test/api/task")
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}", request=req, response=self
            )


READER_RACE_502 = _MockResp(
    status_code=502,
    body={
        "detail": {
            "message": (
                "Execution completed without a result message after 0 tool calls "
                "/ 2 turns (raw_messages=0 types=<none>, parse_failures=0). "
                "Likely cause: reader thread wedged."
            ),
            "metadata": {
                "cost_usd": 0.05,
                "duration_ms": 12000,
                "num_turns": 2,
                "context_window": 200000,
                "input_tokens": 1500,
                "cache_read_tokens": 100,
                "model_name": "claude-sonnet-4-6",
                "recovered_from_jsonl": True,
            },
            "raw_message_count": 0,
            "parse_failure_count": 0,
            "recovery_attempted": True,
        }
    },
)

SUCCESS_200 = _MockResp(
    status_code=200,
    body={
        "response": "ACCEPTANCE-TEST: retry succeeded with content",
        "session_id": "acceptance-sess-001",
        "metadata": {
            "cost_usd": 0.03,
            "duration_ms": 8000,
            "num_turns": 1,
            "context_window": 200000,
            "input_tokens": 800,
            "cache_read_tokens": 200,
        },
        "execution_log": [],
    },
)


async def _run_with_responses(responses, label):
    """Drive execute_task with a pre-baked response sequence."""
    iter_calls = iter(responses)

    async def fake_agent_post(*args, **kwargs):
        try:
            return next(iter_calls)
        except StopIteration:
            return responses[-1]

    svc = get_task_execution_service()
    with patch(
        "services.task_execution_service.agent_post_with_retry",
        new=fake_agent_post,
    ):
        result = await svc.execute_task(
            agent_name="trinity-system",
            message=f"#678 acceptance test: {label}",
            triggered_by="manual",
        )
    return result


async def acceptance_2_retry_success():
    """Reader-race -> retry succeeds. Row should have retry_count=1,
    status=SUCCESS, cost = 0.03 + 0.05 = 0.08 (rolled-in)."""
    print(f"\n{'=' * 70}")
    print("ACCEPTANCE #2: reader-race signature -> retry succeeds")
    print("=" * 70)

    result = await _run_with_responses(
        [READER_RACE_502, SUCCESS_200], "retry-success"
    )

    row = db.get_execution(result.execution_id)
    print(f"Execution ID:    {result.execution_id}")
    print(f"Result status:   {result.status}")
    print(f"Result cost:     {result.cost}")
    print(f"DB row status:   {row.status}")
    print(f"DB row retry:    {row.retry_count}")
    print(f"DB row cost:     {row.cost}")
    print(f"Expected:        status=success, retry_count=1, cost=0.08")
    print()

    ok = (
        result.status == TaskExecutionStatus.SUCCESS
        and row.retry_count == 1
        and abs((row.cost or 0) - 0.08) < 1e-6
    )
    print(f"VERDICT: {'PASS' if ok else 'FAIL'}")
    return ok, result.execution_id


async def acceptance_1_salvage_on_double_failure():
    """Reader-race -> retry also fails. Row should still carry cost +
    context salvaged from the FIRST 502 dict body, NOT null."""
    print(f"\n{'=' * 70}")
    print("ACCEPTANCE #1: salvage cost + context on double-failure (no null)")
    print("=" * 70)

    result = await _run_with_responses(
        [READER_RACE_502, READER_RACE_502], "salvage-only"
    )

    row = db.get_execution(result.execution_id)
    print(f"Execution ID:    {result.execution_id}")
    print(f"Result status:   {result.status}")
    print(f"Result cost:     {result.cost}")
    print(f"Result ctx_used: {result.context_used}")
    print(f"DB row status:   {row.status}")
    print(f"DB row retry:    {row.retry_count}")
    print(f"DB row cost:     {row.cost}")
    print(f"DB row ctx_used: {row.context_used}")
    print(
        f"Expected:        status=failed, retry_count=1, "
        f"cost>0 (salvaged + rolled-in), context_used>0"
    )
    print()

    ok = (
        result.status == TaskExecutionStatus.FAILED
        and row.retry_count == 1
        and (row.cost or 0) > 0
        and (row.context_used or 0) > 0
    )
    print(f"VERDICT: {'PASS' if ok else 'FAIL'}")
    return ok, result.execution_id


async def main():
    a2_ok, a2_id = await acceptance_2_retry_success()
    a1_ok, a1_id = await acceptance_1_salvage_on_double_failure()

    print(f"\n{'=' * 70}")
    print("SUMMARY")
    print("=" * 70)
    print(f"Acceptance #2 (retry success):  {'PASS' if a2_ok else 'FAIL'}  ({a2_id})")
    print(f"Acceptance #1 (salvage cost):   {'PASS' if a1_ok else 'FAIL'}  ({a1_id})")
    return 0 if (a1_ok and a2_ok) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
