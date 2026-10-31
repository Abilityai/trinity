"""What still uses a declared metric — the provider seam (trinity-enterprise#1006).

Retiring a metric from the UI first lists everything that still reads it, and a
retire with any reference needs an explicit acknowledgement. Core code knows
three readers — recorded points, objectives, `dashboard.yaml` widgets — and
finds them in `metric_declaration_service`. Anything else that binds a metric
by `(agent_name, metric)` registers a provider here, so the warning names it
too without core code knowing what it is.

A provider is `fn(agent_name, metric) -> list[dict]`, sync or async. Each dict
is `{"kind", "label", "detail"}`: `kind` a short machine word, `label` what a
person recognises, `detail` an optional second line. A provider that raises is
reported as UNCHECKED, never as "no references" — a failed lookup is not
evidence that nothing uses the metric (#2196).
"""

import inspect
import logging
from typing import Any, Callable, Dict, List, Tuple

logger = logging.getLogger(__name__)

MetricReferenceProvider = Callable[[str, str], Any]

_providers: List[Tuple[str, MetricReferenceProvider]] = []


def register_provider(name: str, provider: MetricReferenceProvider) -> None:
    """Register `provider` under `name`. Idempotent per name (last wins)."""
    global _providers
    _providers = [(n, p) for n, p in _providers if n != name] + [(name, provider)]
    logger.info("[metric_references] provider registered: %s", name)


def clear_providers() -> None:
    """Drop every provider — used by tests."""
    _providers.clear()


def _clean(ref: Any) -> Dict[str, Any]:
    if not isinstance(ref, dict):
        return {}
    kind = ref.get("kind")
    label = ref.get("label")
    if not isinstance(kind, str) or not isinstance(label, str):
        return {}
    detail = ref.get("detail")
    return {"kind": kind[:32], "label": label[:200],
            "detail": detail[:200] if isinstance(detail, str) else None}


async def from_providers(agent_name: str, metric: str) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Every registered provider's references, and the names that failed."""
    found: List[Dict[str, Any]] = []
    unchecked: List[str] = []
    for name, provider in list(_providers):
        try:
            result = provider(agent_name, metric)
            if inspect.isawaitable(result):
                result = await result
            for ref in result or []:
                cleaned = _clean(ref)
                if cleaned:
                    found.append(cleaned)
        except Exception as e:  # noqa: BLE001 — a provider never fails the retire check
            logger.warning("[metric_references] provider %s failed for %s/%s: %s",
                           name, agent_name, metric, type(e).__name__)
            unchecked.append(name)
    return found, unchecked
