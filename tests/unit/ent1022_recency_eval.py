"""Recency-weighted recall, evaluated on a dated dataset against the real
embedder (trinity-enterprise#1022).

Not a test module (no ``test_`` prefix): `test_ent1022_memory_provenance.py`
imports `evaluate` where the model is cached, and the same file runs as a
script inside the backend image, where the model is baked in and the network
is off::

    python tests/unit/ent1022_recency_eval.py tests/unit/fixtures/ent1022_recency_dataset.json

It loads only the two leaf modules — `services/memory_ranking.py` and
`services/memory_embeddings.py` — **by file path**, never through the
`services` package (whose ``__init__`` pulls in `config`, which refuses to
import without a Redis URL). The backend directory is ``TRINITY_BACKEND_DIR``
when set, else ``/app`` when that exists (the image), else ``src/backend`` next
to this checkout. No database, no settings: which is why the ranking lives in
a leaf at all.

For each query the dataset names the fact that must rank first, the facts that
must be in the top ``k`` and the ones that must not surface. Every query is
scored twice — recency ON (the shipped half-life and floor) and recency OFF
(plain cosine) — so the report shows what the weight changed, not only that
the shipped setting passes.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Sequence

RECALL_K = 5
RECALL_MIN_SCORE = 0.30     # the service's gate; kept here so the leaf stays settings-free


def backend_dir() -> Path:
    env = os.environ.get("TRINITY_BACKEND_DIR")
    if env:
        return Path(env)
    if Path("/app/services/memory_ranking.py").exists():
        return Path("/app")
    return Path(__file__).resolve().parents[2] / "src" / "backend"


def load_leaf(name: str):
    """Import ``services/<name>.py`` as a standalone module (no package __init__)."""
    path = backend_dir() / "services" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"ent1022_leaf_{name}", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


mr = load_leaf("memory_ranking")


def _as_utc(stamp: str) -> datetime:
    s = stamp[:-1] + "+00:00" if stamp.endswith("Z") else stamp
    return datetime.fromisoformat(s).astimezone(timezone.utc)


def evaluate(
    dataset: Dict[str, Any], embed: Callable[[Sequence[str]], Any], *,
    half_life_days: float = mr.RECENCY_HALF_LIFE_DAYS, floor: float = mr.RECENCY_FLOOR,
    k: int = RECALL_K, min_score: float = RECALL_MIN_SCORE,
) -> Dict[str, Any]:
    """``{"passed": bool, "queries": [...], "settings": {...}}``. Each query
    entry carries the ranked ids with relevance/score/age for both modes and
    the per-expectation verdicts for the shipped mode."""
    import numpy as np

    now = _as_utc(dataset["now"])
    facts = dataset["facts"]
    ids = [f["id"] for f in facts]
    texts = [f["text"] for f in facts]
    stamps = [(now - timedelta(days=float(f["days_ago"]))).isoformat().replace("+00:00", "Z") for f in facts]
    vectors = embed(texts)
    if not vectors:
        raise RuntimeError("embedder returned nothing")
    matrix = np.vstack([np.asarray(v, dtype="<f4") for v in vectors])

    out: Dict[str, Any] = {"settings": {"half_life_days": half_life_days, "floor": floor, "k": k, "min_score": min_score},
                           "queries": [], "passed": True}
    for q in dataset["queries"]:
        qvec = np.asarray(embed([q["query"]])[0], dtype="<f4")
        on = mr.rank(qvec, ids, texts, matrix, k=k, min_score=min_score, updated_at=stamps,
                     half_life_days=half_life_days, floor=floor, now=now)
        off = mr.rank(qvec, ids, texts, matrix, k=k, min_score=min_score, now=now)
        top = [r.fact_id for r in on]
        checks: List[Dict[str, Any]] = []
        if "expect_first" in q:
            checks.append({"check": f"first == {q['expect_first']}", "ok": bool(top) and top[0] == q["expect_first"]})
        for fid in q.get("expect_in_top", []):
            checks.append({"check": f"{fid} in top {k}", "ok": fid in top})
        for fid in q.get("expect_absent", []):
            checks.append({"check": f"{fid} absent", "ok": fid not in top})
        ok = all(c["ok"] for c in checks)
        out["passed"] = out["passed"] and ok
        out["queries"].append({
            "name": q["name"], "query": q["query"], "ok": ok, "checks": checks,
            "recency_on": [{"id": r.fact_id, "relevance": round(r.relevance, 3), "score": round(r.score, 3),
                            "age_days": None if r.age_days is None else round(r.age_days, 1)} for r in on],
            "recency_off": [{"id": r.fact_id, "relevance": round(r.relevance, 3)} for r in off],
            "changed_order": [r.fact_id for r in on] != [r.fact_id for r in off],
        })
    return out


def format_report(report: Dict[str, Any]) -> str:
    s = report["settings"]
    lines = [f"recency: half-life {s['half_life_days']} d, floor {s['floor']}, k={s['k']}, gate ≥ {s['min_score']}", ""]
    for q in report["queries"]:
        lines.append(("PASS " if q["ok"] else "FAIL ") + q["name"])
        lines.append(f"      query: {q['query']}")
        lines.append("      recency on : " + ", ".join(f"{r['id']} ({r['relevance']}→{r['score']}, {r['age_days']}d)" for r in q["recency_on"]))
        lines.append("      recency off: " + ", ".join(f"{r['id']} ({r['relevance']})" for r in q["recency_off"])
                     + ("   [order changed]" if q["changed_order"] else ""))
        for c in q["checks"]:
            if not c["ok"]:
                lines.append(f"      ✗ {c['check']}")
    lines.append("")
    lines.append("RESULT: " + ("all queries passed" if report["passed"] else "FAILURES above"))
    return "\n".join(lines)


def main(argv: List[str]) -> int:
    if len(argv) != 2:
        print("usage: ent1022_recency_eval.py <dataset.json>", file=sys.stderr)
        return 2
    me = load_leaf("memory_embeddings")
    ok, reason = me.availability()
    if not ok:
        print(f"embedding model unavailable: {reason}", file=sys.stderr)
        return 3
    dataset = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    report = evaluate(dataset, me.embed)
    print(f"backend: {backend_dir()}  model: {me.MODEL_NAME}")
    print(format_report(report))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
