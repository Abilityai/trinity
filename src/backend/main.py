"""
Trinity Agent Platform - Backend API

A universal infrastructure platform for deploying Claude Code agent configurations.
Each agent runs as an isolated Docker container with standardized interfaces.

Refactored for better concern separation:
- config.py: Configuration constants
- models.py: Pydantic models
- dependencies.py: FastAPI dependencies (auth)
- services/: Business logic (docker, template)
- routers/: API endpoints organized by domain
- utils/: Helper functions
"""
import asyncio
import json
import os
import random
from datetime import datetime
from typing import Dict, List, Optional
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Depends, Request, Query
from fastapi.middleware.cors import CORSMiddleware
import httpx

from config import CORS_ORIGINS, VOICE_ENABLED, GEMINI_API_KEY
from models import User
from dependencies import get_current_user
from services.docker_service import docker_client, list_all_agents_fast
from utils.helpers import utc_now_iso

# OpenTelemetry imports for distributed tracing (RELIABILITY-002)
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import TraceIdRatioBased
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.redis import RedisInstrumentor

# Import routers
from routers.auth import router as auth_router
from routers.agents import router as agents_router, set_websocket_manager as set_agents_ws_manager, set_filtered_websocket_manager as set_agents_filtered_ws_manager
from routers.agent_config import router as agent_config_router
from routers.agent_data import router as agent_data_router
from routers.agent_files import router as agent_files_router
from routers.agent_brain_orb import router as agent_brain_orb_router  # #58 Brain Orb proxy
from routers.agent_rename import router as agent_rename_router, set_websocket_manager as set_agent_rename_ws_manager, set_filtered_websocket_manager as set_agent_rename_filtered_ws_manager
from routers.agent_ssh import router as agent_ssh_router
from routers.credentials import router as credentials_router
from routers.templates import router as templates_router
from routers.sharing import router as sharing_router, set_websocket_manager as set_sharing_ws_manager
from routers.mcp_keys import router as mcp_keys_router
from routers.chat import router as chat_router, set_websocket_manager as set_chat_ws_manager
from routers.sessions import router as sessions_router  # SESSION_TAB_2026-04 Phase 2
from routers.fan_out import router as fan_out_router
from routers.schedules import router as schedules_router
from routers.git import router as git_router
from routers.fleet import router as fleet_router
from routers.executions import router as executions_router  # EXEC-022 / Issue #18
from routers.analytics import router as analytics_router  # #1107 — Agent Overview analytics
from routers.activities import router as activities_router
from routers.settings import router as settings_router
from routers.systems import router as systems_router
from routers.observability import router as observability_router
from routers.system_agent import router as system_agent_router
from routers.ops import router as ops_router
from routers.public_links import router as public_links_router, set_websocket_manager as set_public_links_ws_manager
from routers.public import router as public_router
from routers.files import router as files_router  # FILES-001 — outbound file downloads
from routers.setup import router as setup_router
from routers.telemetry import router as telemetry_router
from routers.logs import router as logs_router
from routers.agent_dashboard import router as agent_dashboard_router
from routers.audit_log import router as audit_log_router  # SEC-001 / Issue #20
from routers.canary import router as canary_router  # CANARY-001 / Issue #411
from routers.compatibility import router as compatibility_router  # #668 agent compatibility
from routers.skills import router as skills_router
from routers.internal import router as internal_router
from routers.tags import router as tags_router
from routers.system_views import router as system_views_router
from routers.notifications import router as notifications_router, set_websocket_manager as set_notifications_ws_manager, set_filtered_websocket_manager as set_notifications_filtered_ws_manager
from routers.reports import router as reports_router
from services.report_service import set_websocket_manager as set_reports_ws_manager, set_filtered_websocket_manager as set_reports_filtered_ws_manager
from routers.subscriptions import router as subscriptions_router
from routers.monitoring import router as monitoring_router, set_websocket_manager as set_monitoring_ws_manager, set_filtered_websocket_manager as set_monitoring_filtered_ws_manager
from routers.slack import public_router as slack_public_router, auth_router as slack_auth_router
from routers.telegram import public_router as telegram_public_router, auth_router as telegram_auth_router
from routers.whatsapp import public_router as whatsapp_public_router, auth_router as whatsapp_auth_router
from routers.paid import router as paid_router
from routers.nevermined import router as nevermined_router
from routers.image_generation import router as image_generation_router
from routers.avatar import router as avatar_router
from routers.operator_queue import router as operator_queue_router, set_websocket_manager as set_operator_queue_ws_manager
from routers.voice import router as voice_router
from routers.voip import public_router as voip_public_router, auth_router as voip_auth_router
from routers.event_subscriptions import router as event_subscriptions_router, set_websocket_manager as set_event_subs_ws_manager, set_filtered_websocket_manager as set_event_subs_filtered_ws_manager
from routers.users import router as users_router
from routers.debug import router as debug_router  # #306 soak instrumentation
from routers.a2a import router as a2a_router  # #737 A2A Agent Cards
from routers.admin_recovery import router as admin_recovery_router  # #834 Phase 1c
from routers.messages import router as messages_router  # Proactive Messaging (#321)
from routers.public_memory import router as public_memory_router  # MEM-001 write path (#888)
from routers.loops import (
    agent_router as loops_agent_router,
    loop_router as loops_loop_router,
)  # Sequential agent loops (#740)
from services.loop_service import set_websocket_manager as set_loop_ws_manager
from routers.webhooks import router as webhooks_router  # Webhook triggers (WEBHOOK-001, #291)
from routers.ws_tickets import router as ws_tickets_router  # /ws ticket auth (#550)

# Import activity service
from services.activity_service import activity_service

# Import system agent service
from services.system_agent_service import system_agent_service

# Import log archive service
from services.log_archive_service import log_archive_service

# Import audit retention service (#552)
from services.audit_retention_service import audit_retention_service
from services.db_vacuum_service import db_vacuum_service

# Import operator queue sync service
from services.operator_queue_service import operator_queue_service, set_websocket_manager as set_opqueue_sync_ws_manager
from services.sync_health_service import sync_health_service

# Import cleanup service
from services.cleanup_service import cleanup_service, set_cleanup_ws_manager
from services.canary_service import canary_service  # CANARY-001 / Issue #411


from services.platform_audit_service import platform_audit_service, AuditEventType


# Import logging configuration
import logging
from logging_config import setup_logging

