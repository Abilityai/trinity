"""
Operator Queue Sync Service (OPS-001).

Background service that polls agent containers for operator-queue.json files,
syncs new requests to the database, and writes operator responses back to
agent files.

Polling cycle:
  1. Get list of running agents
  2. For each agent, read ~/.trinity/operator-queue.json
  3. Detect new 'pending' entries -> create DB records, broadcast WebSocket
  4. Detect 'acknowledged' entries -> update DB records
  5. Write operator responses back to agent JSON files
  6. Handle expired entries
"""

import asyncio
import json
import logging
from typing import Optional

from database import db
from services.agent_client import AgentClient

logger = logging.getLogger(__name__)

# WebSocket manager injected from main.py
_websocket_manager = None

QUEUE_FILE_PATH = ".trinity/operator-queue.json"
DEFAULT_POLL_INTERVAL = 5  # seconds
# #1525: after this many consecutive failed create attempts for the same request
# id, quarantine it — stop re-attempting so one persistently-failing create can't
# produce an unbounded ~5s ERROR loop. It's retried again after a process restart
# (or if it leaves `pending` / gets created some other way).
MAX_CREATE_ATTEMPTS = 3
# Safety valve so the in-memory quarantine map can never grow without bound if an
# agent streams unique failing ids; cleared wholesale past this size.
_MAX_QUARANTINE_ENTRIES = 5000

# #1631: the platform mints its OWN operator-queue items with these id prefixes
# (poison-park in lease_reaper_service, sync-failing / git-bloat in
# sync_health_service, cb-dormant in agent_client, skill-not-found in
# task_execution_service). An agent could pre-claim one of these ids in its own
# ~/.trinity/operator-queue.json to hijack or suppress the platform's alert for
# itself, so an agent-AUTHORED request id starting with a reserved prefix is
# rejected here (the platform creates its own items directly, never through this
# sync loop, so this never blocks a legitimate platform alert).
_RESERVED_ID_PREFIXES = (
    "poison-",
    "sync-failing-",
    "git-bloat-",
    "cb-dormant-",
    "skill-not-found-",
)


def set_websocket_manager(manager):
    """Set the WebSocket manager for broadcasting events."""
    global _websocket_manager
    _websocket_manager = manager


