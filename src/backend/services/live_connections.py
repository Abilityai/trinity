"""Live connections follow the account (abilityai/trinity-enterprise#863).

A long-lived connection (the ``/ws`` event socket, the ``/ws/events`` event
stream) is authorized once, when it opens. This module keeps it valid only
while the account it was opened for still reads the way it did then.

**The one rule.** A door records an :class:`AccessSnapshot` of the account row
it read when the connection opened. A re-check reads the row again and closes
the connection when the row is gone or deactivated, when its key is no longer
active, or when the fresh row's snapshot differs from the recorded one (role,
email, password). The comparison is the snapshot type's own equality, so the
fields compared and the fields recorded cannot drift apart.

**Two re-checks, one comparison.**

* The **tick**: every :data:`RECHECK_INTERVAL_S` seconds each worker re-reads,
  in one batched query, the rows (and key states) of every connection it holds.
  It needs no change to any writer, so it covers every writer: a route, a
  private module, a direct database edit. It runs in **every** worker with **no
  leader lease**, by design: the connections it checks are that process's own.
  With no connection held it reads nothing. A read that fails keeps every
  connection open and logs; a row that is definitively gone still closes.
* The **immediate** re-check: a writer that has just changed an account or a
  key calls :func:`notify_account_changed` or :func:`notify_key_changed` after
  its write commits. The same comparison runs at once, for that account's (or
  that key's) connections only, on the worker that served the write. Because a
  writer reported the change, a read that fails here closes those connections
  (with the reconnect code, so each door re-checks at its next connect).

**Ordering.** A door registers (:func:`admit`) before it reads the account, and
nothing awaits between the read and the snapshot it records. A re-check that
reaches an entry with no snapshot yet marks it dirty, and the door refuses the
connection instead of activating it (the client's reconnect is the re-read). A
re-check that decides to close an entry the door has not finished connecting
marks it revoked; the door checks that with :meth:`LiveConnection.go_live` once
it is connected and closes itself. So a change that commits at any point while
a door is opening either is seen by the door's read or reaches its entry.

**Thread contract.** The registry belongs to the event loop that started it (or
first admitted a connection). Every registry access and every closer runs on
that loop. :func:`notify_account_changed` and :func:`notify_key_changed` are
plain synchronous functions, safe from the loop thread and from any other
thread: off the loop they hand the work over with ``call_soon_threadsafe`` and
return once it is queued.

**Closing.** A door supplies an async ``closer(reason)``. It must end the
connection without cancelling the task that serves it (cancelling an ASGI
connection task makes the server log an error per close). ``reason`` is a
:class:`CloseReason`; each door maps it to its own close code.

This module imports nothing from the event bus.
"""
from __future__ import annotations

import asyncio
import enum
import hashlib
import logging
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# The re-check period: a code constant, deliberately not an env setting.
RECHECK_INTERVAL_S = 15.0
# A closer that has not finished within this is abandoned and retried next tick.
CLOSE_TIMEOUT_S = 5.0

DOOR_WS = "ws"
DOOR_EVENT_STREAM = "ws_events"


class CloseReason(str, enum.Enum):
    """Why a connection is closed.

    ``CHANGED``: the account is still active but no longer reads as it did when
    the connection opened (role, email or password), so the client reconnects at
    the new scope. ``ENDED``: the account is deactivated or gone, or the key the
    connection authenticated with is no longer active, so it does not reconnect.
    """

    CHANGED = "changed"
    ENDED = "ended"


@dataclass(frozen=True)
class AccessSnapshot:
    """What a door read about an account when a connection opened.

    Equality IS the comparison: two snapshots that differ in any field mean the
    connection's scope may have changed. The password is held only as a digest
    of the stored hash.
    """

    exists: bool
    role: Optional[str]
    suspended: bool
    email: str
    password_digest: str

    @classmethod
    def of(cls, row: Optional[Mapping[str, Any]]) -> "AccessSnapshot":
        if not row:
            return cls(exists=False, role=None, suspended=False, email="", password_digest="")
        stored = row.get("password") or ""
        return cls(
            exists=True,
            role=row.get("role"),
            suspended=bool(row.get("suspended_at")),
            email=row.get("email") or "",
            password_digest=hashlib.sha256(stored.encode("utf-8")).hexdigest() if stored else "",
        )


def access_snapshot(row: Optional[Mapping[str, Any]]) -> AccessSnapshot:
    """The snapshot of a user row a door has just read (``None`` = no row)."""
    return AccessSnapshot.of(row)


def read_access(username: str) -> AccessSnapshot:
    """Read ``username``'s row now and return its snapshot (synchronous)."""
    from database import db

    return AccessSnapshot.of(db.get_user_by_username(username))


def _read_state(usernames: Set[str], key_ids: Set[str]) -> Tuple[Dict[str, Dict[str, Any]], Set[str]]:
    """The batched read behind every re-check: current user rows by username,
    and the ids of the keys that exist and are active. Runs in a worker thread."""
    from database import db

    rows = db.get_users_access_state(usernames) if usernames else {}
    active = db.get_keys_active(key_ids) if key_ids else set()
    return rows, active


