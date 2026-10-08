"""The chain report — one verdict per chain (trinity-enterprise#794).

Pure: no pytest, no network. The conftest hands it what happened; it decides
what to call it. Kept apart so the rules that decide "passed" are proven by a
unit test (`tests/unit/test_ent794_chain_report.py`) rather than by a run.

Four verdicts, and the one rule they exist for — **a chain that did not run is
never reported as passed**:

* ``passed``   — every step ran and passed, and at least one step ran;
* ``failed``   — a step raised; the report names that step;
* ``not_run``  — the chain could not start (no model key, no test repo, a
                 blocking issue) — the reason is stated;
* ``partial``  — everything that ran passed, but a named step could not run.
                 Not a pass: the AC asks for the whole chain.

A test that passes having recorded no step at all is reported ``failed``: a
chain with nothing to show proved nothing, and calling it green is the #2029
class this tier exists to prevent.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from typing import Iterator, List, Optional

VERDICTS = ("passed", "failed", "not_run", "partial")

#: The prefix every chain skip carries, so a reader of a raw pytest log can tell
#: "this chain could not run" from any other skip.
NOT_RUN_PREFIX = "chain not run: "


@dataclass
class StepRecord:
    name: str
    status: str = "running"          # running | passed | failed | not_run
    detail: Optional[str] = None


@dataclass
class ChainRun:
    """The steps one chain walked, in order."""

    chain_id: str
    title: str
    steps: List[StepRecord] = field(default_factory=list)

    @contextmanager
    def step(self, name: str) -> Iterator[StepRecord]:
        """A step that must pass. An exception marks it failed and propagates,
        so the test fails at the step that broke."""
        rec = StepRecord(name)
        self.steps.append(rec)
        try:
            yield rec
        except BaseException as e:  # noqa: BLE001 — record, then re-raise unchanged
            # A skip raised mid-chain (a precondition found missing inside a
            # step) is "could not run", not a failure — pytest's Skipped is
            # matched by name so this module stays pytest-free.
            if type(e).__name__ == "Skipped":
                rec.status, rec.detail = "not_run", str(e).split(NOT_RUN_PREFIX, 1)[-1]
            else:
                rec.status, rec.detail = "failed", _short(e)
            raise
        else:
            rec.status = "passed"

    def not_run_step(self, name: str, reason: str) -> None:
        """Record a step that this chain cannot run on this build — it makes the
        chain ``partial``, never ``passed``."""
        self.steps.append(StepRecord(name, "not_run", reason))


def _short(e: BaseException, limit: int = 300) -> str:
    text = f"{type(e).__name__}: {e}".strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def verdict(run: ChainRun, *, outcome: str, skip_reason: Optional[str] = None,
            error: Optional[str] = None) -> dict:
    """The one verdict for a chain.

    ``outcome`` is pytest's: ``passed`` | ``failed`` | ``skipped``. ``error`` is
    the failure text when the test failed outside any step (setup, teardown).
    """
    failed = next((s for s in run.steps if s.status == "failed"), None)
    not_run = [s for s in run.steps if s.status == "not_run"]
    base = {"chain": run.chain_id, "title": run.title,
            "steps": [asdict(s) for s in run.steps]}

    if outcome == "skipped":
        reason = (skip_reason or "").split(NOT_RUN_PREFIX, 1)[-1].strip() or "skipped"
        return {**base, "verdict": "not_run", "step": None, "reason": reason}
    if outcome == "failed" or failed is not None:
        if failed is not None:
            return {**base, "verdict": "failed", "step": failed.name, "reason": failed.detail}
        return {**base, "verdict": "failed", "step": None,
                "reason": error or "failed outside any step"}
    if not run.steps:
        return {**base, "verdict": "failed", "step": None,
                "reason": "the chain recorded no step, so it proved nothing"}
    if not_run:
        return {**base, "verdict": "partial", "step": not_run[0].name,
                "reason": not_run[0].detail}
    return {**base, "verdict": "passed", "step": None, "reason": None}


def summary(results: List[dict]) -> dict:
    counts = {v: 0 for v in VERDICTS}
    for r in results:
        counts[r["verdict"]] += 1
    # Release evidence (ent#783): go only when every chain passed. Anything not
    # run or partial is "not evidence", never a silent pass.
    go = bool(results) and counts["passed"] == len(results)
    return {"counts": counts, "total": len(results), "all_passed": go}


def to_json(results: List[dict], meta: Optional[dict] = None) -> str:
    return json.dumps({"meta": meta or {}, "summary": summary(results),
                       "chains": sorted(results, key=lambda r: r["chain"])},
                      indent=2, sort_keys=False)


_ICON = {"passed": "✅", "failed": "❌", "not_run": "⏸️", "partial": "◐"}


def to_markdown(results: List[dict], meta: Optional[dict] = None) -> str:
    """The summary a person attaches to #783 as go / no-go evidence."""
    meta = meta or {}
    s = summary(results)
    lines = ["## System chain tests (trinity-enterprise#794)", ""]
    if meta:
        lines.append(" · ".join(f"**{k}**: {v}" for k, v in meta.items()))
        lines.append("")
    c = s["counts"]
    if not results:
        lines.append("**No chain reported.** Nothing ran, which is not evidence — treat this "
                     "run as failed.")
        return "\n".join(lines) + "\n"
    lines.append(f"**{c['passed']} passed · {c['failed']} failed · {c['partial']} partial · "
                 f"{c['not_run']} not run** of {s['total']}. "
                 + ("All chains passed." if s["all_passed"] else
                    "Not a go: every chain must pass; not run and partial are not evidence."))
    lines += ["", "| Chain | Verdict | Step | Reason |", "|---|---|---|---|"]
    for r in sorted(results, key=lambda r: r["chain"]):
        reason = (r.get("reason") or "").replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {r['chain']} {r['title']} | {_ICON[r['verdict']]} {r['verdict']} "
                     f"| {r.get('step') or ''} | {reason} |")
    return "\n".join(lines) + "\n"
