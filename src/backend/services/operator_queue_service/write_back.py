"""The agent-file channel's write-back half (#3785, S5): `WriteBackMixin`.

`_write_responses_to_agent` re-reads the agent's queue file, merges answers,
terminal flips, receipts and the #3130 ingestion marker into it, and writes it
back under `if_match` (#2915); `_apply_delivery_state` records whether each
one landed. Two reads happen at import: the `ingestion` default is
`filechannel._MARKER_UNCHANGED` itself (an identity sentinel, so never a
copy), and the `client` annotation is `deps.AgentClient`; both modules are
imported above.

`OperatorQueueSyncService` is composed from this mixin in the package
`__init__`. Package-owned names are read as `_oq.X`, collaborators as
`deps.X`, the file channel's helpers as `filechannel.X` and the row
vocabulary as `rows.X`. #3438, which retires the file channel, deletes this
module.
"""

import hashlib
import json
import logging
from typing import Optional

from services import operator_queue_service as _oq
from utils.helpers import utc_now_iso

from . import deps, filechannel, rows

logger = logging.getLogger("services.operator_queue_service")


# `OperatorQueueSyncService` is composed from this mixin in `__init__`; the
# instance state these methods read through `self.` is initialized there.
class WriteBackMixin:
    async def _apply_delivery_state(self, agent_name: str, row: dict, state: str,
                                    detail: Optional[str], now: str) -> bool:
        """Record whether the answer / terminal flip reached the agent's file."""
        if state not in rows.DELIVERY_STATES:
            logger.error(f"Refusing unknown delivery_state {state!r} for {row.get('id')} (programming error)")
            return False
        detail = filechannel._detail(detail) if detail else None
        if row.get("delivery_state") == state and (row.get("delivery_detail") or "") == (detail or ""):
            return False
        try:
            changed = deps.db.set_operator_queue_delivery_state(row["id"], state, detail, now)
        except Exception as e:
            logger.error(f"Failed to record delivery state for {row.get('id')}: {e}")
            return False
        if not changed:
            return False
        row["delivery_state"] = state
        row["delivery_detail"] = detail
        self._changed_this_cycle = True
        if state == rows.DELIVERY_DELIVERED:
            await deps._audit_sync("written_back", agent_name, row["id"], {"status": row.get("status")})
        elif state == rows.DELIVERY_UNDELIVERED:
            await deps._audit_sync("undeliverable", agent_name, row["id"],
                                   {"status": row.get("status"), "detail": detail})
        return True

    async def _write_responses_to_agent(
        self,
        agent_name: str,
        client: deps.AgentClient,
        queue_data: dict,
        responded_items: list,
        terminal_items: Optional[list] = None,
        file_exists: bool = True,
        *,
        content_sha: Optional[str] = None,
        receipt_request_ids=None,
        ingestion=filechannel._MARKER_UNCHANGED,
    ) -> Optional[bool]:
        """Write operator responses back to the agent's queue file.

        #2915 — the write is honest and narrow. Before writing, the file is
        READ AGAIN and merged by id, and the write carries `if_match` (the sha of
        what was read) so the agent server refuses (412) rather than clobbers an
        entry the agent appended in between — the window is narrowed to one
        round trip, and a refused write is recorded and retried, not lost. A
        response is delivered ONLY into an entry that is still `pending` and
        whose content still matches the row (an answer to a rewritten question
        is `undelivered:entry_changed`). Every outcome lands on the row as a
        delivery state; nothing is dropped silently.

        #3130: ``ingestion`` is the file-level `platform.ingestion` marker to
        leave in the file (a dict), to remove (``None``), or
        ``_MARKER_UNCHANGED``. Returns True when a write landed, False when one
        was attempted and refused or failed, None when nothing was written.
        """
        now = utc_now_iso()
        terminal_items = terminal_items or []
        receipt_request_ids = set(receipt_request_ids or ())
        marker_wanted = ingestion is not filechannel._MARKER_UNCHANGED
        if (not responded_items and not terminal_items and not receipt_request_ids
                and not marker_wanted):
            return None

        # Re-read immediately before writing (the cycle-start read may be a whole
        # cycle old). A transient failure here means "try next cycle" — the
        # agent WAS readable seconds ago, so nothing is recorded for it.
        if file_exists:
            try:
                fresh = await client.read_file(_oq.QUEUE_FILE_PATH, timeout=5.0)
            except Exception as e:
                logger.warning(f"Re-read before write-back failed for {agent_name}: {type(e).__name__}")
                return
            if not fresh.get("success"):
                return
            if fresh.get("not_found") or not fresh.get("content"):
                # The cycle-start read HAD a file and this re-read does not: an
                # agent mid-rewrite, not a lost file. Write nothing, record
                # nothing — the next cycle decides. (A file missing at BOTH reads
                # is the container-restart case and is reconstructed below.)
                logger.info(f"operator-queue.json for {agent_name} vanished between the two reads; not writing")
                return
            try:
                queue_data = json.loads(fresh["content"])
            except json.JSONDecodeError:
                logger.warning(f"Re-read of operator-queue.json for {agent_name} is not JSON; not writing")
                return
            if not filechannel._well_formed_queue(queue_data):
                logger.warning(f"Re-read of operator-queue.json for {agent_name} is not the expected shape; not writing")
                return
            content_sha = hashlib.sha256(fresh["content"].encode("utf-8")).hexdigest()

        requests = queue_data.get("requests") or []
        updated = False
        terminal_flips = 0

        # #1631: the DB `id` is now a platform uuid; the agent's file entries are
        # keyed by the string the agent authored, which is persisted as the row's
        # `request_id`. So every match against a file entry (`req.get("id")`)
        # MUST key on `request_id`, not the DB `id` — otherwise write-back
        # silently stops matching and the agent never sees its answer.
        # #2915: a platform-minted row (ent#499) has no loop to close — it is
        # `not_applicable` and takes NO part in delivery. Files written before
        # ent#499 still carry those alarms as `responded` entries; matching them
        # here flip-flopped the row between delivered and not_applicable every
        # cycle, minting an audit row each time (seen live on the first run).
        response_map = {
            item["request_id"]: item for item in responded_items
            if not rows.is_platform_minted(item)
        }
        # Cancelled/expired items (#1017): flip still-'pending' file entries
        # to their terminal status so the agent stops waiting (and so a
        # stale 'pending' file entry can't resurrect a purged row). Never
        # appended if missing from the file.
        terminal_map = {
            item["request_id"]: item for item in (terminal_items or [])
            if not rows.is_platform_minted(item)
        }
        delivered: dict = {}      # row id -> row, IN this write (or already there)
        undelivered: dict = {}    # row id -> (row, detail), decided before the write

        # Update items already in the agent's requests array
        seen_ids = set()
        for req in requests:
            if not isinstance(req, dict):
                continue
            req_id = req.get("id")
            if not isinstance(req_id, str):
                # #3313: every map here is keyed on a row's string request_id,
                # so a non-string id names no row — and a list/object one made
                # the lookup below raise, aborting the whole write-back.
                continue
            if req_id in response_map:
                resp = response_map[req_id]
                # #2989 review: "send again to answer anyway" must reach the file.
                # The operator saw the divergence and acknowledged it at respond
                # time (`divergence_acknowledged_at`), so the answer is written
                # into the entry as it is now — rewritten or closed by the agent.
                forced = bool(resp.get("divergence_acknowledged_at"))
                if req.get("status") == "pending":
                    if filechannel.changed_fields(resp, req) and not forced:
                        # The agent rewrote the question after the human answered
                        # it. Never hand an answer to a different question.
                        undelivered[resp["id"]] = (resp, "entry_changed")
                    else:
                        filechannel._deliver_into(req, resp)
                        updated = True
                        delivered[resp["id"]] = resp
                elif filechannel._is_our_answer(req, resp):
                    delivered[resp["id"]] = resp  # a previous write landed
                elif forced:
                    # The agent closed the entry on its side; the acknowledged
                    # answer is WRITTEN into it (status → responded) so the agent
                    # finds it. Recording `delivered` without a write let the next
                    # cycle read the agent's own `acknowledged` as an ack of an
                    # answer it never saw.
                    filechannel._deliver_into(req, resp)
                    updated = True
                    delivered[resp["id"]] = resp
                else:
                    undelivered[resp["id"]] = (resp, "closed_by_filer")
            elif req_id in terminal_map:
                term = terminal_map[req_id]
                if req.get("status") == "pending":
                    req["status"] = term["status"]
                    updated = True
                    terminal_flips += 1
                delivered[term["id"]] = term
            if req_id in receipt_request_ids and req.get("status") == "pending":
                block = req.get(filechannel._PLATFORM_BLOCK_KEY)
                if not isinstance(block, dict):
                    block = {}
                    req[filechannel._PLATFORM_BLOCK_KEY] = block
                if not block.get(filechannel._AGING_SINCE_KEY):
                    block[filechannel._AGING_SINCE_KEY] = now
                    updated = True
            if req_id:
                seen_ids.add(req_id)

        # Reconstruct items missing from the file (e.g. after container restart).
        # #1631: write the agent's own `request_id` back as the file entry's
        # `id` (never the DB uuid) so the agent recognises the item and the next
        # sync cycle's index — keyed on request_id — matches instead of
        # creating a duplicate.
        for resp in responded_items:
            # ent#499: a platform alarm was never in this agent's file and must
            # not be written into it — see `is_platform_minted`.
            if rows.is_platform_minted(resp):
                continue
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
                    "responded_at": resp.get("responded_at"),  # #715: never who
                })
                updated = True
                delivered[resp["id"]] = resp

        # #2915: a platform alarm has no loop to close — `not_applicable`, once,
        # whether it is still `responded` or already terminal.
        for row in list(responded_items) + list(terminal_items or []):
            if rows.is_platform_minted(row):
                await self._apply_delivery_state(
                    agent_name, row, rows.DELIVERY_NOT_APPLICABLE, "platform_minted", now
                )
        # #2915: a terminal flip whose entry is gone is recorded, not dropped.
        for term in terminal_map.values():
            if term["request_id"] not in seen_ids:
                undelivered[term["id"]] = (term, "entry_missing" if file_exists else "file_missing")
        for row, detail in undelivered.values():
            await self._apply_delivery_state(agent_name, row, rows.DELIVERY_UNDELIVERED, detail, now)

        # #3130: the file-level hold marker rides the same guarded write.
        if marker_wanted and filechannel._apply_ingestion_marker(queue_data, ingestion, now):
            updated = True

        if not updated:
            for row in delivered.values():
                await self._apply_delivery_state(agent_name, row, rows.DELIVERY_DELIVERED, None, now)
            return None

        queue_data["requests"] = requests

        # Write the updated file back to the agent
        try:
            new_content = json.dumps(queue_data, indent=2)
            result = await client.write_file(
                _oq.QUEUE_FILE_PATH,
                new_content,
                timeout=10.0,
                platform=True,  # Allow writes to .trinity directory
                if_match=content_sha,
            )
        except Exception as e:
            result = {"success": False, "error": type(e).__name__}

        if result.get("success"):
            logger.info(
                f"Wrote {len(response_map)} responses and {terminal_flips} "
                f"terminal-status flips back to {agent_name}"
            )
            for row in delivered.values():
                await self._apply_delivery_state(agent_name, row, rows.DELIVERY_DELIVERED, None, now)
            return True
        else:
            code = result.get("status_code")
            if code == 412:
                detail = "conflict"      # the agent wrote in between — retried next cycle
            elif isinstance(code, int):
                detail = f"http_{code}"
            else:
                detail = filechannel._read_failure_detail(result)
            logger.warning(
                f"Failed to write responses to {agent_name}: {detail}"
            )
            for row in delivered.values():
                await self._apply_delivery_state(agent_name, row, rows.DELIVERY_UNDELIVERED, detail, now)
            return False