Closer = Callable[[CloseReason], Awaitable[None]]
StateReader = Callable[[Set[str], Set[str]], Tuple[Dict[str, Dict[str, Any]], Set[str]]]


def _verdict(entry: "LiveConnection", row: Optional[Mapping[str, Any]], key_active: bool) -> Optional[CloseReason]:
    """The one comparison, shared by the tick and the immediate re-check."""
    if not row or row.get("suspended_at"):
        return CloseReason.ENDED
    if entry.key_id is not None and not key_active:
        return CloseReason.ENDED
    if AccessSnapshot.of(row) != entry.snapshot:
        return CloseReason.CHANGED
    return None


class LiveConnection:
    """One open connection's entry, and the handle its door holds.

    Every method runs on the registry's loop (the door's own handler)."""

    def __init__(
        self,
        registry: "LiveConnectionRegistry",
        door: str,
        username: str,
        closer: Closer,
        key_id: Optional[str],
        snapshot: Optional[AccessSnapshot],
    ) -> None:
        self.entry_id = uuid.uuid4().hex
        self.door = door
        self.username = username
        self.key_id = key_id
        self.snapshot = snapshot
        self._registry = registry
        self._closer = closer
        self._dirty = False
        self._live = False
        self._released = False
        self._revoked: Optional[CloseReason] = None
        self._closing = False

    @property
    def revoked(self) -> Optional[CloseReason]:
        """Why a re-check decided to close this connection, else ``None``."""
        return self._revoked

    @property
    def live(self) -> bool:
        return self._live

    def activate(self, snapshot: AccessSnapshot) -> bool:
        """Record the snapshot of the read the door has just made.

        Returns ``False`` when a re-check reached this entry before it had a
        snapshot: the door's read may predate the change, so the door refuses
        the connection (with the reconnect code) instead of serving it."""
        self._registry._assert_owner_loop()
        if self._dirty:
            return False
        self.snapshot = snapshot
        return True

    def go_live(self) -> bool:
        """Mark the connection fully open. From now on a re-check calls the
        closer. Returns ``False`` when a re-check decided to close it while it
        was opening; the door then closes it itself, for :attr:`revoked`."""
        self._registry._assert_owner_loop()
        if self.snapshot is None:
            raise RuntimeError("live connection: go_live() before activate()")
        if self._revoked is not None:
            return False
        self._live = True
        return True

    def release(self) -> None:
        """Remove the entry. Idempotent; every door calls it when the
        connection ends, whatever ended it."""
        if self._released:
            return
        self._released = True
        self._registry._entries.pop(self.entry_id, None)


