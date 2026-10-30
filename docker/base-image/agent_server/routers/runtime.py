"""One-shot completion on the agent's own runtime (trinity-enterprise#1027).

``POST /api/runtime/complete`` — the backend's retrieval memory sends its
extraction / reconcile prompt here so the agent's own subscription or key pays
for it. No tools, no project context, one turn; see
``services/runtime_complete.py`` for the spawn's shape and the failure classes.
"""
import logging
import time
import uuid

from fastapi import APIRouter, HTTPException

from ..models import RuntimeCompleteRequest
from ..state import agent_state
from ..services.runtime_adapter import RuntimeUnsupported, get_runtime
from ..services.runtime_complete import CompleteError, DEFAULT_MODEL

logger = logging.getLogger(__name__)
router = APIRouter()

TIMEOUT_MIN_S = 5
TIMEOUT_MAX_S = 600


@router.post("/api/runtime/complete")
async def runtime_complete(request: RuntimeCompleteRequest):
    """Run one prompt through this agent's runtime and return the text."""
    if not agent_state.runtime_available:
        raise HTTPException(status_code=503, detail={
            "code": "runtime_unavailable",
            "message": f"the {agent_state.agent_runtime} runtime is not available in this container",
        })
    runtime = get_runtime()
    timeout = max(TIMEOUT_MIN_S, min(TIMEOUT_MAX_S, int(request.timeout_seconds or 0)))
    is_claude = agent_state.agent_runtime in ("claude-code", "claude")
    model = request.model or (DEFAULT_MODEL if is_claude else runtime.get_default_model())
    run_id = f"complete-{uuid.uuid4().hex[:12]}"
    started = time.monotonic()
    try:
        text = await runtime.complete(request.prompt, model=model, timeout_seconds=timeout, execution_id=run_id)
    except RuntimeUnsupported as e:
        raise HTTPException(status_code=503, detail={"code": "unsupported", "message": str(e)})
    except CompleteError as e:
        logger.warning("[complete] %s %s: %s (%s)", request.tag, e.code, e.message[:160], run_id)
        raise HTTPException(status_code=e.status, detail={"code": e.code, "message": e.message})
    return {
        "text": text,
        "runtime": agent_state.agent_runtime,
        "model": model,
        "duration_ms": int((time.monotonic() - started) * 1000),
    }