logger = logging.getLogger(__name__)


# Redis Streams event bus replaces the old in-process broadcast. Legacy manager
# classes are kept as thin shims so the 33 existing broadcast call sites don't
# change. See docs/memory/feature-flows/websocket-event-bus.md and
# services/event_bus.py (RELIABILITY-003 / #306).
from services.event_bus import (
    event_bus,
    stream_dispatcher,
    SCOPE_ALL,
    SCOPE_SCOPED,
)


class ConnectionManager:
    """Thin shim over ``event_bus``: preserves the legacy broadcast-a-JSON-string API.

    Connections themselves are tracked by ``StreamDispatcher``; ``connect()``
    here returns a client id so callers can ``disconnect()`` without juggling
    the dispatcher directly."""

    def __init__(self) -> None:
        self._client_ids: Dict[WebSocket, str] = {}

    async def connect(self, websocket: WebSocket, last_event_id: Optional[str] = None) -> None:
        await websocket.accept()
        async def _send(payload: dict) -> None:
            await websocket.send_text(json.dumps(payload))
        client_id = await stream_dispatcher.register(
            websocket,
            scope=SCOPE_ALL,
            send_func=_send,
            last_event_id=last_event_id,
        )
        self._client_ids[websocket] = client_id

    def disconnect(self, websocket: WebSocket) -> None:
        client_id = self._client_ids.pop(websocket, None)
        if client_id:
            stream_dispatcher.unregister(client_id)

    async def broadcast(self, message) -> None:
        """Publish an event for all /ws consumers.

        Accepts either a JSON-encoded string (legacy signature) or a dict
        (preferred going forward)."""
        await event_bus.publish(message, scope=SCOPE_ALL)


class FilteredWebSocketManager:
    """Thin shim over ``event_bus`` for /ws/events (Trinity Connect)."""

    def __init__(self) -> None:
        self._client_ids: Dict[WebSocket, str] = {}

    async def connect(
        self,
        websocket: WebSocket,
        email: str,
        is_admin: bool,
        accessible_agents: List[str],
        last_event_id: Optional[str] = None,
    ) -> None:
        async def _send(payload: dict) -> None:
            await websocket.send_json(payload)
        client_id = await stream_dispatcher.register(
            websocket,
            scope=SCOPE_SCOPED,
            send_func=_send,
            is_admin=is_admin,
            accessible_agents=accessible_agents,
            last_event_id=last_event_id,
        )
        self._client_ids[websocket] = client_id

    def disconnect(self, websocket: WebSocket) -> None:
        client_id = self._client_ids.pop(websocket, None)
        if client_id:
            stream_dispatcher.unregister(client_id)

    def update_accessible_agents(self, websocket: WebSocket, accessible_agents: List[str]) -> None:
        client_id = self._client_ids.get(websocket)
        if client_id:
            stream_dispatcher.update_accessible_agents(client_id, accessible_agents)

    async def broadcast_filtered(self, event: dict) -> None:
        await event_bus.publish(event, scope=SCOPE_SCOPED)


manager = ConnectionManager()
filtered_manager = FilteredWebSocketManager()

# Inject WebSocket manager into routers that need it
set_agents_ws_manager(manager)
set_agents_filtered_ws_manager(filtered_manager)
set_agent_rename_ws_manager(manager)
set_agent_rename_filtered_ws_manager(filtered_manager)
set_sharing_ws_manager(manager)
set_chat_ws_manager(manager)
set_public_links_ws_manager(manager)
set_notifications_ws_manager(manager)
set_notifications_filtered_ws_manager(filtered_manager)
set_reports_ws_manager(manager)
set_reports_filtered_ws_manager(filtered_manager)
set_monitoring_ws_manager(manager)
set_monitoring_filtered_ws_manager(filtered_manager)
set_operator_queue_ws_manager(manager)
set_opqueue_sync_ws_manager(manager)
set_event_subs_ws_manager(manager)
set_event_subs_filtered_ws_manager(filtered_manager)
set_loop_ws_manager(manager)  # #740

# NOTE: Trinity platform instructions are now injected at runtime via
# --append-system-prompt on every chat/task request (Issue #136).
# No startup injection callback needed.

# NOTE: Scheduler broadcast callbacks removed - dedicated scheduler (trinity-scheduler)
# publishes events to Redis which backend subscribes to, or via internal API calls

# Set up activity service WebSocket manager
activity_service.set_websocket_manager(manager)
activity_service.set_filtered_websocket_manager(filtered_manager)

# Set up cleanup service WebSocket manager for watchdog events (Issue #129)
set_cleanup_ws_manager(manager)


