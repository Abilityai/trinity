"""
Telegram albums arrive as ONE agent turn (abilityai/trinity-enterprise#934).

Telegram sends an album (≤10 photos / documents / videos sent together) as one
``message`` update per item, all sharing a ``media_group_id``, with the caption
on one item only. Before this, each item became its own agent turn.

Production runs ``uvicorn --workers 2`` (docker-compose.prod.yml and
docker-compose.hosted.yml), and the items of one album are separate webhook
POSTs that can land on either worker — so the buffer lives in Redis, and the
two-worker cases below drive two coalescers against ONE fake Redis server.

Layers exercised:
- ``adapters.transports.inbound_coalescer.InboundCoalescer`` (Redis buffer),
- ``adapters.telegram_adapter.merge_album_updates`` + ``parse_message``,
- ``TelegramWebhookTransport._process_update`` / ``handle_webhook`` (dedup).

Related flow: docs/memory/feature-flows/telegram-integration.md → Albums.
"""
from __future__ import annotations

import asyncio
import json
import logging
import types
from unittest.mock import AsyncMock

import fakeredis
import pytest

import adapters.telegram_adapter as ta
from adapters.telegram_adapter import TelegramAdapter, merge_album_updates
from adapters.transports import inbound_coalescer as ic
from adapters.transports.inbound_coalescer import InboundCoalescer

WINDOW = 0.05  # seconds — tests use a tiny window and real sleeps


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

def _server():
    return fakeredis.FakeServer()


def _redis(server):
    return fakeredis.aioredis.FakeRedis(server=server, decode_responses=True)


def _coalescer(server, on_flush, **kw):
    kw.setdefault("window_s", WINDOW)
    kw.setdefault("max_items", 10)
    return InboundCoalescer(prefix="test:album", on_flush=on_flush, redis=_redis(server), **kw)


class _Sink:
    """Records every flush: a list of (items, recovered)."""

    def __init__(self):
        self.calls = []

    async def __call__(self, items, recovered=False):
        self.calls.append((items, recovered))


async def _settle(n=4):
    await asyncio.sleep(WINDOW * n)


def _photo(mid, *, group="g1", caption=None, chat=-100, file_id=None, entities=None,
           reply_to=None, topic=None, from_bot=False):
    message = {
        "message_id": mid,
        "date": 1,
        "from": {"id": 9, "is_bot": from_bot, "username": "alice", "first_name": "Alice"},
        "chat": {"id": chat, "type": "supergroup" if chat < 0 else "private", "title": "Builders"},
        "photo": [
            {"file_id": f"small-{mid}", "file_size": 10},
            {"file_id": file_id or f"large-{mid}", "file_size": 1000},
        ],
    }
    if group:
        message["media_group_id"] = group
    if caption is not None:
        message["caption"] = caption
    if entities is not None:
        message["caption_entities"] = entities
    if reply_to is not None:
        message["reply_to_message"] = reply_to
    if topic is not None:
        message["is_topic_message"] = True
        message["message_thread_id"] = topic
    return {"update_id": 1000 + mid, "message": message, "_bot_id": "77",
            "_bot_username": "bot", "_agent_name": "analytics"}


def _document(mid, name, *, group="g1"):
    u = _photo(mid, group=group)
    del u["message"]["photo"]
    u["message"]["document"] = {"file_id": f"doc-{mid}", "file_name": name,
                                "mime_type": "application/pdf", "file_size": 5}
    return u


def _video(mid, *, group="g1"):
    u = _photo(mid, group=group)
    del u["message"]["photo"]
    u["message"]["video"] = {"file_id": f"vid-{mid}", "file_size": 50}
    return u


def _adapter_db(monkeypatch, trigger_mode=None):
    monkeypatch.setattr(ta, "db", types.SimpleNamespace(
        get_telegram_binding=lambda name: {"id": 1, "agent_name": name},
        get_telegram_group_config=lambda b, c: ({"trigger_mode": trigger_mode} if trigger_mode else None),
    ))


# --------------------------------------------------------------------------- #
# The Redis buffer
# --------------------------------------------------------------------------- #