class LiveConnectionRegistry:
    """The per-worker registry of open connections and its re-check loop."""

    def __init__(
        self,
        *,
        read_state: Optional[StateReader] = None,
        interval: float = RECHECK_INTERVAL_S,
    ) -> None:
        self._entries: Dict[str, LiveConnection] = {}
        self._read_state: StateReader = read_state or _read_state
        self._interval = interval
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._task: Optional[asyncio.Task] = None
        self._inflight: Set[asyncio.Task] = set()

    # ------------------------------------------------------------ the loop

    def _assert_owner_loop(self) -> None:
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is None or running is not self._loop:
            raise RuntimeError("live connections: registry used off its event loop")

    def _bind_loop(self) -> None:
        """Adopt the running loop as the owner. A registry whose previous loop
        has stopped (a test's loop, a restarted app) is re-bound, dropping the
        entries of connections that loop can no longer serve."""
        running = asyncio.get_running_loop()
        if running is self._loop:
            return
        old = self._loop
        if old is not None and not old.is_closed() and old.is_running():
            raise RuntimeError("live connections: registry is owned by another running event loop")
        if self._entries:
            logger.warning(
                "live connections: dropping %d entries left by a stopped event loop", len(self._entries)
            )
            self._entries.clear()
        self._inflight.clear()
        self._task = None
        self._loop = running

    async def start(self) -> None:
        """Bind the registry to the running loop and start the tick."""
        self._bind_loop()
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="live_connections_recheck")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        inflight = list(self._inflight)
        for t in inflight:
            t.cancel()
        if inflight:
            await asyncio.gather(*inflight, return_exceptions=True)

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            try:
                await self.revalidate()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — the loop must outlive one bad tick
                logger.exception("live connections: re-check tick failed; next in %.0fs", self._interval)

    # ------------------------------------------------------------ admit

    def admit(
        self,
        door: str,
        username: str,
        closer: Closer,
        *,
        key_id: Optional[str] = None,
        snapshot: Optional[AccessSnapshot] = None,
    ) -> LiveConnection:
        """Register a connection before its door reads the account.

        Without ``snapshot`` the entry is provisional until
        :meth:`LiveConnection.activate`. Call on the door's own loop."""
        if not username:
            raise ValueError("live connections: admit() needs the account's username")
        self._bind_loop()
        entry = LiveConnection(self, door, username, closer, key_id, snapshot)
        self._entries[entry.entry_id] = entry
        return entry

    def count(self) -> int:
        return len(self._entries)

    # ------------------------------------------------------------ re-checks

    def recheck_account(self, username: str) -> None:
        """Re-check ``username``'s connections on this worker now. Synchronous
        and safe from any thread; returns once the re-check is queued."""
        if username:
            self._hand_over(lambda e: e.username == username, f"account {username!r}")

    def recheck_key(self, key_id: str) -> None:
        """Re-check the connections opened with key ``key_id`` on this worker
        now. Synchronous and safe from any thread."""
        if key_id:
            self._hand_over(lambda e: e.key_id == key_id, "key")

    def _hand_over(self, match: Callable[[LiveConnection], bool], what: str) -> None:
        loop = self._loop
        if loop is None:
            return  # nothing was ever admitted on this worker: nothing to close
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            self._begin_recheck(match)
            return
        if loop.is_closed() or not loop.is_running():
            logger.warning("live connections: no running event loop to re-check %s on", what)
            return
        try:
            loop.call_soon_threadsafe(self._begin_recheck, match)
        except RuntimeError:  # the loop closed between the check and the hand-over
            logger.warning("live connections: event loop closed before re-checking %s", what, exc_info=True)

    def _collect(self, match: Callable[[LiveConnection], bool]) -> List[LiveConnection]:
        """The entries a re-check compares. A provisional entry is marked dirty
        instead; one already being closed is left to that close."""
        picked = []
        for entry in list(self._entries.values()):
            if not match(entry):
                continue
            if entry.snapshot is None:
                entry._dirty = True
            elif entry._revoked is None:
                picked.append(entry)
        return picked

    def _begin_recheck(self, match: Callable[[LiveConnection], bool]) -> None:
        """On the loop: collect now (so an entry admitted after this point read
        the account after the change), then read and compare in a task."""
        entries = self._collect(match)
        if not entries:
            return
        task = asyncio.get_running_loop().create_task(self._recheck(entries, immediate=True))
        self._inflight.add(task)
        task.add_done_callback(self._inflight.discard)

    async def revalidate(self) -> None:
        """One tick: compare every connection this worker holds."""
        entries = self._collect(lambda e: True)
        if entries:
            await self._recheck(entries, immediate=False)

    async def _recheck(self, entries: List[LiveConnection], *, immediate: bool) -> None:
        usernames = {e.username for e in entries}
        key_ids = {e.key_id for e in entries if e.key_id is not None}
        try:
            rows, active = await asyncio.to_thread(self._read_state, usernames, key_ids)
        except Exception:  # noqa: BLE001 — decided per path below, never swallowed
            if not immediate:
                logger.error(
                    "live connections: re-check read failed; keeping %d connections open until the next tick",
                    len(entries), exc_info=True,
                )
                return
            logger.warning(
                "live connections: re-check read failed after a reported change; closing %d connections",
                len(entries), exc_info=True,
            )
            await asyncio.gather(*(self._close(entry, CloseReason.CHANGED) for entry in entries))
            return
        # Closes run side by side, each bounded by CLOSE_TIMEOUT_S, so a
        # connection that is slow to close holds up no other one.
        closing = []
        for entry in entries:
            if entry._released:
                continue
            reason = _verdict(entry, rows.get(entry.username), entry.key_id in active)
            if reason is not None:
                closing.append(self._close(entry, reason))
        if closing:
            await asyncio.gather(*closing)

    async def _close(self, entry: LiveConnection, reason: CloseReason) -> None:
        if entry._closing or entry._released:
            return
        entry._revoked = reason
        if not entry._live:
            return  # the door sees it at go_live() and closes itself
        entry._closing = True
        try:
            await asyncio.wait_for(entry._closer(reason), timeout=CLOSE_TIMEOUT_S)
            logger.info(
                "live connections: closed a %s connection of %r (%s)", entry.door, entry.username, reason.value
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — one failing closer must not stop the others
            logger.exception(
                "live connections: closing a %s connection of %r failed; retrying next tick",
                entry.door, entry.username,
            )
            entry._revoked = None
            entry._closing = False


# The worker's registry. Doors and writers use the module functions below.
registry = LiveConnectionRegistry()


def admit(
    door: str,
    username: str,
    closer: Closer,
    *,
    key_id: Optional[str] = None,
    snapshot: Optional[AccessSnapshot] = None,
) -> LiveConnection:
    """Register a connection with this worker's registry (see the class)."""
    return registry.admit(door, username, closer, key_id=key_id, snapshot=snapshot)


def notify_account_changed(username: str) -> None:
    """A writer changed ``username``'s account: re-check its connections now.

    Call after the write commits. Synchronous, safe from the event-loop thread
    and from any other thread; returns once the re-check is queued."""
    registry.recheck_account(username)


def notify_key_changed(key_id: str) -> None:
    """A writer changed MCP key ``key_id``: re-check the connections opened
    with it now. Same contract as :func:`notify_account_changed`."""
    registry.recheck_key(key_id)


async def start() -> None:
    await registry.start()


async def stop() -> None:
    await registry.stop()