class OperatorQueueSyncService:
    """Background service that syncs operator queue files with the database."""

    def __init__(self, poll_interval: int = DEFAULT_POLL_INTERVAL):
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
        logger.info("Operator queue sync service stopped")

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
        from services.docker_service import list_all_agents_fast

        try:
            agents = list_all_agents_fast()
        except Exception as e:
            logger.debug(f"Could not list agents: {e}")
            return

        running_agents = [a.name for a in agents if a.status == "running"]
        if not running_agents:
            return

        # Expire items past their deadline
        expired_count = db.mark_operator_queue_expired()
        if expired_count > 0:
            logger.info(f"Expired {expired_count} operator queue items")

        # Sync each agent concurrently (with a reasonable limit)
        tasks = [self._sync_agent(name) for name in running_agents]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _sync_agent(self, agent_name: str):
        """Sync a single agent's operator queue file."""
        client = AgentClient(agent_name)

        # 1. Read the queue file from the agent
        try:
            result = await client.read_file(QUEUE_FILE_PATH, timeout=5.0)
        except Exception:
            # Agent not reachable or file API not ready — skip silently
            return

        file_exists = result.get("success") and not result.get("not_found")

        if file_exists:
            content = result.get("content")
            if not content:
                file_exists = False

        if file_exists:
            try:
                queue_data = json.loads(content)
            except json.JSONDecodeError:
                logger.warning(f"Invalid JSON in operator-queue.json for {agent_name}")
                queue_data = {"$schema": "operator-queue-v1", "requests": []}
        else:
            queue_data = {"$schema": "operator-queue-v1", "requests": []}

        requests = queue_data.get("requests", [])

        # 2. Process each request
        new_items = []
        acknowledged_items = []

        for req in requests:
            req_id = req.get("id")
            if not req_id:
                continue

            # #1631: reject an agent-authored id that impersonates a platform id
            # prefix (hijack/suppress guard). Log once per (agent, id) so it
            # can't hot-loop the ~5s sync at WARNING.
            if req_id.startswith(_RESERVED_ID_PREFIXES):
                key = (agent_name, req_id)
                if key not in self._rejected_reserved:
                    if len(self._rejected_reserved) >= _MAX_QUARANTINE_ENTRIES:
                        self._rejected_reserved.clear()  # safety valve
                    self._rejected_reserved.add(key)
                    logger.warning(
                        f"Rejecting operator-queue request '{req_id}' from "
                        f"'{agent_name}': ids with a reserved platform prefix are "
                        f"minted only by the platform (#1631)"
                    )
                continue

            fail_key = (agent_name, req_id)
            req_status = req.get("status", "pending")

            if req_status == "pending" and not db.operator_queue_item_exists(agent_name, req_id):
                # #1525: quarantine a request whose create keeps failing so a
                # single malformed/unpersistable entry can't hot-loop the ~5s
                # sync (the row never persists → exists() stays False → retry
                # forever). Skip once it has hit the attempt cap.
                if self._create_failures.get(fail_key, 0) >= MAX_CREATE_ATTEMPTS:
                    continue
                # New item — create in DB
                try:
                    db.create_operator_queue_item(agent_name, req)
                    new_items.append(req)
                    self._create_failures.pop(fail_key, None)  # recovered — clear count
                except Exception as e:
                    attempts = self._create_failures.get(fail_key, 0) + 1
                    if len(self._create_failures) >= _MAX_QUARANTINE_ENTRIES:
                        self._create_failures.clear()  # safety valve — never unbounded
                    self._create_failures[fail_key] = attempts
                    if attempts >= MAX_CREATE_ATTEMPTS:
                        logger.error(
                            f"Quarantining operator-queue request {req_id} for "
                            f"'{agent_name}' after {attempts} failed create attempts "
                            f"(last error: {e}); it won't be retried until it changes "
                            f"or the service restarts"
                        )
                    else:
                        logger.error(
                            f"Failed to create queue item {req_id} for '{agent_name}' "
                            f"(attempt {attempts}/{MAX_CREATE_ATTEMPTS}): {e}"
                        )

            elif req_status == "acknowledged":
                # Agent acknowledged our response. #1631: broadcast the row's
                # platform uuid (returned here), not the agent's `req_id` — the
                # frontend store keys items by uuid `id`, so an ack keyed on
                # request_id would never match a live item.
                ack_uuid = db.mark_operator_queue_acknowledged(agent_name, req_id)
                if ack_uuid:
                    acknowledged_items.append(ack_uuid)

        # 3. Broadcast new items via WebSocket
        if new_items and _websocket_manager:
            for item in new_items:
                try:
                    await _websocket_manager.broadcast(json.dumps({
                        "type": "operator_queue_new",
                        "data": {
                            "id": item.get("id", ""),
                            "agent_name": agent_name,
                            "type": item.get("type", "question"),
                            "priority": item.get("priority", "medium"),
                            "title": item.get("title", ""),
                            "created_at": item.get("created_at", ""),
                        }
                    }))
                except Exception as e:
                    logger.error(f"Failed to broadcast queue event: {e}")

        if acknowledged_items and _websocket_manager:
            for ack_id in acknowledged_items:
                try:
                    await _websocket_manager.broadcast(json.dumps({
                        "type": "operator_queue_acknowledged",
                        "data": {
                            "id": ack_id,
                            "agent_name": agent_name,
                        }
                    }))
                except Exception:
                    pass

        # 4. Write responses back to the agent's file. Cancelled/expired
        # items are propagated too (#1017) so the agent stops waiting on
        # them — but only as in-place status flips on entries still in the
        # file.
        responded_items = db.get_operator_queue_responded_for_agent(agent_name)
        terminal_items = (
            db.get_operator_queue_terminal_for_agent(agent_name)
            if file_exists else []
        )
        if responded_items or terminal_items:
            await self._write_responses_to_agent(
                agent_name, client, queue_data, responded_items,
                terminal_items, file_exists
            )

    async def _write_responses_to_agent(
        self,
        agent_name: str,
        client: AgentClient,
        queue_data: dict,
        responded_items: list,
        terminal_items: Optional[list] = None,
        file_exists: bool = True,
    ):
        """Write operator responses back to the agent's queue file."""
        requests = queue_data.get("requests", [])
        updated = False
        terminal_flips = 0

        # #1631: the DB `id` is now a platform uuid; the agent's file entries are
        # keyed by the string the agent authored, which is persisted as the row's
        # `request_id`. So every match against a file entry (`req.get("id")`)
        # MUST key on `request_id`, not the DB `id` — otherwise write-back
        # silently stops matching and the agent never sees its answer.
        response_map = {item["request_id"]: item for item in responded_items}
        # Cancelled/expired items (#1017): flip still-'pending' file entries
        # to their terminal status so the agent stops waiting (and so a
        # stale 'pending' file entry can't resurrect a purged row). Never
        # appended if missing from the file.
        terminal_map = {item["request_id"]: item for item in (terminal_items or [])}

        # Update items already in the agent's requests array
        seen_ids = set()
        for req in requests:
            req_id = req.get("id")
            if req_id in response_map and req.get("status") == "pending":
                resp = response_map[req_id]
                req["status"] = "responded"
                req["response"] = resp["response"]
                req["response_text"] = resp.get("response_text")
                req["responded_by"] = resp.get("responded_by_email")
                req["responded_at"] = resp.get("responded_at")
                updated = True
            elif req_id in terminal_map and req.get("status") == "pending":
                req["status"] = terminal_map[req_id]["status"]
                updated = True
                terminal_flips += 1
            if req_id:
                seen_ids.add(req_id)

        # Reconstruct items missing from the file (e.g. after container restart).
        # #1631: write the agent's own `request_id` back as the file entry's
        # `id` (never the DB uuid) so the agent recognises the item and the next
        # sync cycle's exists() check — keyed on request_id — matches instead of
        # creating a duplicate.
        for resp in responded_items:
            if resp["request_id"] not in seen_ids:
                requests.append({
                    "id": resp["request_id"],
                    "type": resp.get("type", "question"),
                    "status": "responded",
                    "priority": resp.get("priority", "medium"),
                    "title": resp.get("title", ""),
                    "question": resp.get("question", ""),
                    "options": resp.get("options"),
                    "context": resp.get("context"),
                    "created_at": resp.get("created_at", ""),
                    "response": resp["response"],
                    "response_text": resp.get("response_text"),
                    "responded_by": resp.get("responded_by_email"),
                    "responded_at": resp.get("responded_at"),
                })
                updated = True

        if not updated:
            return

        queue_data["requests"] = requests

        # Write the updated file back to the agent
        try:
            new_content = json.dumps(queue_data, indent=2)
            result = await client.write_file(
                QUEUE_FILE_PATH,
                new_content,
                timeout=10.0,
                platform=True,  # Allow writes to .trinity directory
            )
            if result.get("success"):
                logger.info(
                    f"Wrote {len(response_map)} responses and {terminal_flips} "
                    f"terminal-status flips back to {agent_name}"
                )
            else:
                logger.warning(
                    f"Failed to write responses to {agent_name}: {result.get('error')}"
                )
        except Exception as e:
            logger.error(f"Error writing responses to {agent_name}: {e}")


# Global service instance
operator_queue_service = OperatorQueueSyncService()