class TestCoalescer:
    @pytest.mark.asyncio
    async def test_three_items_one_group_flush_once(self):
        sink = _Sink()
        c = _coalescer(_server(), sink)
        for mid in (1, 2, 3):
            assert await c.add("1", "-100:g1", _photo(mid)) is True
        await _settle()
        assert len(sink.calls) == 1
        items, recovered = sink.calls[0]
        assert [i["message"]["message_id"] for i in items] == [1, 2, 3]
        assert recovered is False

    @pytest.mark.asyncio
    async def test_window_rearms_on_each_item(self):
        sink = _Sink()
        c = _coalescer(_server(), sink)
        await c.add("1", "-100:g1", _photo(1))
        await asyncio.sleep(WINDOW * 0.6)
        await c.add("1", "-100:g1", _photo(2))
        await asyncio.sleep(WINDOW * 0.6)  # past item 1's window, inside item 2's
        assert sink.calls == []
        await _settle()
        assert len(sink.calls) == 1 and len(sink.calls[0][0]) == 2

    @pytest.mark.asyncio
    async def test_two_workers_one_album_one_flush(self):
        """Items split across two workers (two coalescers, one Redis) → one turn."""
        server, sink = _server(), _Sink()
        worker_a, worker_b = _coalescer(server, sink), _coalescer(server, sink)
        await worker_a.add("1", "-100:g1", _photo(1))
        await worker_b.add("1", "-100:g1", _photo(2))
        await worker_a.add("1", "-100:g1", _photo(3))
        await worker_b.add("1", "-100:g1", _photo(4))
        await _settle()
        assert len(sink.calls) == 1
        assert sorted(i["message"]["message_id"] for i in sink.calls[0][0]) == [1, 2, 3, 4]

    @pytest.mark.asyncio
    async def test_interleaved_albums_stay_separate(self):
        sink = _Sink()
        c = _coalescer(_server(), sink)
        await c.add("1", "-100:g1", _photo(1, group="g1"))
        await c.add("1", "-100:g2", _photo(2, group="g2"))
        await c.add("1", "-100:g1", _photo(3, group="g1"))
        await c.add("1", "-100:g2", _photo(4, group="g2"))
        await _settle()
        groups = sorted(sorted(i["message"]["message_id"] for i in items) for items, _ in sink.calls)
        assert groups == [[1, 3], [2, 4]]

    @pytest.mark.asyncio
    async def test_same_group_id_in_two_chats_or_bots_never_merges(self):
        sink = _Sink()
        c = _coalescer(_server(), sink)
        await c.add("1", "-100:g1", _photo(1, chat=-100))
        await c.add("1", "-200:g1", _photo(2, chat=-200))
        await c.add("2", "-100:g1", _photo(3, chat=-100))  # another bot binding
        await _settle()
        assert len(sink.calls) == 3

    @pytest.mark.asyncio
    async def test_cap_flushes_at_once(self):
        sink = _Sink()
        c = _coalescer(_server(), sink, window_s=60)  # the window alone would never fire
        for mid in range(1, 11):
            await c.add("1", "-100:g1", _photo(mid))
        await asyncio.sleep(0.05)
        assert len(sink.calls) == 1 and len(sink.calls[0][0]) == 10
        await c.stop()

    @pytest.mark.asyncio
    async def test_late_item_after_flush_is_handed_back(self, caplog):
        sink = _Sink()
        c = _coalescer(_server(), sink)
        await c.add("1", "-100:g1", _photo(1))
        await _settle()
        with caplog.at_level(logging.INFO):
            assert await c.add("1", "-100:g1", _photo(2)) is False
        assert "g1" in caplog.text and "after" in caplog.text.lower()

    @pytest.mark.asyncio
    async def test_redis_down_hands_the_item_back(self):
        class Broken:
            def __getattr__(self, name):
                raise ConnectionError("redis down")

        c = InboundCoalescer(prefix="t", on_flush=_Sink(), redis=Broken(), window_s=WINDOW, max_items=10)
        assert await c.add("1", "-100:g1", _photo(1)) is False

    @pytest.mark.asyncio
    async def test_redis_down_mid_album_flushes_what_was_buffered(self, monkeypatch):
        """Items 1–2 buffered, then Redis fails on item 3: 1–2 must still reach the
        agent (forced flush), item 3 is handed back to the caller."""
        sink = _Sink()
        c = _coalescer(_server(), sink, window_s=60)
        await c.add("1", "-100:g1", _photo(1))
        await c.add("1", "-100:g1", _photo(2))
        real = c._buffer

        async def failing_buffer(*a, **kw):
            raise ConnectionError("blip")

        monkeypatch.setattr(c, "_buffer", failing_buffer)
        assert await c.add("1", "-100:g1", _photo(3)) is False
        await asyncio.sleep(0.05)
        assert len(sink.calls) == 1
        assert [i["message"]["message_id"] for i in sink.calls[0][0]] == [1, 2]
        monkeypatch.setattr(c, "_buffer", real)
        await c.stop()

    @pytest.mark.asyncio
    async def test_oversized_item_is_handed_back(self):
        c = _coalescer(_server(), _Sink(), max_item_bytes=100)
        assert await c.add("1", "-100:g1", _photo(1, caption="x" * 500)) is False

    @pytest.mark.asyncio
    async def test_open_album_cap_per_scope(self):
        c = _coalescer(_server(), _Sink(), window_s=60, max_open_per_scope=2)
        assert await c.add("1", "-100:g1", _photo(1, group="g1")) is True
        assert await c.add("1", "-100:g2", _photo(2, group="g2")) is True
        assert await c.add("1", "-100:g3", _photo(3, group="g3")) is False
        # an already-open album still accepts items, and another scope is unaffected
        assert await c.add("1", "-100:g1", _photo(4, group="g1")) is True
        assert await c.add("2", "-100:g9", _photo(5, group="g9")) is True
        await c.stop()

    @pytest.mark.asyncio
    async def test_stop_cancels_timers_and_leaves_the_album_for_the_sweep(self):
        server, sink = _server(), _Sink()
        c = _coalescer(server, sink, window_s=60)
        await c.add("1", "-100:g1", _photo(1))
        await c.stop()
        assert sink.calls == []
        assert await _redis(server).llen("test:album:1:-100:g1:items") == 1


