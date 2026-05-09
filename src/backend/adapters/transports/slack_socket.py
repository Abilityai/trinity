"""
Slack Socket Mode transport — outbound WebSocket connection.

No public URL needed. Trinity connects out to Slack.
Default transport for local development.

Multi-connection mode (#244): Slack Socket Mode allows up to 10 concurrent
WebSocket connections per app. Slack's edge fans out events across active
connections so a connection half-closing is a no-op rather than a brief
outage. Default N=2; configurable via SLACK_SOCKET_CONNECTION_COUNT.

Each connection runs an independent watchdog task with its own backoff
counter, so one client failing does not delay the other's recovery.

Requires:
- slack_sdk[socket-mode] package
- App-Level Token (xapp-...) with connections:write scope
- Socket Mode enabled in Slack App settings
"""

import logging
import asyncio
import os
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Optional

from adapters.transports.base import ChannelTransport

logger = logging.getLogger(__name__)

# Watchdog config
WATCHDOG_INTERVAL_SECONDS = 60
WATCHDOG_PING_TIMEOUT_SECONDS = 5
WATCHDOG_BACKOFF_INITIAL_SECONDS = 60
WATCHDOG_BACKOFF_MAX_SECONDS = 300

# Multi-connection config (#244)
DEFAULT_CONNECTION_COUNT = 2
MIN_CONNECTION_COUNT = 1
MAX_CONNECTION_COUNT = 10  # Slack's documented per-app maximum

# Envelope-ID dedup ring config (#244)
DEDUP_RING_SIZE = 1024


def _read_connection_count() -> int:
    """Read SLACK_SOCKET_CONNECTION_COUNT from env, clamp to [MIN, MAX].

    Falls back to DEFAULT on parse error with a WARNING log so a malformed
    env var never crashes backend startup (Slack is an optional feature).
    """
    raw = os.environ.get("SLACK_SOCKET_CONNECTION_COUNT")
    if raw is None or raw.strip() == "":
        return DEFAULT_CONNECTION_COUNT
    try:
        n = int(raw)
    except (ValueError, TypeError):
        # Don't echo the raw value — operators sometimes paste credentials
        # into the wrong env var; an app token (xapp-...) would fail int()
        # parsing and end up in Vector logs verbatim. Log only the length.
        logger.warning(
            f"SLACK_SOCKET_CONNECTION_COUNT is not an integer "
            f"(got {len(raw)}-char value); "
            f"falling back to default {DEFAULT_CONNECTION_COUNT}"
        )
        return DEFAULT_CONNECTION_COUNT
    if n < MIN_CONNECTION_COUNT:
        logger.warning(
            f"SLACK_SOCKET_CONNECTION_COUNT={n} below minimum {MIN_CONNECTION_COUNT}; "
            f"clamping to {MIN_CONNECTION_COUNT}"
        )
        return MIN_CONNECTION_COUNT
    if n > MAX_CONNECTION_COUNT:
        logger.warning(
            f"SLACK_SOCKET_CONNECTION_COUNT={n} above Slack maximum {MAX_CONNECTION_COUNT}; "
            f"clamping to {MAX_CONNECTION_COUNT}"
        )
        return MAX_CONNECTION_COUNT
    return n


@dataclass
class _ClientCtx:
    """Per-client state. One instance per concurrent Socket Mode connection."""
    index: int
    client: object  # SocketModeClient — typed as object to keep imports lazy
    watchdog_task: Optional[asyncio.Task] = None
    consecutive_failures: int = 0


