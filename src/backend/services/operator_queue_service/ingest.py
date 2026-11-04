"""The agent-file channel's ingest half (#3785, S5): `IngestMixin`.

`_sync_agent` reads one agent's `~/.trinity/operator-queue.json`, creates rows
for its new entries under the #1632 caps, reconciles the rows it already holds
(#2915), and hands the answers back to the write-back. `_apply_sync_state`
records what a cycle established about a row; `_notice_file_channel` names the
channel's deprecation (trinity-enterprise#611). `_sync_agent` shares this
module with the two small methods because a mixin holding it alone scores over
the /code-health ceiling.

`OperatorQueueSyncService` is composed from this mixin in the package
`__init__` (the `db/schedules` shape, Invariant #2). Everything that crosses a
mixin goes through `self.`: the flood alert and its episode end
(`_maybe_emit_flood_alert`, `_end_flood_episode`), the write-back, the
instance state. Package-owned names are read as `_oq.X`, collaborators as
`deps.X`, the file channel's helpers as `filechannel.X` and the row
vocabulary as `rows.X`. #3438, which retires the file channel, deletes this
module.
"""

import hashlib
import json
import logging
from typing import Optional

from services import operator_queue_service as _oq
from services.operator_queue_choices import options_cap_violation, title_cap_violation
from utils.helpers import iso_cutoff, utc_now_iso

from . import deps, filechannel, rows

logger = logging.getLogger("services.operator_queue_service")