class TestOrphanSweep:
    """A restart inside the window leaves items in Redis with no timer alive."""

    @pytest.mark.asyncio
    async def test_recent_orphan_is_delivered(self, caplog):
        server, sink = _server(), _Sink()
        now = [1_000_000]
        dead = _coalescer(server, _Sink(), window_s=60, clock_ms=lambda: now[0])
        await dead.add("1", "-100:g1", _photo(1))
        await dead.add("1", "-100:g1", _photo(2))
        await dead.stop()  # the worker died; its timer is gone
        now[0] += 15_000   # past the orphan threshold, inside the recovery window
        booted = _coalescer(server, sink, clock_ms=lambda: now[0])
        with caplog.at_level(logging.WARNING):
            assert await booted.sweep_orphans() == 1
        await asyncio.sleep(0.02)
        assert len(sink.calls) == 1
        items, recovered = sink.calls[0]
        assert len(items) == 2 and recovered is True
        assert "g1" in caplog.text

    @pytest.mark.asyncio
    async def test_stale_orphan_is_dropped_with_a_warning(self, caplog):
        server, sink = _server(), _Sink()
        now = [1_000_000]
        dead = _coalescer(server, _Sink(), window_s=60, clock_ms=lambda: now[0])
        for mid in (1, 2, 3):
            await dead.add("1", "-100:g1", _photo(mid))
        await dead.stop()
        now[0] += 5 * 60_000
        booted = _coalescer(server, sink, clock_ms=lambda: now[0])
        with caplog.at_level(logging.WARNING):
            await booted.sweep_orphans()
        assert sink.calls == []
        assert "g1" in caplog.text and "3" in caplog.text
        assert await _redis(server).llen("test:album:1:-100:g1:items") == 0

    @pytest.mark.asyncio
    async def test_live_album_is_not_stolen(self):
        """An album whose timer is still pending on the other worker is left alone."""
        server, sink = _server(), _Sink()
        live = _coalescer(server, sink)
        await live.add("1", "-100:g1", _photo(1))
        thief = _coalescer(server, _Sink())
        assert await thief.sweep_orphans() == 0
        await _settle()
        assert len(sink.calls) == 1


