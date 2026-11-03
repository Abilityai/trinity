"""The operator-queue poller's core (#3785, S4): `PollerCoreMixin`.

The instance state the whole service shares (initialized here, and only here),
`start`/`stop`, the #1632 cross-worker leader lease, the loop, the cycle and its
one thin WS trigger. `OperatorQueueSyncService` is composed from this mixin in
the package `__init__` (the `db/schedules` shape, Invariant #2), and the cycle
reaches the per-agent work (`_sync_agent`, …) through `self.`, i.e. the MRO.

Package-owned names (O1) are read as `_oq.X` at call time, so a patch on
`oqs.X` reaches this reader too. The one import-time read is the `__init__`
default `_oq.DEFAULT_POLL_INTERVAL`, which the package defines above
`from . import poller`. Collaborators are read as `deps.X` (O2).
"""

import asyncio
import json
import logging
import os
import uuid
from typing import Optional

from services import operator_queue_service as _oq
from utils.helpers import utc_now_iso

from . import deps

logger = logging.getLogger("services.operator_queue_service")


# `OperatorQueueSyncService` is composed from this mixin in `__init__`; the
# instance state below is the ONE initialization of every attribute the other
# methods of the service read through `self.`.
class PollerCoreMixin:
    def __init__(self, poll_interval: int = _oq.DEFAULT_POLL_INTERVAL):
        self.poll_interval = poll_interval
        self._task: Optional[asyncio.Task] = None
        self._running = False
        # #1525: (agent_name, req_id) → consecutive create-failure count. Bounds
        # the retry loop for a request whose DB create keeps raising (malformed
        # input, a DB error, …) so it can't hot-loop forever. Entry is dropped on
        # success. #1631: keyed by the (agent, id) TUPLE — two agents can now
        # share a req_id, so a bare-id key would let agent A's failing id
        # quarantine agent B's distinct, healthy request.
        self._create_failures: dict[tuple[str, str], int] = {}
        # #1631: (agent, req_id) already warned about for a reserved-prefix
        # hijack attempt — logged once, not every ~5s cycle. Bounded like the
        # quarantine map so a crafted stream of unique reserved ids can't grow it
        # without bound.
        self._rejected_reserved: set[tuple[str, str]] = set()
        # trinity-enterprise#611: (agent, req_id) already logged for a file entry
        # that re-uses the id of an ask the agent raised over the platform, and
        # the agents already told the file channel is deprecated. Logged once
        # per process each, bounded like the sets above.
        self._skipped_native: set[tuple[str, str]] = set()
        # #3243: (agent, req_id) already logged for an entry held over the
        # authoring caps — once per process, bounded like the sets above.
        self._cap_held_logged: set[tuple[str, str]] = set()
        self._file_channel_noticed: set[str] = set()
        # #1632: unique per worker process so the cross-worker leader lock only
        # ever refreshes/releases ITS OWN lease (mirror monitoring #1464).
        self._worker_id = f"{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self._is_leader = False  # last observed leadership, for transition logs
        # #1632: agent_name → monotonic ts of the last flood alert — now the
        # minimum spacing between episodes, so a flapping cap cannot re-alert
        # every cycle.
        self._flood_alert_cooldown: dict[str, float] = {}
        # #3130: (agent_name, reason) pairs whose over-cap episode has already
        # been alerted. An episode ends when a cycle reads the file and holds
        # nothing (`ingestion_cap`) or reads a sane-size file (`oversize_file`);
        # only then can the next one alert. In-memory like the cooldown: a
        # leader failover may re-alert once, and the #1677 budget bounds that.
        self._flood_episodes: set = set()
        # #3130: agent_name → the queue-file sha a `platform.ingestion` write was
        # refused against. Retried only once the file has changed, so a 412
        # from an agent still rewriting its file is not a write every 5 s.
        self._ingestion_write_refused: dict[str, str] = {}
        # #2915: consecutive failed reads per agent — hysteresis before a row is
        # called `unconfirmed` (a busy container times out intermittently).
        self._read_failures: dict[str, int] = {}
        # #2915: did any row's sync/delivery state change this cycle? ONE thin WS
        # trigger per cycle, never per agent or per item.
        self._changed_this_cycle = False

    def start(self):
        """Start the background polling loop."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._poll_loop())
        logger.info(f"Operator queue sync service started (interval={self.poll_interval}s)")

    def stop(self):
        """Stop the background polling loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None
        # #1632: hand leadership off immediately on graceful shutdown instead of
        # leaving a sibling worker idle until the TTL expires. Never raises.
        self._release_leadership()
        logger.info("Operator queue sync service stopped")

    def _leader_ttl(self) -> int:
        """Lease TTL. Refreshed once at the TOP of each `_poll_cycle`, so it must
        comfortably outlast ONE worst-case cycle plus the inter-cycle sleep — or
        the lease expires mid-cycle, a sibling grabs it, and leadership flaps
        (both workers run an overlapping cycle → the flood alert double-emits,
        since its id carries `utc_now_iso()` and so isn't deduped by
        `on_conflict`). A `_sync_agent` can take a fast read + `write_file`
        (10s timeout, `_write_responses_to_agent`), and the loop then sleeps
        `poll_interval`, so the bare `poll_interval * 3` (15s at the 5s default)
        has ~zero headroom over a single slow-writing agent. Floor at 30s so one
        slow write can't drop leadership. (Monitoring #1464 mirrors this pattern
        but at a 30s interval, where `* 3` already dwarfs its cycle.)"""
        return max(self.poll_interval * 3, 30)

    def _try_acquire_leadership(self) -> bool:
        """#1632 — cross-worker leader election for the sync loop (mirror
        monitoring #1464). Returns True iff this worker holds the lease for this
        cycle. Fail-open: if Redis is unreachable, act as leader (single-worker
        dev keeps syncing; in a Redis-down prod the DB depth cap still bounds a
        double feed, and duplicate creates are idempotent via on_conflict)."""
        r = deps.get_breaker_redis()
        if r is None:
            return True  # fail-open: no Redis → behave as the sole worker
        ttl = self._leader_ttl()
        try:
            if r.set(_oq._LEADER_KEY, self._worker_id, nx=True, ex=ttl):
                return True
            # Already held — refresh the TTL only if the lease is OURS.
            if r.get(_oq._LEADER_KEY) == self._worker_id:
                r.expire(_oq._LEADER_KEY, ttl)
                return True
            return False
        except Exception as e:
            logger.warning("operator-queue leader lock check failed-open (%s)", e)
            return True

    def _release_leadership(self) -> None:
        """Delete the lease iff we hold it (best-effort, never raises)."""
        try:
            r = deps.get_breaker_redis()
            if r is not None and r.get(_oq._LEADER_KEY) == self._worker_id:
                r.delete(_oq._LEADER_KEY)
        except Exception:
            pass

    async def _poll_loop(self):
        """Main polling loop."""
        while self._running:
            try:
                await self._poll_cycle()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Operator queue sync error: {e}")

            try:
                await asyncio.sleep(self.poll_interval)
            except asyncio.CancelledError:
                break

    async def _poll_cycle(self):
        """Single poll cycle: sync all running agents."""
        # #1632: only the lease-holding worker syncs (mirror monitoring #1464),
        # so `--workers 2` doesn't double-charge the rate limiter, double-scan
        # agent files, or double-broadcast the flood alert.
        leader = self._try_acquire_leadership()
        if leader and not self._is_leader:
            logger.info("Operator queue sync acquired leadership (worker %s)", self._worker_id)
        elif not leader and self._is_leader:
            logger.info("Operator queue sync yielded leadership (worker %s)", self._worker_id)
        self._is_leader = leader
        if not leader:
            return

        from services.docker_service import agent_container_states

        # Reset BEFORE expiry (trinity-enterprise#611): an expiry is a change, and
        # the reset used to sit below it, so an expired ask reached the UI only on
        # its 15 s poll.
        self._changed_this_cycle = False

        # Expire items past their deadline — BEFORE the running-agents gate
        # (#2915): the early return below used to sit above this, so an
        # all-stopped fleet never expired anything. Through the ask sink
        # (trinity-enterprise#611): the ledger (`disposed_by = 'timeout'`), one
        # audit row per ask, and the wake for each filer; the sink sends no
        # trigger of its own — this cycle's ONE trigger announces it.
        try:
            expired = deps.ask_service.expire().rows
            if expired:
                self._changed_this_cycle = True
                logger.info(f"Expired {len(expired)} operator queue items")
        except Exception as e:
            logger.error(f"Operator queue expiry failed: {e}")

        # trinity-enterprise#751: gated-skill approvals whose ending no observer
        # consumed, and approved runs lost between claim and start. Compare-and-
        # sets throughout, so every worker may run it; a miss waits a cycle and
        # never dispatches anything wrongly.
        try:
            from services import skill_gate_service
            await skill_gate_service.sweep()
        except Exception as e:
            logger.error(f"Skill gate sweep failed: {e}")

        # #2915: TRI-state, deliberately (#2196 class). `list_all_agents_fast`
        # collapses "Docker unreadable" into "no agents"; keyed on that, one
        # daemon blip would flip every open row fleet-wide to
        # `unconfirmed:agent_not_running` and back. `None` here means "could
        # not look" — nothing is swept and nothing is synced this cycle.
        try:
            states = agent_container_states()
        except Exception as e:
            logger.debug(f"Could not read agent container states: {e}")
            states = None
        if states is None:
            # Nothing is swept or synced — but an expiry that already happened
            # is still announced.
            if self._changed_this_cycle:
                await self._broadcast_sync()
            return

        running_agents = sorted(name for name, state in states.items() if state == "running")
        now = utc_now_iso()

        # Sweep: every open row of an agent that is NOT running is
        # `unconfirmed:agent_not_running`. Edge-triggered — at steady state the
        # WHERE matches nothing. An empty running list sweeps every open row
        # (an explicit branch in the accessor; never `notin_([])`).
        try:
            swept = deps.db.mark_operator_queue_unconfirmed(
                "agent_not_running", now, exclude_agents=running_agents,
                exclude_request_id_prefixes=_oq._RESERVED_ID_PREFIXES,
            )
            if swept:
                self._changed_this_cycle = True
        except Exception as e:
            logger.error(f"Operator queue not-running sweep failed: {e}")

        # #2989 review (AC3): an answer or terminal flip waiting on an agent that
        # is NOT running cannot reach its file — the row says so,
        # `undelivered:agent_not_running`, not merely `unconfirmed`. Edge-triggered
        # and audited once per transition; the write-back flips it to `delivered`
        # when the agent is back. Platform alarms (`not_applicable`) are skipped.
        try:
            stalled = deps.db.mark_operator_queue_undelivered_for_stopped_agents(
                now, running_agents=running_agents,
                exclude_request_id_prefixes=_oq._RESERVED_ID_PREFIXES,
            )
        except Exception as e:
            logger.error(f"Operator queue not-running delivery sweep failed: {e}")
            stalled = []
        for row in stalled:
            self._changed_this_cycle = True
            await deps._audit_sync("undeliverable", row.get("agent_name") or "", row["id"],
                                   {"status": row.get("status"), "detail": "agent_not_running"})

        if running_agents:
            # Sync each agent concurrently (with a reasonable limit)
            tasks = [self._sync_agent(name) for name in running_agents]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            # #3313: `return_exceptions=True` keeps one agent's failure from
            # cancelling the others, but it also dropped the error unlogged.
            for name, result in zip(running_agents, results):
                if isinstance(result, Exception):
                    logger.error(
                        f"Operator queue sync failed for '{name}': "
                        f"{type(result).__name__}: {result}"
                    )

        if self._changed_this_cycle:
            await self._broadcast_sync()

    async def _broadcast_sync(self):
        """ONE thin trigger per cycle (#918: identifiers only — here none; the
        store refetches the access-controlled list). Never per agent or per
        item: a fleet restart would otherwise fire hundreds of refetches."""
        if not _oq._websocket_manager:
            return
        try:
            await _oq._websocket_manager.broadcast(json.dumps({
                "type": "operator_queue_sync",
                "data": {},
            }))
        except Exception as e:
            logger.error(f"Failed to broadcast operator-queue sync: {e}")