# `OperatorQueueSyncService` is composed from this mixin in `__init__`; the
# instance state these methods read through `self.` is initialized there.
class IngestMixin:
    async def _apply_sync_state(self, agent_name: str, row: dict, state: str,
                                detail: Optional[str], now: str) -> bool:
        """Record what the poller established about one row; True iff it changed.

        The row is the edge: `set_sync_state`'s WHERE excludes rows already
        carrying the value and its rowcount is the transition, so two
        overlapping leaders cannot double-record. Audit ONLY the accountability
        transitions — entering a diverged state, or leaving one for confirmed.
        The `confirmed ↔ unconfirmed` flap is never audited: a fleet restart
        must write zero audit rows.
        """
        if state not in rows.SYNC_STATES:
            logger.error(f"Refusing unknown sync_state {state!r} for {row.get('id')} (programming error)")
            return False
        detail = filechannel._detail(detail) if detail else None
        prior = row.get("sync_state")
        if prior == state and (row.get("sync_detail") or "") == (detail or ""):
            return False
        try:
            changed = deps.db.set_operator_queue_sync_state(row["id"], state, detail, now)
        except Exception as e:
            logger.error(f"Failed to record sync state for {row.get('id')}: {e}")
            return False
        if not changed:
            return False
        row["sync_state"] = state
        row["sync_detail"] = detail
        self._changed_this_cycle = True
        if state in rows.DIVERGED_STATES:
            await deps._audit_sync("diverged", agent_name, row["id"],
                                   {"from": prior, "to": state, "detail": detail})
        elif state == rows.SYNC_CONFIRMED and prior in rows.DIVERGED_STATES:
            await deps._audit_sync("reconciled", agent_name, row["id"], {"from": prior})
        return True

    def _notice_file_channel(self, agent_name: str) -> None:
        """trinity-enterprise#611: name the file channel's deprecation once per
        agent per process, on the first ask the poller actually ingests."""
        if agent_name in self._file_channel_noticed:
            return
        if len(self._file_channel_noticed) >= _oq._MAX_QUARANTINE_ENTRIES:
            self._file_channel_noticed.clear()  # safety valve
        self._file_channel_noticed.add(agent_name)
        logger.warning(
            f"Agent '{agent_name}' raised an ask through {_oq.QUEUE_FILE_PATH}. The file "
            f"channel is deprecated: raise asks with the ask_operator MCP tool. The "
            f"file stays supported for two releases (trinity-enterprise#611)."
        )

    async def _sync_agent(self, agent_name: str):
        """Sync a single agent's operator queue file."""
        client = deps.AgentClient(agent_name)
        now = utc_now_iso()

        # 1. Read the queue file from the agent
        try:
            result = await client.read_file(_oq.QUEUE_FILE_PATH, timeout=5.0)
        except Exception as e:
            result = {"success": False, "error": type(e).__name__}

        if not result.get("success"):
            # #2915: "could not read" is a fact, not silence — but with
            # hysteresis. The 5 s read times out intermittently on a busy
            # container, so a row is called `unconfirmed` only after
            # READ_FAILURE_THRESHOLD consecutive failed cycles, and returns to
            # `confirmed` on the first good read. No ingest and no write-back
            # this cycle either way.
            failures = self._read_failures.get(agent_name, 0) + 1
            self._read_failures[agent_name] = failures
            if failures >= filechannel.READ_FAILURE_THRESHOLD:
                try:
                    if deps.db.mark_operator_queue_unconfirmed(
                        filechannel._read_failure_detail(result), now, agent_name=agent_name,
                        exclude_request_id_prefixes=_oq._RESERVED_ID_PREFIXES,
                    ):
                        self._changed_this_cycle = True
                except Exception as e:
                    logger.error(f"Failed to mark {agent_name} rows unconfirmed: {e}")
            return
        self._read_failures.pop(agent_name, None)

        file_exists = result.get("success") and not result.get("not_found")
        content = result.get("content") if file_exists else None
        if file_exists and not content:
            file_exists = False

        # #1632: skip a pathologically large queue file wholesale (C1 per-cycle
        # DoS guard) — don't even parse it. One flood alert, then skip this agent
        # this cycle; response write-back resumes once the file is sane again.
        if file_exists and len(content) > _oq.OPERATOR_QUEUE_MAX_FILE_BYTES:
            logger.warning(
                f"operator-queue.json for {agent_name} is {len(content)} bytes "
                f"(> {_oq.OPERATOR_QUEUE_MAX_FILE_BYTES}); skipping ingestion this cycle"
            )
            await self._maybe_emit_flood_alert(agent_name, reason="oversize_file")
            try:
                if deps.db.mark_operator_queue_unconfirmed(
                    "oversize_file", now, agent_name=agent_name,
                    exclude_request_id_prefixes=_oq._RESERVED_ID_PREFIXES,
                ):
                    self._changed_this_cycle = True
            except Exception as e:
                logger.error(f"Failed to mark {agent_name} rows unconfirmed: {e}")
            return

        if file_exists:
            # #3130: a sane-size read ends an oversize episode.
            self._end_flood_episode(agent_name, "oversize_file")
            try:
                queue_data = json.loads(content)
            except json.JSONDecodeError:
                # #2915: an unparseable file is `unconfirmed`, never "empty", and
                # NOTHING is written back this cycle — the previous path treated
                # it as an empty request list and then overwrote the agent's
                # file with the reconstructed responses alone.
                logger.warning(f"Invalid JSON in operator-queue.json for {agent_name}")
                try:
                    if deps.db.mark_operator_queue_unconfirmed(
                        "invalid_json", now, agent_name=agent_name,
                        exclude_request_id_prefixes=_oq._RESERVED_ID_PREFIXES,
                    ):
                        self._changed_this_cycle = True
                except Exception as e:
                    logger.error(f"Failed to mark {agent_name} rows unconfirmed: {e}")
                return
            if not filechannel._well_formed_queue(queue_data):
                logger.warning(
                    f"operator-queue.json for {agent_name} is not the expected shape "
                    f"(an object with a `requests` list); not reconciling, not writing"
                )
                try:
                    if deps.db.mark_operator_queue_unconfirmed(
                        "wrong_shape", now, agent_name=agent_name,
                        exclude_request_id_prefixes=_oq._RESERVED_ID_PREFIXES,
                    ):
                        self._changed_this_cycle = True
                except Exception as e:
                    logger.error(f"Failed to mark {agent_name} rows unconfirmed: {e}")
                return
        else:
            queue_data = {"$schema": "operator-queue-v1", "requests": []}

        requests = queue_data.get("requests") or []
        content_sha = (
            hashlib.sha256(content.encode("utf-8")).hexdigest() if file_exists else None
        )

        # #2915: everything the platform holds for this agent, in two reads —
        # full rows for the open (pending/responded) items the file is expected
        # to carry, and id/status for every terminal row, so a pending entry that
        # re-uses a terminal row's id is recognised as `stale_id` instead of
        # being re-admitted (the on-conflict create returns the surviving uuid
        # silently: a phantom admit against the depth cap and a "new" broadcast
        # every cycle). Replaces the per-entry `exists()` probe.
        try:
            index = deps.db.get_operator_queue_sync_index_for_agent(agent_name)
        except Exception as e:
            logger.error(f"Failed to read the sync index for {agent_name}: {e}")
            return
        open_rows = list(index["open"] or [])
        terminal_index = index["terminal"] or {}
        native_rids = set(index["foreign"] or ())
        open_by_rid = {r["request_id"]: r for r in open_rows if r.get("request_id")}
        seen_rids: set = set()
        hours = rows.aging_hours()
        receipt_rids: set = set()

        # 2. Process each request. Two independent bounds guard this agent-authored
        #    seam (#1632): a DB-measured pending-DEPTH cap (primary, Redis-independent)
        #    and a per-agent + fleet RATE cap (burst smoothing, fail-open), plus
        #    per-field hygiene inside the create try/except.
        new_items = []
        acknowledged_items = []

        # DEPTH cap baseline — one DB count per cycle; `admitted` tracks this
        # cycle's creates so the cap holds without re-counting per item.
        # #3130: the agent's OWN rows only — a platform row about the agent (its
        # flood alarm above all) never spends the agent's budget.
        pending_count = deps.db.count_operator_queue_pending_for_agent(
            agent_name, exclude_request_id_prefixes=_oq._RESERVED_ID_PREFIXES,
        )
        admitted = 0
        held = 0
        # #3130: why entries were held this cycle — reported to the agent in its
        # file (`platform.ingestion`). The most actionable reason wins.
        hold_reason = None
        # #3243: entries held over the authoring caps. NOT counted in `held`:
        # the flood alert reads "runaway or compromised agent", and a too-long
        # label is an authoring slip that holds until the agent edits it.
        cap_held: dict[str, list] = {filechannel.HOLD_INVALID_OPTIONS: [], filechannel.HOLD_INVALID_TITLE: []}

        for req in requests[:_oq.OPERATOR_QUEUE_MAX_SCAN_PER_CYCLE]:
            if not isinstance(req, dict):
                continue
            req_id = req.get("id")
            if not req_id:
                continue
            if not isinstance(req_id, str):
                # #3313: only a string id can name a row. A list or object id
                # reached the unguarded `in open_by_rid` below and raised out of
                # the whole sync, so none of this agent's answers were written
                # back. A pending one is held as `invalid_id`, as the create-side
                # shape check would hold it; any other status names nothing.
                if req.get("status", "pending") == "pending":
                    logger.warning(
                        f"Rejecting malformed operator-queue id from {agent_name}: {req_id!r}"
                    )
                    held += 1
                    hold_reason = filechannel._stronger_hold(hold_reason, filechannel.HOLD_INVALID_ID)
                continue

            # #1631: reject an agent-authored id that impersonates a platform id
            # prefix (hijack/suppress guard). Log once per (agent, id) so it
            # can't hot-loop the ~5s sync at WARNING. Normalize before the check
            # (case/whitespace-fold) so a lookalike like ` Poison-x` can't slip a
            # platform-styled alert past the filter — the platform mints these
            # prefixes lowercase and unpadded, so a normalized id that matches can
            # only be an impersonation attempt.
            if isinstance(req_id, str) and req_id.strip().lower().startswith(
                _oq._RESERVED_ID_PREFIXES
            ):
                key = (agent_name, req_id)
                # #2372: only a PENDING entry can pre-create (and so suppress)
                # a platform row. A non-pending one is what the pre-ent#499
                # write-back left in the file — the platform's own entry, not
                # the agent's — and nothing removes it, so the row may already
                # be pruned. Skipped quietly; the WARNING stays for the
                # impersonation case.
                if req.get("status", "pending") != "pending":
                    continue
                if key not in self._rejected_reserved:
                    if len(self._rejected_reserved) >= _oq._MAX_QUARANTINE_ENTRIES:
                        self._rejected_reserved.clear()  # safety valve
                    self._rejected_reserved.add(key)
                    logger.warning(
                        f"Rejecting operator-queue request '{req_id}' from "
                        f"'{agent_name}': ids with a reserved platform prefix are "
                        f"minted only by the platform (#1631)"
                    )
                continue

            # trinity-enterprise#611: an id this agent raised over the platform.
            # The native ask lives outside the file contract, so the entry is
            # skipped before any branch reads it: the acknowledged branch matches
            # rows by request_id alone, and the create branch would read the id
            # as brand new every cycle (the phantom admit).
            if isinstance(req_id, str) and req_id in native_rids:
                key = (agent_name, req_id)
                if key not in self._skipped_native:
                    if len(self._skipped_native) >= _oq._MAX_QUARANTINE_ENTRIES:
                        self._skipped_native.clear()  # safety valve
                    self._skipped_native.add(key)
                    logger.warning(
                        f"Ignoring operator-queue file entry '{req_id}' from "
                        f"'{agent_name}': the agent raised that id with ask_operator, "
                        f"and a native ask is never read from or written back to the "
                        f"file (trinity-enterprise#611)"
                    )
                continue

            fail_key = (agent_name, req_id)
            req_status = req.get("status", "pending")
            if isinstance(req_id, str):
                if req_id in seen_rids:
                    # #2989 review: a duplicated id is reconciled ONCE, on its first
                    # entry. Two copies with different content used to flip the row
                    # confirmed ↔ changed every cycle: two audit rows, two writes and
                    # a broadcast per 5 s.
                    continue
                seen_rids.add(req_id)

            if req_status == "acknowledged":
                # Agent acknowledged our response. #1631: broadcast the row's
                # platform uuid (returned here), not the agent's `req_id` — the
                # frontend store keys items by uuid `id`, so an ack keyed on
                # request_id would never match a live item.
                ack_uuid = deps.db.mark_operator_queue_acknowledged(agent_name, req_id)
                if ack_uuid:
                    acknowledged_items.append(ack_uuid)
                    open_by_rid.pop(req_id, None)  # it just went terminal
                elif req_id in open_by_rid:
                    row = open_by_rid[req_id]
                    if row.get("status") == "responded" and filechannel._is_our_answer(req, row):
                        # Our answer landed and the agent acknowledged it, but the
                        # flip waits for the delivery record (`mark_acknowledged`
                        # requires `delivered`, #2989 review) — confirmed, not closed.
                        await self._apply_sync_state(agent_name, row, rows.SYNC_CONFIRMED, None, now)
                    else:
                        # #2915: acknowledged on a row that was never responded — the
                        # agent closed its own ask. The platform used to drop this
                        # (the UPDATE matches only `responded` rows) and keep showing
                        # the human a pending card for an item the agent had closed.
                        await self._apply_sync_state(
                            agent_name, row, rows.SYNC_CLOSED_BY_FILER, "acknowledged", now
                        )
                continue

            if req_id in open_by_rid:
                # #2915: the platform already holds this item — reconcile the
                # entry against the row instead of skipping it. The row is the
                # frozen ingest snapshot; nothing here rewrites it.
                row = open_by_rid[req_id]
                if req_status == "pending":
                    fields = filechannel.changed_fields(row, req)
                    if fields:
                        await self._apply_sync_state(agent_name, row, rows.SYNC_CHANGED, ",".join(fields), now)
                    else:
                        await self._apply_sync_state(agent_name, row, rows.SYNC_CONFIRMED, None, now)
                        if (
                            row.get("status") == "pending"
                            and rows.is_aged(row, hours)
                            and not (isinstance(req.get(filechannel._PLATFORM_BLOCK_KEY), dict)
                                     and req[filechannel._PLATFORM_BLOCK_KEY].get(filechannel._AGING_SINCE_KEY))
                        ):
                            receipt_rids.add(req_id)
                elif req_status == "responded":
                    # Our own write-back landed; the agent has not acknowledged yet.
                    await self._apply_sync_state(agent_name, row, rows.SYNC_CONFIRMED, None, now)
                else:
                    await self._apply_sync_state(
                        agent_name, row, rows.SYNC_CLOSED_BY_FILER, filechannel._fold_agent_status(req_status), now
                    )
                continue

            if isinstance(req_id, str) and req_id in terminal_index:
                term = terminal_index[req_id]
                if req_status == "pending":
                    # #3024: this runs BEFORE the write-back (step 4), so right
                    # after the platform ends a row the file still holds the
                    # ORIGINAL pending entry — the one step 4 is about to flip.
                    # That is not a re-use; only an entry the write-back will not
                    # flip is.
                    if not filechannel.awaits_terminal_flip(term):
                        await self._apply_sync_state(
                            agent_name,
                            {"id": term["id"], "sync_state": term.get("sync_state"), "sync_detail": None},
                            rows.SYNC_STALE_ID, filechannel._fold_agent_status(term.get("status")), now,
                        )
                elif term.get("sync_state") == rows.SYNC_STALE_ID and req_status == term.get("status"):
                    # #3024: the entry reads the row's own ending — file and row
                    # agree. Heals the rows flagged before this fix; a genuine
                    # re-use cannot get here, because the write-back never flips an
                    # entry once the row has left its set.
                    await self._apply_sync_state(
                        agent_name,
                        {"id": term["id"], "sync_state": rows.SYNC_STALE_ID, "sync_detail": None},
                        rows.SYNC_CONFIRMED, None, now,
                    )
                continue

            if req_status != "pending":
                continue

            # #1525: quarantine a request whose create keeps failing so a single
            # malformed/unpersistable entry can't hot-loop the ~5s sync (the row
            # never persists → exists() stays False → retry forever). #1631: keyed
            # by the (agent, id) tuple so agent A's failing id can't quarantine
            # agent B's distinct, healthy request.
            if self._create_failures.get(fail_key, 0) >= _oq.MAX_CREATE_ATTEMPTS:
                continue

            # NB: an agent-authored id impersonating a platform-reserved prefix is
            # already rejected above (normalized, logged once per (agent, id)), so
            # no second reserved-prefix check is needed here.

            # #1632 C4: a create PK can't be safely rewritten, so a malformed /
            # oversize id is rejected (held → may trigger the summary alert).
            if (
                not isinstance(req_id, str)
                or len(req_id) > _oq.OPERATOR_QUEUE_ID_MAX
                or not _oq._ID_RE.match(req_id)
            ):
                logger.warning(
                    f"Rejecting malformed operator-queue id from {agent_name}: {req_id!r}"
                )
                held += 1
                hold_reason = filechannel._stronger_hold(hold_reason, filechannel.HOLD_INVALID_ID)
                continue

            # #3243: the authoring caps, the same predicate as the native raise.
            # A NEW entry only (an ingested row was matched above, so an ask
            # already in the queue is never re-judged), and before the depth and
            # rate caps so a held entry spends no token.
            cap_hold = None
            if options_cap_violation(req.get("options"),
                                     max_options=_oq.OPERATOR_QUEUE_MAX_OPTIONS,
                                     max_chars=_oq.OPERATOR_QUEUE_OPTION_MAX_CHARS):
                cap_hold = filechannel.HOLD_INVALID_OPTIONS
            elif title_cap_violation(req.get("title"),
                                     max_chars=_oq.OPERATOR_QUEUE_ASK_TITLE_MAX_CHARS):
                cap_hold = filechannel.HOLD_INVALID_TITLE
            if cap_hold:
                cap_held[cap_hold].append(req_id)
                hold_reason = filechannel._stronger_hold(hold_reason, cap_hold)
                key = (agent_name, req_id)
                if key not in self._cap_held_logged:
                    if len(self._cap_held_logged) >= _oq._MAX_QUARANTINE_ENTRIES:
                        self._cap_held_logged.clear()  # safety valve
                    self._cap_held_logged.add(key)
                    logger.warning(
                        f"Holding operator-queue request '{req_id}' from '{agent_name}': "
                        f"{cap_hold} (#3243)")
                continue

            # #1632 C3: DEPTH cap (hard, primary). At the cap STOP ingesting —
            # `admitted` only grows, so every later item is over too → break.
            if pending_count + admitted >= _oq.OPERATOR_QUEUE_MAX_PENDING_PER_AGENT:
                held += 1
                hold_reason = filechannel._stronger_hold(hold_reason, filechannel.HOLD_QUEUE_FULL)
                break

            # #1632: RATE cap (per-agent + fleet, fail-open). Charged only at the
            # real create point. Denied → hold + stop scanning (the window is
            # monotonic within a cycle, so a re-check would also deny). NB: on a
            # per-agent-allow / fleet-deny, this cycle's ≤1 per-agent token is
            # "spent" without a create — a fail-safe (slightly stricter) drift
            # bounded to one item because we break.
            if not _oq.rate_limiter.check(
                f"operator_queue_create:{agent_name}",
                _oq.OPERATOR_QUEUE_CREATE_RATE_LIMIT,
                _oq.OPERATOR_QUEUE_CREATE_RATE_WINDOW,
            ).allowed or not _oq.rate_limiter.check(
                "operator_queue_create:_fleet",
                _oq.OPERATOR_QUEUE_FLEET_CREATE_RATE_LIMIT,
                _oq.OPERATOR_QUEUE_CREATE_RATE_WINDOW,
            ).allowed:
                held += 1
                hold_reason = filechannel._stronger_hold(hold_reason, filechannel.HOLD_RATE_LIMITED)
                break

            # New item — clamp then create. The clamp runs INSIDE the try so any
            # clamp/create failure is quarantined by #1525 rather than hot-looping.
            try:
                clamped = filechannel._clamp_ingested_item(req, agent_name)
                # trinity-enterprise#611: provenance from what the POLLER knows,
                # keyword-only — never from the entry, which could claim any
                # channel it likes.
                new_id, inserted = deps.db.create_operator_queue_item_with_outcome(
                    agent_name, clamped, channel="file", raised_by="agent",
                )
                self._create_failures.pop(fail_key, None)  # recovered — clear count
                if not inserted:
                    # The row already exists: an ask raised with the same id
                    # over the platform between this cycle's index read and
                    # now. Nothing was admitted, so nothing spends a depth-cap
                    # slot, announces itself or opens an audit story.
                    continue
                admitted += 1
                new_items.append(clamped)
                self._notice_file_channel(agent_name)
            except Exception as e:
                attempts = self._create_failures.get(fail_key, 0) + 1
                if len(self._create_failures) >= _oq._MAX_QUARANTINE_ENTRIES:
                    self._create_failures.clear()  # safety valve — never unbounded
                self._create_failures[fail_key] = attempts
                if attempts >= _oq.MAX_CREATE_ATTEMPTS:
                    logger.error(
                        f"Quarantining operator-queue request {req_id} for "
                        f"'{agent_name}' after {attempts} failed create attempts "
                        f"(last error: {e}); it won't be retried until it changes "
                        f"or the service restarts"
                    )
                else:
                    logger.error(
                        f"Failed to create queue item {req_id} for '{agent_name}' "
                        f"(attempt {attempts}/{_oq.MAX_CREATE_ATTEMPTS}): {e}"
                    )
                continue

            # #2915: a freshly ingested row is confirmed by construction (the
            # entry was just read) — and `ingested` is the first accountability
            # row of its audit story.
            await self._apply_sync_state(
                agent_name, {"id": new_id, "sync_state": None, "sync_detail": None},
                rows.SYNC_CONFIRMED, None, now,
            )
            # `type` is agent-authored free text — the clamp bounds its length
            # (#3385) but not its content; the
            # audit row is durable operator-visible state (G-04), so it carries
            # the folded token or `other` — never the string itself. `req_id`
            # passed the `_ID_RE` shape check above.
            await deps._audit_sync("ingested", agent_name, new_id,
                                   {"request_id": req_id, "type": filechannel._fold_agent_status(clamped.get("type"))})

        # #2915: open rows the file no longer carries. The entry was pruned or the
        # file is gone — the platform used to keep showing a live card for it.
        for rid, row in open_by_rid.items():
            if rid in seen_rids:
                continue
            # A platform alarm (ent#499) was never in this file — there is no entry
            # to miss (#2989 review: every alarm went `missing` on the first cycle
            # after upgrade). A `responded` row the file lost is the write-back's
            # business: it re-appends the entry this same cycle, so marking it
            # `missing` first only minted a `diverged` + `reconciled` pair.
            if rows.is_platform_minted(row) or row.get("status") == "responded":
                continue
            await self._apply_sync_state(
                agent_name, row, rows.SYNC_MISSING,
                "entry_missing" if file_exists else "file_missing", now,
            )

        # #2915: "last confirmed at HH:MM" without a write per row per cycle —
        # one batched UPDATE per agent, for confirmed rows older than a minute.
        try:
            deps.db.refresh_operator_queue_last_confirmed(
                agent_name, now, iso_cutoff(minutes=filechannel.LAST_CONFIRMED_REFRESH_SECONDS // 60)
            )
        except Exception as e:
            logger.error(f"Failed to refresh last_confirmed_at for {agent_name}: {e}")

        # #1632: one aggregated summary alert per episode when items were held
        # (depth cap, rate cap, or malformed ids) — never one per skipped item.
        if held > 0:
            await self._maybe_emit_flood_alert(agent_name, held=held)
        else:
            # #3130: nothing held — the episode is over; the next one alerts.
            self._end_flood_episode(agent_name, "ingestion_cap")

        # 3. Broadcast new items via WebSocket
        if new_items and _oq._websocket_manager:
            for item in new_items:
                try:
                    await _oq._websocket_manager.broadcast(json.dumps({
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

        if acknowledged_items and _oq._websocket_manager:
            for ack_id in acknowledged_items:
                try:
                    await _oq._websocket_manager.broadcast(json.dumps({
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
        # them — as in-place status flips on entries still in the file; an
        # entry that is gone is recorded `undelivered:entry_missing` (#2915),
        # never silently skipped. The aging receipt rides the same write.
        responded_items = deps.db.get_operator_queue_responded_for_agent(agent_name)
        terminal_items = deps.db.get_operator_queue_terminal_for_agent(agent_name)
        # #3130: tell a held file so — but only when the marker it carries is not
        # already the one wanted, and never again against a file a write was
        # already refused on.
        ingestion = filechannel._ingestion_marker(hold_reason, cap_held[filechannel.HOLD_INVALID_OPTIONS],
                                                  cap_held[filechannel.HOLD_INVALID_TITLE])
        marker_due = (
            file_exists
            and filechannel._marker_differs(queue_data, ingestion)
            and self._ingestion_write_refused.get(agent_name) != content_sha
        )
        if responded_items or terminal_items or receipt_rids or marker_due:
            wrote = await self._write_responses_to_agent(
                agent_name, client, queue_data, responded_items,
                terminal_items, file_exists,
                content_sha=content_sha, receipt_request_ids=receipt_rids,
                ingestion=ingestion if marker_due else filechannel._MARKER_UNCHANGED,
            )
            if marker_due and wrote is False:
                self._ingestion_write_refused[agent_name] = content_sha
            elif wrote:
                self._ingestion_write_refused.pop(agent_name, None)
