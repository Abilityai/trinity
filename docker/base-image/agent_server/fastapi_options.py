"""Constructor options for the agent server's FastAPI app (#3106).

FastAPI 0.142 added automatic OpenTelemetry export, configured at ASGI lifespan
startup from the standard `OTEL_*` environment. Trinity injects those variables
for Claude Code (metrics over `grpc` to the collector), and FastAPI refuses
`grpc`, so the server exited on boot on every OTEL-enabled agent. The `OTEL_*`
env is Claude Code's, not the agent server's: opt out.

`telemetry=` does not exist before 0.142, so it is passed only when the
installed FastAPI accepts it — an agent whose template pinned an older FastAPI
keeps booting too.
"""
from __future__ import annotations

import inspect


def fastapi_app_options(fastapi_cls) -> dict:
    """Extra keyword arguments for `fastapi_cls(...)`."""
    try:
        params = inspect.signature(fastapi_cls.__init__).parameters
    except (TypeError, ValueError):
        return {}
    if "telemetry" in params:
        return {"telemetry": {"auto_configure": False}}
    return {}