# --------------------------------------------------------------------------- #
# Merging and parsing (adapter)
# --------------------------------------------------------------------------- #

class TestMergeAndParse:
    def test_merge_orders_by_message_id_and_keeps_the_caption(self):
        merged = merge_album_updates([_photo(3), _photo(1), _photo(2, caption="pick the best one")])
        msg = merged["message"]
        assert msg["message_id"] == 1
        assert [m["message_id"] for m in msg["_album"]] == [1, 2, 3]
        assert msg["caption"] == "pick the best one"
        assert merged["_agent_name"] == "analytics"

    def test_merge_carries_reply_and_topic_from_any_item(self):
        reply = {"from": {"id": 77, "is_bot": True}, "text": "hi"}
        merged = merge_album_updates([_photo(1), _photo(2, reply_to=reply, topic=5)])
        assert merged["message"]["reply_to_message"] == reply
        assert merged["message"]["message_thread_id"] == 5

    def test_one_message_with_all_files_in_order(self, monkeypatch):
        _adapter_db(monkeypatch, trigger_mode="all")
        merged = merge_album_updates([_photo(2), _photo(1), _photo(3, caption="compare these")])
        msg = TelegramAdapter().parse_message(merged)
        assert [f.id for f in msg.files] == ["large-1", "large-2", "large-3"]
        assert "[User sent an album of 3 photos: compare these]" in msg.text
        assert msg.metadata["album_size"] == 3

    def test_mixed_album_names_every_kind(self, monkeypatch):
        _adapter_db(monkeypatch, trigger_mode="all")
        merged = merge_album_updates([
            _photo(1, caption="the docs"), _document(2, "spec.pdf"), _video(3)])
        msg = TelegramAdapter().parse_message(merged)
        assert [f.name for f in msg.files] == ["photo.jpg", "spec.pdf"]
        assert "album of 3 items (1 photo, 1 document, 1 video): the docs" in msg.text
        assert "spec.pdf" in msg.text
        assert "video" in msg.text and "not readable" in msg.text

    def test_album_without_caption_still_has_text(self, monkeypatch):
        _adapter_db(monkeypatch, trigger_mode="all")
        msg = TelegramAdapter().parse_message(merge_album_updates([_photo(1), _photo(2)]))
        assert msg.text == "[User sent an album of 2 photos]"

    def test_single_photo_unchanged(self, monkeypatch):
        _adapter_db(monkeypatch, trigger_mode="all")
        msg = TelegramAdapter().parse_message(_photo(1, group=None, caption="look"))
        assert [f.id for f in msg.files] == ["large-1"]
        assert msg.text == "[User sent a photo: look]"
        assert "album_size" not in msg.metadata


class TestCaptionMention:
    """A tag in a caption is a tag (choice A on ent#934): before this, `@bot` in a
    photo caption was never seen, so a mention-mode group observed it silently."""

    _TAG = [{"type": "mention", "offset": 0, "length": 4}]

    def test_tagged_single_photo_triggers(self, monkeypatch):
        _adapter_db(monkeypatch)  # mention mode
        msg = TelegramAdapter().parse_message(
            _photo(1, group=None, caption="@bot what is this?", entities=self._TAG))
        assert msg.metadata["observe_only"] is False
        assert "@bot" not in msg.text
        assert "what is this?" in msg.text

    def test_tagged_caption_on_a_later_item_triggers_the_whole_album(self, monkeypatch):
        _adapter_db(monkeypatch)
        merged = merge_album_updates([
            _photo(1), _photo(2, caption="@bot which is best?", entities=self._TAG), _photo(3)])
        msg = TelegramAdapter().parse_message(merged)
        assert msg.metadata["observe_only"] is False
        assert len(msg.files) == 3

    def test_untagged_album_is_observed_once_as_a_unit(self, monkeypatch):
        _adapter_db(monkeypatch)
        msg = TelegramAdapter().parse_message(merge_album_updates([_photo(1), _photo(2)]))
        assert msg.metadata["observe_only"] is True
        assert msg.text == "[User sent an album of 2 photos]"

    def test_reply_to_bot_on_any_item_triggers(self, monkeypatch):
        _adapter_db(monkeypatch)
        reply = {"from": {"id": 77, "is_bot": True}, "text": "send me the shots"}
        msg = TelegramAdapter().parse_message(
            merge_album_updates([_photo(1), _photo(2, reply_to=reply)]))
        assert msg.metadata["observe_only"] is False