class SlackSocketTransport(ChannelTransport):
    """Slack Socket Mode — outbound WebSocket(s), no public URL needed.

    Runs N concurrent SocketModeClient instances (default 2). Slack distributes
    events across them; if one half-closes, the others continue receiving.
    """

    def __init__(self, app_token: str, adapter, router):
        super().__init__(adapter, router)
        self.app_token = app_token
        self.contexts: list[_ClientCtx] = []
        # Envelope-ID dedup ring: defends against the (currently unproven)
        # possibility that Slack delivers the same event to multiple
        # connections. INFO log on hit so we can measure whether it ever fires.
        self._envelope_seen: OrderedDict[str, float] = OrderedDict()
        self._dedup_lock = asyncio.Lock()
        self._dedup_hits = 0  # for tests / introspection
        # Startup recovery supervisor (#708): when all initial connect attempts
        # fail, this task retries with exponential backoff until at least one
        # client connects, then graduates to the per-client watchdog model.
        # Without this, a 10s startup blip leaves Slack permanently offline.
        self._supervisor_task: Optional[asyncio.Task] = None
        self._supervisor_attempts: int = 0

    @property
    def is_connected(self) -> bool:
        """True if at least one client has a healthy session.

        Permissive semantics intentionally — a partial-startup state
        (1 of N connected) still serves Slack traffic via the live client.
        """
        if not self._running or not self.contexts:
            return False
        return any(self._client_alive(ctx.client) for ctx in self.contexts)

    @property
    def connected_count(self) -> int:
        """Number of clients currently with a live session."""
        if not self._running:
            return 0
        return sum(1 for ctx in self.contexts if self._client_alive(ctx.client))

    @staticmethod
    def _client_alive(client) -> bool:
        """Per-client liveness probe shared by is_connected and connected_count."""
        if client is None:
            return False
        session = getattr(client, "current_session", None)
        return session is not None and not getattr(session, "closed", True)

    async def start(self) -> None:
        """Connect N concurrent Socket Mode clients in parallel.

        Non-blocking: per-client connection failures are logged but never
        crash startup. As long as ANY client connects, the transport is up.
        """
        if not self.app_token or not self.app_token.startswith("xapp-"):
            logger.error(
                "Invalid Slack App Token format (must start with 'xapp-'). "
                "Socket Mode not started."
            )
            return

        n = _read_connection_count()
        logger.info(f"Slack Socket Mode: starting {n} concurrent connection(s)")

        # Connect all clients in parallel — total startup time stays at
        # ~10s ceiling rather than N * 10s sequential.
        results = await asyncio.gather(
            *(self._start_one_client(i) for i in range(n)),
            return_exceptions=True,
        )

        for i, result in enumerate(results):
            if isinstance(result, _ClientCtx):
                self.contexts.append(result)
            else:
                # _start_one_client logged the reason internally
                logger.warning(f"Slack Socket Mode [c={i}]: did not connect")

        # _running is set BEFORE the supervisor branch so the supervisor's
        # `while self._running` guard cannot race with start() returning.
        # is_connected still requires at least one healthy session, so callers
        # that check it (main.py, /api/settings/slack/connect, /slack/status)
        # see False until the supervisor actually recovers a client (#708).
        self._running = True

        if not self.contexts:
            # All initial attempts failed — spawn the recovery supervisor.
            # Returns immediately; the supervisor retries in the background
            # with the same exponential backoff the per-client watchdog uses.
            logger.error(
                "Slack Socket Mode: all initial connection attempts failed. "
                "Starting recovery supervisor — will retry in background "
                f"(first retry in {WATCHDOG_BACKOFF_INITIAL_SECONDS}s)."
            )
            self._supervisor_task = asyncio.create_task(
                self._startup_supervisor(target_count=n)
            )
            return

        # Spawn one independent watchdog task per connected client.
        for ctx in self.contexts:
            ctx.watchdog_task = asyncio.create_task(self._watchdog(ctx))

        if len(self.contexts) < n:
            logger.warning(
                f"Slack Socket Mode: degraded — {len(self.contexts)}/{n} connections "
                f"established. Watchdog will retry the failed clients."
            )
        else:
            logger.info(
                f"Slack Socket Mode: {len(self.contexts)} connection(s) established "
                f"(watchdog started for each)"
            )

    async def _start_one_client(self, index: int) -> Optional[_ClientCtx]:
        """Start a single SocketModeClient. Returns ctx on success, None on failure."""
        try:
            from slack_sdk.socket_mode.aiohttp import SocketModeClient
        except ImportError:
            logger.error(
                "slack_sdk[socket-mode] not installed. "
                "Run: pip install slack_sdk[socket-mode]"
            )
            return None

        client = SocketModeClient(
            app_token=self.app_token,
            auto_reconnect_enabled=False,  # Don't auto-reconnect on invalid token
        )
        client.socket_mode_request_listeners.append(self._handle_request)

        try:
            await asyncio.wait_for(client.connect(), timeout=10)
        except asyncio.TimeoutError:
            logger.error(
                f"Slack Socket Mode [c={index}]: connection timed out (10s). "
                f"Check app token and network."
            )
            try:
                await client.disconnect()
            except Exception:
                pass
            return None
        except Exception as e:
            logger.error(f"Slack Socket Mode [c={index}]: connection failed: {e}")
            try:
                await client.disconnect()
            except Exception:
                pass
            return None

        # Connection succeeded — enable auto-reconnect on this client now
        client.auto_reconnect_enabled = True
        return _ClientCtx(index=index, client=client)

    async def _startup_supervisor(self, target_count: int) -> None:
        """Recovery supervisor — runs only when ALL initial connect attempts fail.

        The per-client watchdog model assumes at least one connection has
        been established. When the network is hiccuping during backend
        startup and every initial attempt times out (#708), nothing in
        the watchdog model fires. This task fills that gap.

        Behavior:
        - Sleeps `_supervisor_backoff_interval()` (60 → 120 → 240 → 300s cap),
          then retries `_start_one_client(i)` for every slot 0..target_count-1.
        - As soon as ANY client connects, appends it to self.contexts, spawns
          per-client watchdogs for the recovered clients, and exits — handing
          recovery back to the watchdog model.
        - Logs ERROR after 3 consecutive failures so operators get a paging
          signal rather than just the single startup log line.
        - Cancelled by stop(); CancelledError is re-raised so awaiters of the
          task see clean cancellation.
        """
        logger.info(
            f"Socket Mode startup supervisor: started "
            f"(target={target_count} connection(s))"
        )
        try:
            while self._running:
                interval = self._supervisor_backoff_interval()
                logger.info(
                    f"Socket Mode startup supervisor: next attempt in {interval}s "
                    f"(attempt #{self._supervisor_attempts + 1})"
                )
                await asyncio.sleep(interval)

                if not self._running:
                    break

                self._supervisor_attempts += 1

                results = await asyncio.gather(
                    *(self._start_one_client(i) for i in range(target_count)),
                    return_exceptions=True,
                )
                recovered: list[_ClientCtx] = []
                for i, result in enumerate(results):
                    if isinstance(result, _ClientCtx):
                        recovered.append(result)
                    else:
                        logger.warning(
                            f"Socket Mode startup supervisor [c={i}]: still failing"
                        )

                if recovered:
                    self.contexts.extend(recovered)
                    for ctx in recovered:
                        ctx.watchdog_task = asyncio.create_task(self._watchdog(ctx))
                    logger.info(
                        f"Socket Mode startup supervisor: recovered "
                        f"{len(recovered)}/{target_count} connection(s) after "
                        f"{self._supervisor_attempts} attempt(s); "
                        f"watchdog(s) started, supervisor exiting."
                    )
                    self._supervisor_attempts = 0
                    return

                if self._supervisor_attempts >= 3:
                    next_interval = self._supervisor_backoff_interval()
                    logger.error(
                        f"SLACK SOCKET MODE STARTUP UNREACHABLE — "
                        f"{self._supervisor_attempts} consecutive recovery attempts failed. "
                        f"Next retry in {next_interval}s. "
                        f"Check Slack app token and network connectivity."
                    )
        except asyncio.CancelledError:
            logger.info("Socket Mode startup supervisor: cancelled")
            raise
        except Exception as e:
            logger.error(
                f"Socket Mode startup supervisor: unexpected error: {e}",
                exc_info=True,
            )
        finally:
            logger.info("Socket Mode startup supervisor: stopped")

    def _supervisor_backoff_interval(self) -> int:
        """Exponential backoff for the startup supervisor.

        Mirrors the per-client watchdog cadence so operators see a single
        consistent retry rhythm (60 → 120 → 240 → 300s cap).
        """
        if self._supervisor_attempts == 0:
            return WATCHDOG_BACKOFF_INITIAL_SECONDS
        backoff = WATCHDOG_BACKOFF_INITIAL_SECONDS * (2 ** (self._supervisor_attempts - 1))
        return min(backoff, WATCHDOG_BACKOFF_MAX_SECONDS)

    async def stop(self) -> None:
        """Stop the supervisor (if any), all watchdog tasks, and disconnect all clients.

        Order matters: flip _running first so any loops observing it exit on
        their next iteration; then cancel the supervisor (it may be mid-sleep
        or mid-`_start_one_client`); then per-client watchdogs; then disconnect
        live clients. This keeps shutdown bounded even if a supervisor retry
        is in flight when stop() is called (#708 admin-disconnect path).
        """
        self._running = False
        if self._supervisor_task is not None:
            self._supervisor_task.cancel()
            try:
                await self._supervisor_task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.warning(
                    f"Socket Mode startup supervisor: error during cancel: {e}"
                )
            self._supervisor_task = None
        for ctx in self.contexts:
            if ctx.watchdog_task is not None:
                ctx.watchdog_task.cancel()
                ctx.watchdog_task = None
        for ctx in self.contexts:
            if ctx.client is not None:
                try:
                    await ctx.client.disconnect()
                    logger.info(f"Slack Socket Mode [c={ctx.index}]: disconnected")
                except Exception as e:
                    logger.warning(
                        f"Slack Socket Mode [c={ctx.index}]: error disconnecting: {e}"
                    )
        self.contexts = []

    async def _handle_request(self, client, req) -> None:
        """Handle incoming Socket Mode request. Shared across all N clients.

        Always acks the envelope to Slack — even on dedup hit — so Slack
        does not retry. The dedup check guards only the spawn of the
        downstream processing task, ensuring exactly-once execution.
        """
        from slack_sdk.socket_mode.response import SocketModeResponse

        envelope_id = getattr(req, "envelope_id", None)
        payload_event_type = (
            req.payload.get('event', {}).get('type')
            if isinstance(req.payload, dict) else 'n/a'
        )
        logger.info(
            f"Socket Mode received: type={req.type}, envelope_id={envelope_id}, "
            f"payload_type={payload_event_type}"
        )

        # 1) Acknowledge ALWAYS. If we suppress the ack on a dedup'd duplicate,
        #    Slack thinks the receiving connection lost it and retries forever.
        response = SocketModeResponse(envelope_id=envelope_id)
        await client.send_socket_mode_response(response)

        if req.type != "events_api":
            if req.type == "interactive":
                # Future: handle Block Kit button clicks
                logger.info(f"Received interactive event: {req.payload.get('type')}")
            else:
                logger.info(f"Unhandled Socket Mode request type: {req.type}")
            return

        # 2) Dedup check under lock — N coroutines may race here when Slack
        #    delivers the same envelope to multiple connections.
        if envelope_id is not None:
            async with self._dedup_lock:
                if envelope_id in self._envelope_seen:
                    self._dedup_hits += 1
                    logger.info(
                        f"Slack dual-delivery detected: envelope_id={envelope_id} "
                        f"already processed by another connection — ignoring duplicate "
                        f"(total dedup hits: {self._dedup_hits})"
                    )
                    return
                self._envelope_seen[envelope_id] = time.monotonic()
                while len(self._envelope_seen) > DEDUP_RING_SIZE:
                    self._envelope_seen.popitem(last=False)

        # 3) Process exactly once.
        async def _process():
            try:
                await self.on_event(req.payload)
            except Exception as e:
                logger.error(f"Error processing event: {e}", exc_info=True)
        asyncio.create_task(_process())

    async def _watchdog(self, ctx: _ClientCtx) -> None:
        """Per-client watchdog. Detects silent socket death and reconnects.

        The SDK has its own monitor (monitor_current_session) that pings and
        reconnects, but if that monitor task itself dies, the socket stays
        dead forever with no recovery. This watchdog is independent.

        Each client gets its own watchdog with its own backoff counter so a
        single failing connection cannot delay recovery on a sibling.

        Checks every WATCHDOG_INTERVAL_SECONDS:
        1. Session dead (None or closed) → reconnect
        2. SDK monitor dead (cancelled/done) → reconnect
        3. Ping/pong timeout — connection is dead → reconnect

        Uses exponential backoff (60s → 120s → 240s → 300s cap) on consecutive
        failures. Logs a prominent error when backoff hits the cap.
        """
        logger.info(f"Socket Mode watchdog [c={ctx.index}]: started")
        while self._running:
            try:
                interval = self._get_backoff_interval(ctx)
                await asyncio.sleep(interval)

                if not self._running or ctx.client is None:
                    break

                reason = self._check_health(ctx)

                if reason is None:
                    reason = await self._ping_check(ctx)

                if reason:
                    await self._attempt_reconnect(ctx, reason)
                else:
                    if ctx.consecutive_failures > 0:
                        logger.info(
                            f"Socket Mode watchdog [c={ctx.index}]: "
                            f"connection recovered, resetting backoff"
                        )
                    ctx.consecutive_failures = 0

            except asyncio.CancelledError:
                break
            except Exception as e:
                # Never crash the backend — log and continue
                logger.error(
                    f"Socket Mode watchdog [c={ctx.index}]: unexpected error: {e}",
                    exc_info=True,
                )

        logger.info(f"Socket Mode watchdog [c={ctx.index}]: stopped")

    def _check_health(self, ctx: _ClientCtx) -> Optional[str]:
        """Check session and monitor state. Returns failure reason or None."""
        client = ctx.client
        session = getattr(client, "current_session", None)

        if session is None or session.closed:
            return "session is dead/closed"

        monitor = getattr(client, "current_session_monitor", None)
        if monitor is None:
            return "SDK monitor is None"
        if monitor.done():
            return "SDK monitor task exited"

        return None

    async def _ping_check(self, ctx: _ClientCtx) -> Optional[str]:
        """Ping the WebSocket. Returns failure reason or None."""
        session = getattr(ctx.client, "current_session", None)
        if not session:
            return "session gone before ping"
        try:
            await asyncio.wait_for(session.ping(), timeout=WATCHDOG_PING_TIMEOUT_SECONDS)
            return None
        except asyncio.TimeoutError:
            return f"ping timeout ({WATCHDOG_PING_TIMEOUT_SECONDS}s)"
        except Exception as e:
            return f"ping failed: {e}"

    async def _attempt_reconnect(self, ctx: _ClientCtx, reason: str) -> None:
        """Try to reconnect this one client. Tracks consecutive failures."""
        ctx.consecutive_failures += 1
        backoff = self._get_backoff_interval(ctx)

        if backoff >= WATCHDOG_BACKOFF_MAX_SECONDS:
            logger.error(
                f"SLACK SOCKET MODE [c={ctx.index}] UNREACHABLE — {reason}. "
                f"Failed {ctx.consecutive_failures} consecutive reconnect attempts. "
                f"Next retry in {backoff}s. Check Slack app token and network connectivity."
            )
        else:
            logger.warning(
                f"Socket Mode watchdog [c={ctx.index}]: {reason} — attempting reconnect "
                f"(attempt {ctx.consecutive_failures}, next check in {backoff}s)"
            )

        try:
            # SDK's connect_to_new_endpoint uses an internal lock, so concurrent
            # calls (from SDK monitor + our watchdog) are safe.
            await ctx.client.connect_to_new_endpoint()
            logger.info(
                f"Socket Mode watchdog [c={ctx.index}]: reconnected successfully "
                f"after {ctx.consecutive_failures} attempt(s)"
            )
            ctx.consecutive_failures = 0
        except Exception as e:
            logger.error(
                f"Socket Mode watchdog [c={ctx.index}]: reconnect failed: {e}"
            )

    def _get_backoff_interval(self, ctx: _ClientCtx) -> int:
        """Exponential backoff: 60 → 120 → 240 → 300 (cap), per-client."""
        if ctx.consecutive_failures == 0:
            return WATCHDOG_INTERVAL_SECONDS
        backoff = WATCHDOG_BACKOFF_INITIAL_SECONDS * (2 ** (ctx.consecutive_failures - 1))
        return min(backoff, WATCHDOG_BACKOFF_MAX_SECONDS)