def setup_opentelemetry(app: FastAPI) -> bool:
    """
    Initialize OpenTelemetry distributed tracing (RELIABILITY-002).

    Auto-instruments FastAPI, httpx, and Redis for trace propagation.
    Traces are exported to the OTel Collector via OTLP/gRPC.

    Returns True if initialization succeeded, False otherwise.
    """
    if os.getenv("OTEL_ENABLED", "0") != "1":
        return False

    try:
        # Configure sampling: 10% in production, 100% in development
        sample_rate = float(os.getenv("OTEL_SAMPLE_RATE", "0.1"))
        sampler = TraceIdRatioBased(sample_rate)

        # Create resource with service metadata
        resource = Resource.create({
            "service.name": "trinity-backend",
            "service.version": "1.0.0",
            "deployment.environment": os.getenv("ENVIRONMENT", "development"),
        })

        # Set up tracer provider with sampling and resource
        provider = TracerProvider(resource=resource, sampler=sampler)
        trace.set_tracer_provider(provider)

        # Configure OTLP exporter to collector
        endpoint = os.getenv("OTEL_COLLECTOR_ENDPOINT", "http://trinity-otel-collector:4317")
        exporter = OTLPSpanExporter(endpoint=endpoint, insecure=True)
        provider.add_span_processor(BatchSpanProcessor(exporter))

        # Auto-instrument frameworks (injects traceparent headers automatically)
        FastAPIInstrumentor.instrument_app(app)
        HTTPXClientInstrumentor().instrument()
        RedisInstrumentor().instrument()

        return True
    except Exception as e:
        # Log but don't fail startup — tracing is optional
        # print (not logger): setup_opentelemetry() runs at module-import time,
        # before lifespan calls setup_logging(); flush=True guarantees delivery.
        # Same rationale as the register_enterprise prints. (#858)
        print(f"OpenTelemetry initialization failed: {e}", flush=True)
        return False


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler."""
    # Set up structured JSON logging (captured by Vector)
    setup_logging()

    # Signal first-time setup as early as possible — before any other startup step
    # that could hang and suppress it. The setup-token guard was removed in
    # trinity-enterprise#49 (no token to print); the operator just opens the UI to
    # create the admin account. Use logger.warning (not print): the logging
    # StreamHandler flushes after every record, whereas print() is block-buffered
    # to the Docker pipe without PYTHONUNBUFFERED=1 and is silently lost from
    # `docker logs` (#858).
    from database import db as _db
    if _db.get_setting_value('setup_completed', 'false') != 'true':
        logger.warning(
            "TRINITY FIRST-TIME SETUP REQUIRED — open the Trinity UI to create the "
            "admin account. On an internet-reachable instance, keep it behind a "
            "tunnel/VPN until setup completes (docs/DEPLOYMENT.md → Security)."
        )

    # Start Redis Streams event bus + dispatcher (RELIABILITY-003 / #306).
    # Must start before the WebSocket endpoints begin accepting clients so the
    # first connection has a live dispatcher to register with.
    try:
        await event_bus.start()
        await stream_dispatcher.start()
        logger.info("Redis Streams event bus started (maxlen=%d)",
                    int(os.getenv("REDIS_STREAM_MAXLEN", "10000")))
    except Exception as e:
        logger.error(f"Event bus startup failed (broadcasts will degrade): {e}")

    await platform_audit_service.log(
        event_type=AuditEventType.SYSTEM,
        event_action="startup",
        source="system",
        details={"otel_enabled": bool(_otel_enabled)},
    )

    # Report OpenTelemetry status (RELIABILITY-002)
    if _otel_enabled:
        sample_rate = float(os.getenv("OTEL_SAMPLE_RATE", "0.1"))
        logger.info(f"OpenTelemetry tracing enabled (sample rate: {sample_rate * 100:.0f}%)")
    else:
        logger.info("OpenTelemetry tracing disabled (set OTEL_ENABLED=1 to enable)")

    # #1159: surface a missing agent-auth master at boot, not per-request. The
    # backend is fail-closed — derive_agent_token() raises on an empty secret, so
    # without this warning the first symptom is a 500 on every backend→agent call
    # (and the WARNING gives the operator a clear, immediate fix). start.sh
    # auto-generates it; a deploy that bypasses start.sh must set it in .env and
    # forward it to the backend service (it is, in both compose files).
    if not os.getenv("AGENT_AUTH_SECRET"):
        logger.warning(
            "AGENT_AUTH_SECRET is not set — every backend→agent HTTP call will "
            "fail with a 500 (#1159, fail-closed). Set it in .env and forward it "
            "to the backend service (start.sh auto-generates it on first boot)."
        )


    if docker_client:
        try:
            agents = list_all_agents_fast()  # Fast startup - no slow Docker API calls
            logger.info(f"Found {len(agents)} existing Trinity agent containers")
            for agent in agents:
                logger.info(f"  - Agent: {agent.name} (status: {agent.status}, ssh_port: {agent.port})")
        except Exception as e:
            logger.error(f"Error checking agents: {e}")

        # Auto-deploy system agent (Phase 11.1)
        try:
            result = await system_agent_service.ensure_deployed()
            logger.info(f"System agent: {result['action']} - {result['message']}")
            if result.get('status') == 'error':
                logger.warning(f"  Warning: System agent deployment issue - {result.get('message')}")
        except Exception as e:
            logger.error(f"Error deploying system agent: {e}")
            # Don't fail startup - system agent is important but not critical for platform operation
    else:
        logger.info("Docker not available - running in demo mode")

    # NOTE: Embedded scheduler REMOVED (2026-02-11)
    # All schedule execution is handled by the dedicated scheduler service (trinity-scheduler container)
    # which uses Redis distributed locking and syncs schedules from database periodically.
    # Manual triggers are also delegated to the dedicated scheduler.
    # See: src/scheduler/, docs/memory/feature-flows/scheduler-service.md
    logger.info("Using dedicated scheduler service (trinity-scheduler)")

    # Initialize log archive service
    try:
        log_archive_service.start()
        logger.info("Log archive service started")
    except Exception as e:
        logger.error(f"Error starting log archive service: {e}")

    # Initialize audit retention service (#552)
    try:
        audit_retention_service.start()
        logger.info("Audit retention service started")
    except Exception as e:
        logger.error(f"Error starting audit retention service: {e}")

    # Initialize DB VACUUM service (#772)
    try:
        db_vacuum_service.start()
        logger.info("DB vacuum service started")
    except Exception as e:
        logger.error(f"Error starting DB vacuum service: {e}")

    # PERF-269: Stagger background services to reduce SQLite write contention
    # Start operator queue sync service (OPS-001) — polls every 5s
    try:
        operator_queue_service.start()
        logger.info("Operator queue sync service started")
    except Exception as e:
        logger.error(f"Error starting operator queue sync service: {e}")

    # Stagger cleanup service start by 2.5s to offset from operator queue writes
    async def _start_cleanup_delayed():
        await asyncio.sleep(2.5)
        try:
            cleanup_service.start()
            logger.info("Cleanup service started (staggered +2.5s)")
        except Exception as e:
            logger.error(f"Error starting cleanup service: {e}")
    asyncio.create_task(_start_cleanup_delayed())

    # SESSION_TAB_2026-04 Phase 4.2: periodic JSONL reaper for the Session
    # tab. Stagger +7.5s to offset from cleanup_service so they don't both
    # hit Docker at the same instant. Default poll = 6h, race-guard = 1h.
    async def _start_session_cleanup_delayed():
        await asyncio.sleep(7.5)
        try:
            from services.session_cleanup_service import get_session_cleanup_service
            get_session_cleanup_service().start()
            logger.info("Session cleanup service started (staggered +7.5s)")
        except Exception as e:
            logger.error(f"Error starting session cleanup service: {e}")
    asyncio.create_task(_start_session_cleanup_delayed())

    # Issue #389: Sync health service — 60s poll cadence, staggered +5s.
    async def _start_sync_health_delayed():
        await asyncio.sleep(5)
        try:
            sync_health_service.start()
            logger.info("Sync health service started (staggered +5s)")
        except Exception as e:
            logger.error(f"Error starting sync health service: {e}")
    asyncio.create_task(_start_sync_health_delayed())

    # CANARY-001 / Issue #411: Canary watcher — 5-min cycle. Disabled by
    # default (CANARY_ENABLED=1 to enable on staging/dev). Service self-
    # gates internally; the start() call is a no-op when not enabled.
    try:
        canary_service.start()
    except Exception as e:
        logger.error(f"Error starting canary service: {e}")

    # BACKLOG-001 / CAPACITY-CONSOLIDATE (#428): instantiate the unified
    # CapacityManager (this also wires the slot-release → backlog-drain
    # callback internally) and spawn the 60s maintenance loop. The
    # maintenance loop handles two things:
    #   1. Expire queued rows older than 24h (-> FAILED)
    #   2. Drain orphans — queued work that missed its release callback
    #      (e.g. backend restarted between enqueue and drain).
    try:
        from services.capacity_manager import get_capacity_manager
        capacity = get_capacity_manager()

        async def _capacity_maintenance_loop():
            # First tick after a short delay so startup stays snappy. #1085: a
            # small startup jitter so replicas don't realign their maintenance
            # ticks after a coordinated restart.
            await asyncio.sleep(15 + random.uniform(0, 2))
            while True:
                try:
                    await capacity.run_maintenance(max_age_hours=24)
                except Exception as exc:
                    logger.warning(f"[Capacity] maintenance tick failed: {exc}")
                # #1085: jitter the loop period so concurrent replicas' drain
                # sweeps spread out instead of firing in lockstep every 60s.
                await asyncio.sleep(60 + random.uniform(0, 15))

        asyncio.create_task(_capacity_maintenance_loop())
        logger.info("CapacityManager initialised; maintenance loop running (60s)")
    except Exception as e:
        logger.error(f"Error wiring CapacityManager: {e}")

    # RELIABILITY-004 / #307: agent heartbeat watch loop — 5s cadence,
    # staggered +10s. Actively downgrades an agent to a soft `degraded` health
    # state after 3 consecutive missed heartbeats (additive to the 30s
    # monitoring loop, which stays authoritative). Self-survives Redis/Docker
    # blips; old-image agents that never heartbeat resolve to `unsupported`
    # and are ignored.
    async def _start_heartbeat_watch_delayed():
        await asyncio.sleep(10)
        try:
            from services.heartbeat_service import schedule_heartbeat_watch
            schedule_heartbeat_watch()
            logger.info("Heartbeat watch loop started (staggered +10s, 5s cadence)")
        except Exception as e:
            logger.error(f"Error starting heartbeat watch loop: {e}")
    asyncio.create_task(_start_heartbeat_watch_delayed())

    # MON-001 / #1121: resume the authoritative 30s fleet-monitoring loop from
    # its persisted setting. Previously the loop was only ever started by an
    # admin hitting POST /api/monitoring/enable, so every backend restart
    # silently killed it and left it off until a human re-enabled it. We now
    # read the persisted `monitoring_config` (single source of truth, default
    # OFF) and start the loop only when enabled — so the choice survives
    # restarts. Staggered +12s to keep boot snappy and offset from the other
    # delayed loops.
    async def _start_monitoring_delayed():
        await asyncio.sleep(12)
        try:
            from routers.monitoring import load_persisted_monitoring_config
            from services.monitoring_service import start_monitoring_service
            config = load_persisted_monitoring_config()
            if config.enabled:
                await start_monitoring_service(config)
                logger.info("Monitoring service resumed from persisted config (enabled)")
            else:
                logger.info("Monitoring service not started (persisted setting disabled / default off)")
        except Exception as e:
            logger.error(f"Error resuming monitoring service: {e}")
    asyncio.create_task(_start_monitoring_delayed())

    # Recover orphaned regular task executions (Issue #128).
    # #748: flip the warming-up gate open in a finally block so the
    # /internal/execute-task route doesn't 503 forever if recovery raises.
    from services.cleanup_service import (
        mark_startup_recovery_complete,
        recover_orphaned_executions,
    )
    try:
        task_recovery = await recover_orphaned_executions()
        if task_recovery["recovered"] > 0:
            logger.info(
                f"Task execution recovery: "
                f"recovered={task_recovery['recovered']}, "
                f"still_running={task_recovery['still_running']}, "
                f"skipped_grace={task_recovery.get('skipped_grace', 0)}"
            )
        else:
            logger.info("Task execution recovery: no orphaned executions found")
    except Exception as e:
        logger.error(f"Error recovering task executions: {e}")
        # Don't fail startup - recovery is important but not critical
    finally:
        mark_startup_recovery_complete()

    # Start Slack channel transport (Socket Mode or webhook)
    try:
        from adapters.slack_adapter import SlackAdapter
        from adapters.message_router import message_router
        from services.settings_service import get_slack_transport_mode, get_slack_app_token, get_slack_signing_secret

        _slack_adapter = SlackAdapter()
        _slack_transport = None
        slack_mode = get_slack_transport_mode()

        if slack_mode == "socket":
            app_token = get_slack_app_token()
            if app_token:
                from adapters.transports.slack_socket import SlackSocketTransport
                _slack_transport = SlackSocketTransport(app_token, _slack_adapter, message_router)
                await _slack_transport.start()
                if _slack_transport.is_connected:
                    logger.info("Slack transport started (Socket Mode)")
                else:
                    # #708: keep the transport reference — its startup recovery
                    # supervisor (slack_socket.py:_startup_supervisor) is now
                    # retrying in the background and will populate contexts
                    # when the network recovers. Setting _slack_transport=None
                    # here would orphan the supervisor and leave Slack offline
                    # until the next manual restart.
                    logger.warning("Slack Socket Mode: initial connection failed; recovery supervisor retrying in background.")
            else:
                logger.info("Slack Socket Mode: no app token configured (set slack_app_token in Settings)")
        else:
            signing_secret = get_slack_signing_secret()
            if signing_secret:
                from adapters.transports.slack_webhook import SlackWebhookTransport
                from routers.slack import set_webhook_transport
                _slack_transport = SlackWebhookTransport(signing_secret, _slack_adapter, message_router)
                await _slack_transport.start()
                set_webhook_transport(_slack_transport)
                logger.info("Slack transport started (webhook mode)")
            else:
                logger.info("Slack webhook mode: no signing secret configured")

        # Store transport for shutdown
        app.state.slack_transport = _slack_transport
    except Exception as e:
        logger.error(f"Error starting Slack transport: {e}")
        # Don't fail startup — Slack is optional

    # Start Telegram webhook transport
    try:
        from adapters.telegram_adapter import TelegramAdapter
        from adapters.transports.telegram_webhook import TelegramWebhookTransport, register_webhook
        from routers.telegram import set_webhook_transport as set_telegram_webhook_transport

        _telegram_adapter = TelegramAdapter()
        _telegram_transport = TelegramWebhookTransport(_telegram_adapter, message_router)
        await _telegram_transport.start()
        set_telegram_webhook_transport(_telegram_transport)
        app.state.telegram_transport = _telegram_transport

        # Reconcile webhooks for all existing bindings on startup
        from services.settings_service import settings_service
        public_url = settings_service.get_setting("public_chat_url", "")
        if public_url:
            bindings = _db.get_all_telegram_bindings()
            for binding in bindings:
                try:
                    await register_webhook(binding["agent_name"], public_url)
                except Exception as we:
                    logger.warning(f"Telegram webhook reconciliation failed for {binding['agent_name']}: {we}")
            if bindings:
                logger.info(f"Telegram transport ready ({len(bindings)} bot(s) registered)")
            else:
                logger.info("Telegram transport ready (no bots configured)")
        else:
            logger.info("Telegram transport ready (no public URL — webhooks not registered)")
    except Exception as e:
        logger.error(f"Error starting Telegram transport: {e}")
        # Don't fail startup — Telegram is optional

    # Start WhatsApp (Twilio) webhook transport (WHATSAPP-001)
    try:
        from adapters.whatsapp_adapter import WhatsAppAdapter
        from adapters.transports.twilio_webhook import (
            TwilioWebhookTransport,
            backfill_webhook_urls as backfill_whatsapp_webhook_urls,
        )
        from adapters.message_router import message_router
        from routers.whatsapp import set_webhook_transport as set_whatsapp_webhook_transport

        _whatsapp_adapter = WhatsAppAdapter()
        _whatsapp_transport = TwilioWebhookTransport(_whatsapp_adapter, message_router)
        await _whatsapp_transport.start()
        set_whatsapp_webhook_transport(_whatsapp_transport)
        app.state.whatsapp_transport = _whatsapp_transport

        # Backfill webhook_url for existing bindings so UI displays the current URL
        from services.settings_service import settings_service as _settings_svc
        public_url = _settings_svc.get_setting("public_chat_url", "")
        if public_url:
            backfill_whatsapp_webhook_urls(public_url)
            bindings = _db.get_all_whatsapp_bindings()
            logger.info(f"WhatsApp transport ready ({len(bindings)} binding(s); webhook URLs refreshed)")
        else:
            logger.info("WhatsApp transport ready (no public URL — webhook URLs not computed)")
    except Exception as e:
        logger.error(f"Error starting WhatsApp transport: {e}")
        # Don't fail startup — WhatsApp is optional

    yield

    # NOTE: Embedded scheduler shutdown removed - scheduler runs in dedicated container
    # See: src/scheduler/, docs/memory/feature-flows/scheduler-service.md

    # Shutdown log archive service
    try:
        log_archive_service.stop()
        logger.info("Log archive service stopped")
    except Exception as e:
        logger.error(f"Error stopping log archive service: {e}")

    # Shutdown audit retention service (#552)
    try:
        audit_retention_service.stop()
        logger.info("Audit retention service stopped")
    except Exception as e:
        logger.error(f"Error stopping audit retention service: {e}")

    # Shutdown DB vacuum service (#772)
    try:
        db_vacuum_service.stop()
        logger.info("DB vacuum service stopped")
    except Exception as e:
        logger.error(f"Error stopping DB vacuum service: {e}")

    # Shutdown cleanup service
    try:
        cleanup_service.stop()
        logger.info("Cleanup service stopped")
    except Exception as e:
        logger.error(f"Error stopping cleanup service: {e}")

    # Shutdown session cleanup service (Phase 4.2)
    try:
        from services.session_cleanup_service import get_session_cleanup_service
        get_session_cleanup_service().stop()
        logger.info("Session cleanup service stopped")
    except Exception as e:
        logger.error(f"Error stopping session cleanup service: {e}")

    # Shutdown fleet monitoring loop (MON-001 / #1121) — parity with the
    # other lifespan-managed loops; no-op when it was never started.
    try:
        from services.monitoring_service import get_monitoring_service, stop_monitoring_service
        if get_monitoring_service().is_running:
            await stop_monitoring_service()
            logger.info("Monitoring service stopped")
    except Exception as e:
        logger.error(f"Error stopping monitoring service: {e}")

    # Shutdown Slack transport
    try:
        slack_transport = getattr(app.state, 'slack_transport', None)
        if slack_transport:
            await slack_transport.stop()
            logger.info("Slack transport stopped")
    except Exception as e:
        logger.error(f"Error stopping Slack transport: {e}")

    # Shutdown Telegram transport
    try:
        telegram_transport = getattr(app.state, 'telegram_transport', None)
        if telegram_transport:
            await telegram_transport.stop()
            logger.info("Telegram transport stopped")
    except Exception as e:
        logger.error(f"Error stopping Telegram transport: {e}")

    # Shutdown WhatsApp transport
    try:
        whatsapp_transport = getattr(app.state, 'whatsapp_transport', None)
        if whatsapp_transport:
            await whatsapp_transport.stop()
            logger.info("WhatsApp transport stopped")
    except Exception as e:
        logger.error(f"Error stopping WhatsApp transport: {e}")


    # Shutdown sync health service (#389)
    try:
        sync_health_service.stop()
        logger.info("Sync health service stopped")
    except Exception as e:
        logger.error(f"Error stopping sync health service: {e}")

    # Shutdown canary service (CANARY-001 / Issue #411)
    try:
        canary_service.stop()
        logger.info("Canary service stopped")
    except Exception as e:
        logger.error(f"Error stopping canary service: {e}")

    # Shutdown operator queue sync service
    try:
        operator_queue_service.stop()
        logger.info("Operator queue sync service stopped")
    except Exception as e:
        logger.error(f"Error stopping operator queue sync service: {e}")

    # Close pooled HTTP clients (RELIABILITY-001)
    try:
        from services.agent_client import close_all_clients
        await close_all_clients()
        logger.info("Agent HTTP client pool closed")
    except Exception as e:
        logger.error(f"Error closing agent HTTP client pool: {e}")

    try:
        await platform_audit_service.log(
            event_type=AuditEventType.SYSTEM,
            event_action="shutdown",
            source="system",
        )
    except Exception as e:
        logger.error(f"Error writing shutdown audit entry: {e}")

    # Drain event bus + stop dispatcher last so late-lifecycle broadcasts
    # (e.g. "agent_stopped" emitted during service shutdown) still land on
    # the stream. 2s drain window per #306.
    try:
        await stream_dispatcher.stop()
        await event_bus.stop(drain_timeout=2.0)
        logger.info("Event bus and stream dispatcher stopped")
    except Exception as e:
        logger.error(f"Error stopping event bus/dispatcher: {e}")


# Create FastAPI app
app = FastAPI(
    title="Trinity Agent Platform",
    description="Universal infrastructure for deploying Claude Code agent configurations",
    version="2.0.0",
    lifespan=lifespan
)

# Initialize OpenTelemetry distributed tracing (RELIABILITY-002)
# Must be called after app creation but before middleware/routers
_otel_enabled = setup_opentelemetry(app)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Source-Agent", "Accept"],
)

# Request-ID middleware — generates a correlation ID for every request.
# Stored on request.state.request_id for use by audit logging (SEC-001 Phase 2b).
# Respects an incoming X-Request-ID header if present (e.g. from nginx or upstream proxy).
import uuid as _uuid

@app.middleware("http")
async def add_request_id(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID") or str(_uuid.uuid4())
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    return response


# Security headers middleware — covers API responses when accessed directly (dev mode)
# or through nginx proxy.
#
# Issue #549 (UnderDefense pentest 3.4.2): FastAPI responses lacked
# X-Frame-Options, Cross-Origin-Opener-Policy, and HSTS. The frontend
# proxy adds those for HTML/asset responses, but API responses shown to
# tools like Swagger UI / direct curl were unprotected. Add them here so
# API responses match the frontend baseline. CSP is intentionally NOT
# set on API responses — they're JSON, not rendered, and a strict CSP
# can spuriously block legitimate Swagger / docs interactions.
#
# HSTS is gated on the connection actually being HTTPS (request scheme
# or X-Forwarded-Proto from a trusted reverse proxy) so local HTTP dev
# still works without forcing browsers to upgrade and pin a stale
# header.
@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=()"
    response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
    response.headers["Cross-Origin-Opener-Policy"] = "same-origin"

    # HSTS only when we know the wire is HTTPS — checking both the
    # direct scheme and the X-Forwarded-Proto header set by upstream
    # reverse proxies (uvicorn launched with --proxy-headers honours it).
    is_https = (
        request.url.scheme == "https"
        or request.headers.get("x-forwarded-proto", "").lower() == "https"
    )
    if is_https:
        # 1 year, includeSubDomains. preload intentionally omitted —
        # opting in is a one-way commitment that ops should make
        # explicitly via the load balancer rather than at the app.
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"

    return response


# Include all routers
app.include_router(auth_router)
app.include_router(agents_router)
app.include_router(agent_config_router)
app.include_router(agent_data_router)  # #1169: data export/import
app.include_router(agent_files_router)
app.include_router(agent_brain_orb_router)  # #58: Brain Orb read-only data proxy
app.include_router(agent_rename_router)
app.include_router(agent_ssh_router)
app.include_router(activities_router)
app.include_router(credentials_router)
app.include_router(templates_router)
app.include_router(sharing_router)
app.include_router(mcp_keys_router)
app.include_router(chat_router)
app.include_router(sessions_router)  # SESSION_TAB_2026-04 Phase 2 — gated on is_session_tab_enabled()
app.include_router(fan_out_router)
app.include_router(schedules_router)
app.include_router(git_router)
app.include_router(fleet_router)  # #390 S6 fleet sync-audit
app.include_router(executions_router)  # EXEC-022 / Issue #18 — Unified Executions Dashboard
app.include_router(analytics_router)  # #1107 — Agent Detail Overview analytics
app.include_router(settings_router)
app.include_router(systems_router)
app.include_router(observability_router)
app.include_router(system_agent_router)
app.include_router(ops_router)
app.include_router(public_links_router)
app.include_router(public_router)
app.include_router(files_router)  # FILES-001: /api/files/{id} — token-gated downloads
app.include_router(setup_router)
app.include_router(telemetry_router)
app.include_router(logs_router)
app.include_router(agent_dashboard_router)
app.include_router(audit_log_router)  # SEC-001 / #20: Platform audit log (Phase 1)
app.include_router(canary_router)  # CANARY-001 / #411: Invariant violations
app.include_router(compatibility_router)  # #668: Agent compatibility validation
app.include_router(skills_router) # Skills Management System
app.include_router(internal_router)  # Internal agent-to-backend endpoints (no auth)
app.include_router(tags_router)  # Agent Tags (ORG-001)
app.include_router(system_views_router)  # System Views (ORG-001 Phase 2)
app.include_router(notifications_router)  # Agent Notifications (NOTIF-001)
app.include_router(reports_router)  # Agent Reports (#918)
app.include_router(messages_router)  # Proactive Messaging (#321)
app.include_router(public_memory_router)  # MEM-001 write path (#888)
app.include_router(subscriptions_router)  # Subscription Management (SUB-001)
app.include_router(monitoring_router)  # Agent Monitoring (MON-001)
app.include_router(slack_public_router)  # Slack Integration Public (SLACK-001)
app.include_router(slack_auth_router)  # Slack Integration Auth (SLACK-001)
app.include_router(telegram_public_router)  # Telegram Integration Public (TELEGRAM-001)
app.include_router(telegram_auth_router)  # Telegram Integration Auth (TELEGRAM-001)
app.include_router(whatsapp_public_router)  # WhatsApp via Twilio Public (WHATSAPP-001)
app.include_router(whatsapp_auth_router)  # WhatsApp via Twilio Auth (WHATSAPP-001)
app.include_router(paid_router)  # Nevermined Paid Chat (NVM-001)
app.include_router(nevermined_router)  # Nevermined Admin Config (NVM-001)
app.include_router(image_generation_router)  # Image Generation (IMG-001)
app.include_router(avatar_router)  # Agent Avatars (AVATAR-001)
app.include_router(operator_queue_router)  # Operator Queue (OPS-001)
app.include_router(voice_router)  # Voice Chat (VOICE-001)
app.include_router(voip_public_router)  # VoIP Telephony Media Streams WS (VOIP-001)
app.include_router(voip_auth_router)  # VoIP Telephony binding + trigger (VOIP-001)
app.include_router(event_subscriptions_router)  # Agent Event Subscriptions (EVT-001)
app.include_router(users_router)  # User Management (ROLE-001)
app.include_router(debug_router)  # #306 soak dashboard
app.include_router(a2a_router)  # A2A Agent Cards (#737)
app.include_router(admin_recovery_router)  # Soft-delete admin recovery (#834 Phase 1c)
app.include_router(loops_agent_router)  # Sequential agent loops (#740)
app.include_router(loops_loop_router)  # Sequential agent loops (#740)
app.include_router(webhooks_router)  # Webhook Triggers (WEBHOOK-001, #291)
app.include_router(ws_tickets_router)  # WebSocket auth tickets (#550)


# #847 Phase 0 — Enterprise modules (closed-source companion submodule
# at `src/backend/enterprise/`, repo `Abilityai/trinity-enterprise`).
# The submodule is OPTIONAL: customers running the public repo without
# enterprise access clone without it, and the ImportError below silently
# no-ops. When mounted, `register_enterprise(app)` installs the SSO /
# SCIM / SIEM routers under `/api/enterprise/*`. The function is
# idempotent (guards on `app.state.enterprise_registered`). Entitlement
# gating happens per-endpoint via `requires_entitlement(feature_id)`
# from `dependencies.py` — endpoints are mounted unconditionally and
# the gate decides whether to serve them. This keeps the wiring
# deterministic regardless of license state.
#
# Import path is `enterprise.backend.register_enterprise`: the private
# repo is restructured into `backend/` and `frontend/` subdirs so the
# same repo can be dual-mounted (`src/backend/enterprise/` for Python,
# `src/frontend/src/enterprise/` for Vite). See
# `docs/planning/ENTERPRISE_ARCHITECTURE.md` for rationale.
try:
    from enterprise.backend import register_enterprise  # type: ignore[import-not-found]
    register_enterprise(app)
    # `print(..., flush=True)`: this import block runs at module init,
    # which is BEFORE `lifespan` calls `setup_logging()`. The default
    # Python logger drops INFO-level records, so `logger.info` here
    # would be silently swallowed. Print to stdout instead — docker
    # logs captures it for ops + the CI workflow greps for it.
    print("Trinity Enterprise modules registered", flush=True)
except ImportError:
    print(
        "Trinity Enterprise submodule not present — OSS-only build "
        "(this is normal; enterprise modules are an optional private submodule)",
        flush=True,
    )
except Exception as e:
    # A BUG in enterprise registration (schema init, migration, router
    # mount, pusher start) must NOT take down the core platform. Degrade
    # to OSS-only and surface loudly instead of crashing boot. Any modules
    # that registered before the failure stay active; the rest are absent
    # (their entitlement simply won't appear in feature-flags). (#995/#997)
    import traceback
    print(
        f"Trinity Enterprise registration FAILED — continuing OSS-only: {e!r}",
        flush=True,
    )
    traceback.print_exc()


# WebSocket endpoint
@app.websocket("/ws")
async def websocket_endpoint(
    websocket: WebSocket,
    ticket: str = Query(default=None),
    last_event_id: Optional[str] = Query(default=None, alias="last-event-id"),
):
    """
    WebSocket endpoint for real-time updates.

    Security (#178/C-002 + #550): authentication is REQUIRED before
    ``websocket.accept()``. Clients first call ``POST /api/ws/ticket``
    to mint a single-use 30-second opaque ticket, then connect to:

        /ws?ticket=<opaque_ticket>

    Switching from a long-lived JWT in the URL to an opaque single-use
    ticket closes the JWT-leak surface (nginx logs, browser history,
    upstream proxies) flagged by the April 2026 remediation pentest
    (finding 3.2.1) and mitigates CSWSH — a malicious page can't mint
    a ticket on the victim's behalf because the ticket endpoint requires
    the JWT in an ``Authorization`` header.

    Reconnect replay (#306): clients may pass ``last-event-id=<stream_id>``
    to receive events missed during a disconnect. Malformed or too-old ids
    produce a ``{"type": "resync_required"}`` message — the client must
    then fetch current state via REST.
    """
    from services.event_bus import validate_last_event_id
    from services.ws_ticket_service import consume_ticket

    if not ticket:
        await websocket.close(code=4001, reason="Authentication required: provide ?ticket=<opaque>")
        return

    payload = consume_ticket(ticket)
    if not payload or not payload.get("sub"):
        await websocket.close(code=4001, reason="Invalid or expired WebSocket ticket")
        return

    # Ticket validated — now accept the connection
    await manager.connect(websocket, last_event_id=validate_last_event_id(last_event_id))

    try:
        while True:
            data = await websocket.receive_text()

            # Handle ping/pong for keepalive (prevents idle timeout)
            try:
                msg = json.loads(data)
                if msg.get("type") == "ping":
                    await websocket.send_text(json.dumps({"type": "pong"}))
                    continue
            except (json.JSONDecodeError, TypeError):
                pass
    except WebSocketDisconnect:
        manager.disconnect(websocket)


# WebSocket endpoint for external listeners (Trinity Connect)
@app.websocket("/ws/events")
async def websocket_events_endpoint(
    websocket: WebSocket,
    token: str = Query(None, description="MCP API key for authentication"),
    last_event_id: Optional[str] = Query(None, alias="last-event-id"),
):
    """
    WebSocket endpoint for external event listeners (Trinity Connect).

    Authentication: MCP API key via ?token= query parameter
    Events: Filtered to only agents the authenticated user can access

    Usage:
        websocat "ws://localhost:8000/ws/events?token=trinity_mcp_xxx"
        wscat -c "ws://localhost:8000/ws/events?token=trinity_mcp_xxx"

    Events received:
        - agent_activity (chat_start, schedule_start, tool_call completions)
        - schedule_execution_completed
        - agent_started / agent_stopped
        - agent_collaboration

    Commands (send as text):
        - "ping" -> receives "pong"
        - "refresh" -> refreshes accessible agents list
    """
    from database import db
    from services.event_bus import validate_last_event_id

    # Validate MCP API key
    if not token or not token.startswith("trinity_mcp_"):
        await websocket.close(code=4001, reason="MCP API key required (use ?token=trinity_mcp_xxx)")
        return

    key_info = db.validate_mcp_api_key(token)
    if not key_info:
        await websocket.close(code=4001, reason="Invalid or inactive MCP API key")
        return

    user_email = key_info.get("user_email")
    # Determine if admin by checking user role
    user_data = db.get_user_by_username(key_info.get("user_id"))  # user_id is actually username
    is_admin = user_data and user_data.get("role") == "admin"

    # Get list of accessible agents for this user
    accessible_agents = db.get_accessible_agent_names(user_email, is_admin)

    await websocket.accept()
    await websocket.send_json({
        "type": "connected",
        "user": user_email,
        "accessible_agents": accessible_agents,
        "message": "Listening for events. Events filtered to your accessible agents."
    })

    # Add to filtered connections manager — enables reconnect replay via #306.
    await filtered_manager.connect(
        websocket, user_email, is_admin, accessible_agents,
        last_event_id=validate_last_event_id(last_event_id),
    )

    try:
        while True:
            # Keep connection alive, handle commands
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_text("pong")
            elif data == "refresh":
                # Refresh accessible agents list (e.g., after sharing changes)
                accessible_agents = db.get_accessible_agent_names(user_email, is_admin)
                filtered_manager.update_accessible_agents(websocket, accessible_agents)
                await websocket.send_json({
                    "type": "refreshed",
                    "accessible_agents": accessible_agents
                })
    except WebSocketDisconnect:
        filtered_manager.disconnect(websocket)


# Health check endpoint
@app.get("/health")
async def health_check():
    """Health check endpoint. Reports unhealthy if schema migrations are incomplete."""
    from db.migrations import MIGRATIONS
    from db.connection import get_db_connection
    from db.engine import is_sqlite
    from fastapi.responses import JSONResponse

    # PostgreSQL (#300) builds the schema fresh from schema.py at head and does
    # not run the sqlite-only PRAGMA migrations, so there is no schema_migrations
    # table to count. The migration-completeness gate is SQLite-only.
    if not is_sqlite():
        return {"status": "healthy", "timestamp": utc_now_iso()}

    from db.migrations import migration_health

    expected = len(MIGRATIONS)
    try:
        with get_db_connection() as conn:
            applied, expected, first_pending = migration_health(conn.cursor())
    except Exception as e:
        logger.warning("health_check: could not query schema_migrations: %s", e)
        # Can't read the tracking table — treat as incomplete and name the first
        # registered migration as the suspect.
        applied = 0
        first_pending = MIGRATIONS[0][0] if MIGRATIONS else None

    if applied < expected:
        # first_pending names the stuck/pending migration so a 503 is actionable,
        # not just a count (#1160).
        return JSONResponse(
            status_code=503,
            content={
                "status": "unhealthy",
                "timestamp": utc_now_iso(),
                "migrations": {
                    "applied": applied,
                    "expected": expected,
                    "first_pending": first_pending,
                },
            },
        )
    return {"status": "healthy", "timestamp": datetime.now()}


def _build_version_payload(voice_enabled: bool) -> dict:
    """Pure dict-builder for the `/api/version` payload (#926-testable).

    Extracted from the FastAPI handler so the env-var → response mapping
    can be tested without pulling main.py's full router graph through
    importlib (opentelemetry, slack_sdk, twilio, …).
    """
    import os
    from pathlib import Path

    # Version resolution order (#993):
    #   1. VERSION env var — build-stamped from git (e.g. "0.9.0+g4c640b6e"),
    #      wired through docker-compose backend.build.args + start.sh.
    #   2. VERSION file — curated semver, mounted in dev / copied in image.
    #   3. "unknown" — neither present.
    # Env-first means dev (bind-mount) and prod (build-arg) agree for the
    # same commit instead of diverging on the file-mount being absent.
    version = os.getenv("VERSION") or None
    if not version:
        version_paths = [
            Path("/app/VERSION"),  # In container (mounted)
            Path(__file__).parent.parent.parent / "VERSION",  # Development
        ]
        for version_file in version_paths:
            if version_file.exists():
                version = version_file.read_text().strip()
                break
    version = version or "unknown"

    git_commit = os.getenv("GIT_COMMIT", "unknown")
    git_commit_short = git_commit[:8] if git_commit != "unknown" else "unknown"

    return {
        "version": version,
        "platform": "trinity",
        "components": {
            "backend": version,
            "agent_server": version,
            "base_image": f"trinity-agent-base:{version}"
        },
        "runtimes": ["claude-code", "gemini-cli"],
        "build_date": os.getenv("BUILD_DATE", "unknown"),
        "git_commit": git_commit,
        "git_commit_short": git_commit_short,
        "git_commit_subject": os.getenv("GIT_COMMIT_SUBJECT", "unknown"),
        "git_commit_timestamp": os.getenv("GIT_COMMIT_TIMESTAMP", "unknown"),
        "git_branch": os.getenv("GIT_BRANCH", "unknown"),
        "voice_enabled": voice_enabled,
    }


# Version endpoint
@app.get("/api/version")
async def get_version(current_user: User = Depends(get_current_user)):
    """Get Trinity platform version information. Requires authentication (SEC-180).

    Build-time provenance fields (#926) — `git_commit`, `git_commit_short`,
    `git_commit_subject`, `git_commit_timestamp`, `git_branch`, `build_date` —
    come from Dockerfile ARG/ENV wired through docker-compose
    `backend.build.args` and `scripts/deploy/start.sh`. Default to "unknown"
    when the build args are absent (local dev / volume-mount workflows).
    """
    return _build_version_payload(VOICE_ENABLED and bool(GEMINI_API_KEY))


# User info endpoint
@app.get("/api/users/me")
async def get_current_user_info(current_user: User = Depends(get_current_user)):
    """Get current user information."""
    from database import db
    user_data = db.get_user_by_username(current_user.username)
    if user_data:
        return {
            "username": user_data["username"],
            "email": user_data.get("email"),
            "name": user_data.get("name"),
            "picture": user_data.get("picture"),
            "role": user_data["role"]
        }
    return {"username": current_user.username, "role": current_user.role}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