# --------------------------------------------------------------------------- #
# Transport
# --------------------------------------------------------------------------- #

def _transport(monkeypatch, server=None):
    from adapters.transports import telegram_webhook as tw
    transport = tw.TelegramWebhookTransport(TelegramAdapter(), router=None)
    transport.on_event = AsyncMock()
    if server is not None:
        transport._albums = InboundCoalescer(
            prefix="test:album", window_s=WINDOW, max_items=10,
            on_flush=transport._flush_album, redis=_redis(server))
    return tw, transport


class TestTransport:
    @pytest.mark.asyncio
    async def test_album_reaches_the_pipeline_once(self, monkeypatch):
        _, t = _transport(monkeypatch, _server())
        binding = {"id": 1, "agent_name": "analytics"}
        for mid in (1, 2, 3):
            await t._process_update(_photo(mid, caption="hi" if mid == 2 else None), binding)
        t.on_event.assert_not_awaited()
        await _settle()
        t.on_event.assert_awaited_once()
        merged = t.on_event.await_args.args[0]
        assert len(merged["message"]["_album"]) == 3
        assert merged["message"]["caption"] == "hi"

    @pytest.mark.asyncio
    async def test_lone_photo_goes_straight_through(self, monkeypatch):
        server = _server()
        _, t = _transport(monkeypatch, server)
        await t._process_update(_photo(1, group=None), {"id": 1, "agent_name": "analytics"})
        t.on_event.assert_awaited_once()
        assert await _redis(server).keys("*") == []

    @pytest.mark.asyncio
    async def test_items_from_a_bot_are_not_buffered(self, monkeypatch):
        server = _server()
        _, t = _transport(monkeypatch, server)
        await t._process_update(_photo(1, from_bot=True), {"id": 1, "agent_name": "analytics"})
        t.on_event.assert_awaited_once()
        assert await _redis(server).keys("*") == []

    @pytest.mark.asyncio
    async def test_no_buffer_means_todays_behaviour(self, monkeypatch):
        _, t = _transport(monkeypatch)  # Redis unavailable at start → no coalescer
        t._albums = None
        for mid in (1, 2):
            await t._process_update(_photo(mid), {"id": 1, "agent_name": "analytics"})
        assert t.on_event.await_count == 2


class _Request:
    def __init__(self, update, token="tok"):
        self.headers = {"X-Telegram-Bot-Api-Secret-Token": token}
        self._body = json.dumps(update).encode()

    async def body(self):
        return self._body


class _BrokenRedis:
    """A client whose every command fails, as during a Redis outage."""

    async def set(self, *a, **kw):
        raise ConnectionError("redis down")


