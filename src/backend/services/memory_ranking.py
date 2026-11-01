"""Ranking for retrieval memory (trinity-enterprise#1022, MEM-002) — a leaf.

Pure numpy over vectors the caller already holds: no database, no model, no
settings. ``memory_service`` wires it to the store; the offline evaluation in
``tests/unit/ent1022_recency_eval.py`` imports it next to the real embedder
inside the backend image, which is why nothing here may import the app.

Two signals, kept apart on purpose:

* **Relevance** is the cosine between the query and the fact. It is the GATE:
  a fact below ``min_score`` is never recalled, however recent it is.
* **Recency** only re-orders the facts that passed the gate. The weight is
  ``floor + (1 - floor) * 0.5 ** (age_days / half_life_days)`` over the fact's
  ``updated_at`` — a fresh fact counts fully, one a half-life old counts
  ``(1 + floor) / 2``, and nothing ever drops below ``floor``, so an old fact
  that is the only relevant one is still recalled.

  ``floor = 0.6`` is ``gate / 0.5``: a fact at cosine ≥ 0.5 never scores below
  the 0.30 gate line however old it is, so a fresh fact that barely passes the
  gate cannot overtake an old strong one; and the newest fact can beat an old
  one by at most 1/0.6 ≈ 1.67× on equal relevance — enough, with a 30-day
  half-life, for the newer wording of a superseded fact to rank first when the
  old wording matches the query better by ~1.2× (the dated dataset in
  ``tests/unit/fixtures/ent1022_recency_dataset.json`` pins both ends; 0.7
  failed that case, 0.5 would let the fresh weak match win).

The reconcile step's neighbour search passes no timestamps and gets plain
cosine: whether two facts say the same thing has nothing to do with when.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import List, NamedTuple, Optional, Sequence

RECENCY_HALF_LIFE_DAYS = 30.0   # default; the operator knob is `trinity_memory_recency_half_life_days`
RECENCY_FLOOR = 0.6             # the weight an infinitely old fact keeps (= gate / 0.5, see above)


class Ranked(NamedTuple):
    fact_id: str
    text: str
    relevance: float     # raw cosine — what the gate saw
    score: float         # relevance × recency — what the order used
    age_days: Optional[float]


def parse_age_days(updated_at: Optional[str], now: Optional[datetime] = None) -> Optional[float]:
    """Days between ``updated_at`` (ISO-8601, ``Z`` or offset or naive-UTC) and
    ``now``; None when the stamp is missing or unreadable. Never negative."""
    if not updated_at:
        return None
    s = str(updated_at).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        then = datetime.fromisoformat(s)
    except ValueError:
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return max(0.0, (now - then).total_seconds() / 86400.0)


def recency_weight(age_days: Optional[float], *, half_life_days: float = RECENCY_HALF_LIFE_DAYS,
                   floor: float = RECENCY_FLOOR) -> float:
    """``floor + (1 - floor) * 0.5 ** (age / half_life)``. An unknown age, a
    non-positive half-life (recency off) or a floor ≥ 1 all weigh 1.0."""
    if age_days is None or half_life_days <= 0 or floor >= 1.0:
        return 1.0
    floor = max(0.0, floor)
    return floor + (1.0 - floor) * (0.5 ** (max(0.0, age_days) / half_life_days))


def rank(
    query_vec, ids: Sequence[str], texts: Sequence[str], matrix, *,
    k: int, min_score: float,
    updated_at: Optional[Sequence[Optional[str]]] = None,
    half_life_days: float = RECENCY_HALF_LIFE_DAYS, floor: float = RECENCY_FLOOR,
    now: Optional[datetime] = None,
) -> List[Ranked]:
    """The top ``k`` facts with cosine ≥ ``min_score``, ordered by
    ``cosine × recency`` when ``updated_at`` is given, by cosine alone otherwise."""
    import numpy as np

    if matrix is None or not ids:
        return []
    relevance = matrix @ np.asarray(query_vec, dtype="<f4")
    passed = [i for i in range(len(ids)) if float(relevance[i]) >= min_score]
    if not passed:
        return []
    ages: List[Optional[float]] = [None] * len(ids)
    if updated_at is not None:
        now = now or datetime.now(timezone.utc)
        for i in passed:
            ages[i] = parse_age_days(updated_at[i] if i < len(updated_at) else None, now)
    scored = [
        Ranked(ids[i], texts[i], float(relevance[i]),
               float(relevance[i]) * recency_weight(ages[i], half_life_days=half_life_days, floor=floor),
               ages[i])
        for i in passed
    ]
    scored.sort(key=lambda r: (-r.score, -r.relevance, r.fact_id))
    return scored[:k]