class TestDedup:
    """Telegram delivers over up to 40 parallel connections and its docs say
    updates can arrive out of order. A `<= last_update_id` watermark read and
    written in two steps drops a real update that loses the race — an album
    item, possibly the captioned one."""

    def _setup(self, monkeypatch, server, last=0):
        tw, t = _transport(monkeypatch, server)
        writes = []
        binding = {"id": 1, "agent_name": "analytics", "telegram_secret_token": "tok",
                   "last_update_id": last, "bot_id": "77", "bot_username": "bot"}
        monkeypatch.setattr(tw, "db", types.SimpleNamespace(
            get_telegram_binding_by_webhook_secret=lambda s: dict(binding),
            update_telegram_last_update_id=lambda a, u: writes.append(u)))
        t._process_update = AsyncMock()
        t._dedup_redis = _redis(server) if server is not None else _BrokenRedis()
        return t, writes

    @pytest.mark.asyncio
    async def test_out_of_order_updates_are_both_processed(self, monkeypatch):
        # The binding row already says 102 when 101 arrives — the old watermark
        # would have dropped 101.
        t, _ = self._setup(monkeypatch, _server(), last=102)
        await t.handle_webhook(_Request({"update_id": 101, "message": {}}), "s")
        await asyncio.sleep(0)
        assert t._process_update.await_count == 1

    @pytest.mark.asyncio
    async def test_a_repeated_update_is_processed_once(self, monkeypatch):
        t, _ = self._setup(monkeypatch, _server())
        for _ in range(2):
            await t.handle_webhook(_Request({"update_id": 7, "message": {}}), "s")
        await asyncio.sleep(0)
        assert t._process_update.await_count == 1

    @pytest.mark.asyncio
    async def test_without_redis_the_watermark_still_guards(self, monkeypatch):
        t, _ = self._setup(monkeypatch, None, last=7)
        await t.handle_webhook(_Request({"update_id": 7, "message": {}}), "s")
        await t.handle_webhook(_Request({"update_id": 8, "message": {}}), "s")
        await asyncio.sleep(0)
        assert t._process_update.await_count == 1


class TestWatermarkWrite:
    """The fallback watermark only moves forward: with parallel delivery, a
    slower request for update 101 must not rewind a row that already says 102."""

    def test_last_update_id_never_moves_backwards(self, tmp_path, monkeypatch):
        from sqlalchemy import create_engine, insert, select

        import db.telegram_channels as tc
        from db import tables as t

        engine = create_engine(f"sqlite:///{tmp_path / 't.db'}")
        t.metadata.create_all(engine, tables=[t.telegram_bindings])
        monkeypatch.setattr(tc, "get_engine", lambda: engine)
        with engine.begin() as conn:
            conn.execute(insert(t.telegram_bindings).values(
                agent_name="analytics", bot_token_encrypted="x", bot_id="77",
                webhook_secret="s", last_update_id=0,
                created_at="2026-10-21T00:00:00Z", updated_at="2026-10-21T00:00:00Z"))
        ops = tc.TelegramChannelOperations()

        def current():
            with engine.connect() as conn:
                return conn.execute(select(t.telegram_bindings.c.last_update_id)).scalar()

        ops.update_last_update_id("analytics", 102)
        ops.update_last_update_id("analytics", 101)
        assert current() == 102
        ops.update_last_update_id("analytics", 103)
        assert current() == 103


class TestTransportLifecycle:
    @pytest.mark.asyncio
    async def test_start_delivers_an_album_a_restart_left_in_redis(self, monkeypatch):
        server = _server()
        now = [1_000_000]
        dead = _coalescer(server, _Sink(), window_s=60, clock_ms=lambda: now[0])
        await dead.add("1", "-100:g1", _photo(1, caption="before the restart"))
        await dead.add("1", "-100:g1", _photo(2))
        await dead.stop()
        now[0] += 15_000

        _, t = _transport(monkeypatch)
        t._albums = InboundCoalescer(prefix="test:album", window_s=WINDOW, max_items=10,
                                     on_flush=t._flush_album, redis=_redis(server),
                                     clock_ms=lambda: now[0])
        await t.start()
        await asyncio.sleep(0.05)
        t.on_event.assert_awaited_once()
        merged = t.on_event.await_args.args[0]
        assert len(merged["message"]["_album"]) == 2
        assert merged["message"]["caption"] == "before the restart"

    @pytest.mark.asyncio
    async def test_stop_leaves_a_pending_album_for_the_next_start(self, monkeypatch):
        server = _server()
        _, t = _transport(monkeypatch, server)
        t._albums._window_s = 60
        await t._process_update(_photo(1), {"id": 1, "agent_name": "analytics"})
        await t.stop()
        t.on_event.assert_not_awaited()
        assert await _redis(server).llen("test:album:1:-100:g1:items") == 1
