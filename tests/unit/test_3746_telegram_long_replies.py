"""Regression #3746: ordered Telegram replies without dangling file references."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from adapters import message_router as mr
from adapters import telegram_adapter as tg
from adapters.base import ChannelResponse, NormalizedMessage


@pytest.fixture
def wire(monkeypatch):
    calls = []
    outcomes = []
    client_type = httpx.AsyncClient

    def handle(request):
        method = request.url.path.rsplit('/', 1)[-1]
        payload = json.loads(request.content) if method == 'sendMessage' else request.content
        calls.append((method, payload))
        if outcomes:
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            status, body = outcome
            return httpx.Response(status, json=body)
        return httpx.Response(200, json={'ok': True, 'result': {'message_id': len(calls)}})

    monkeypatch.setattr(tg.httpx, 'AsyncClient', lambda **kw: client_type(
        transport=httpx.MockTransport(handle), **kw))
    return calls, outcomes


def response(text):
    return ChannelResponse(text=text, metadata={'bot_token': 'test-token'})


def delivered(calls):
    return ''.join(tg.TelegramAdapter._strip_html(p['text'])
                   for method, p in calls if method == 'sendMessage')


@pytest.mark.asyncio
@pytest.mark.parametrize('size', [99, 100, 101, 4095, 4096, 4097, 6000])
async def test_router_retains_telegram_code_block(wire, monkeypatch, size):
    calls, _ = wire
    monkeypatch.setattr(mr, 'db', SimpleNamespace(add_public_chat_message=lambda *a, **k: None))
    monkeypatch.setattr(mr, '_agent_avatar_url', lambda name: None)
    router = mr.ChannelMessageRouter()
    monkeypatch.setattr(router, '_resolve_indicator', AsyncMock())
    adapter = tg.TelegramAdapter()
    text = '```txt\n' + 'x' * size + '\n```'
    msg = NormalizedMessage(sender_id='1', channel_id='2', text='hello', timestamp='0')
    await router._finalize_response(adapter, msg, 'example-agent', 'test-token',
        'telegram', False, None, None, 'session', None,
        SimpleNamespace(cost=0, execution_id='execution'), text)
    assert delivered(calls) == 'x' * size + '\n'
    if size > 4096:
        assert len(calls) > 1
    assert all('see attached' not in p['text'] for _, p in calls)


@pytest.mark.asyncio
@pytest.mark.parametrize('channel', ['slack', 'whatsapp'])
async def test_router_other_channels_still_receive_extracted_files(monkeypatch, channel):
    monkeypatch.setattr(mr, 'db', SimpleNamespace(add_public_chat_message=lambda *a, **k: None))
    monkeypatch.setattr(mr, '_agent_avatar_url', lambda name: None)
    router = mr.ChannelMessageRouter()
    monkeypatch.setattr(router, '_resolve_indicator', AsyncMock())
    adapter = SimpleNamespace(send_response=AsyncMock(), on_response_sent=AsyncMock())
    msg = NormalizedMessage(sender_id='1', channel_id='2', text='hello', timestamp='0')
    await router._finalize_response(adapter, msg, 'example-agent', 'test-token',
        channel, False, None, None, 'session', None,
        SimpleNamespace(cost=0, execution_id='execution'), '```txt\n' + 'x' * 100 + '\n```')
    reply = adapter.send_response.call_args.args[1]
    assert reply.text == '(see attached: response_1.txt)'
    assert reply.files[0].content == b'x' * 100 + b'\n'


@pytest.mark.asyncio
@pytest.mark.parametrize('size', [4095, 4096, 4097, 8192, 12000])
@pytest.mark.parametrize('char', ['x', '😀', '界', 'e\u0301'])
async def test_boundary_and_unicode_preserve_content(wire, size, char):
    calls, _ = wire
    text = char * size
    await tg.TelegramAdapter().send_response('2', response(text))
    assert delivered(calls) == text
    assert all(len(p['text'].encode('utf-16-le')) // 2 <= 4096 for _, p in calls)


@pytest.mark.asyncio
@pytest.mark.parametrize('text', [
    'x' * 3995 + '&' + 'y' * 500,
    '```python\n' + 'print("<&😀>")\n' * 800 + '```',
    '**' + 'bold ' * 1800 + '**',
    '# Heading\n' + '[link](https://example.com) & `x < y`\n' * 500,
], ids=['entity-at-cut', 'code', 'bold', 'links-and-inline-code'])
async def test_markdown_chunks_preserve_entities_and_balanced_tags(wire, text):
    calls, _ = wire
    adapter = tg.TelegramAdapter()
    await adapter.send_response('2', response(text))
    assert delivered(calls) == adapter._strip_html(adapter._markdown_to_html(text))
    for _, payload in calls:
        stack = []
        for match in adapter._HTML_TAG_RE.finditer(payload['text']):
            if match.group(1):
                assert stack.pop() == match.group(2)
            else:
                stack.append(match.group(2))
        assert not stack
        assert len(payload['text'].encode('utf-16-le')) // 2 <= 4096


@pytest.mark.asyncio
async def test_rate_limit_retries_current_chunk_before_next(wire, monkeypatch):
    calls, outcomes = wire
    outcomes.extend([(429, {'parameters': {'retry_after': 0}}),
                     (200, {'ok': True, 'result': {'message_id': 1}})])
    await tg.TelegramAdapter().send_response('2', response('a' * 4096 + 'b' * 4096))
    assert calls[0] == calls[1]
    assert delivered(calls[1:]) == 'a' * 4096 + 'b' * 4096


@pytest.mark.asyncio
async def test_exhausted_retry_stops_before_later_chunks(wire):
    calls, outcomes = wire
    outcomes.extend([(429, {'parameters': {'retry_after': 0}})] * 2)
    await tg.TelegramAdapter().send_response('2', response('a' * 10000))
    assert len(calls) == 2
    assert calls[0] == calls[1]


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', [
    (403, {'ok': False}),
    (200, {'ok': False}),
    (200, {'ok': True, 'result': {}}),
    httpx.ReadTimeout('uncertain delivery'),
])
async def test_failure_after_first_chunk_never_replays_prefix_or_sends_suffix(wire, failure):
    calls, outcomes = wire
    outcomes.extend([(200, {'ok': True, 'result': {'message_id': 1}}), failure])
    await tg.TelegramAdapter().send_response('2', response('a' * 14000))
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_html_fallback_can_retry_rate_limit_without_replaying(wire, monkeypatch):
    calls, outcomes = wire
    sleep = AsyncMock()
    monkeypatch.setattr(tg.asyncio, 'sleep', sleep)
    outcomes.extend([
        (400, {'description': "Bad Request: can't parse entities"}),
        (429, {'parameters': {'retry_after': 30}}),
        (200, {'ok': True, 'result': {'message_id': 1}}),
    ])
    await tg.TelegramAdapter().send_response('2', response('**' + 'a' * 9000 + '**'))
    assert calls[0][1]['parse_mode'] == 'HTML'
    assert calls[1][1]['parse_mode'] == ''
    assert calls[1] == calls[2]
    assert delivered(calls[2:]) == 'a' * 9000
    sleep.assert_awaited_once_with(30)


def test_large_nested_markup_falls_back_without_looping():
    text = '<b>' * 1500 + 'value' * 2000 + '</b>' * 1500
    chunks = tg.TelegramAdapter._split_message(text)
    assert ''.join(tg.TelegramAdapter._strip_html(c) for c in chunks) == 'value' * 2000
    assert all(len(c.encode('utf-16-le')) // 2 <= 4096 for c in chunks)


@pytest.mark.asyncio
@pytest.mark.parametrize("retry_after", [0, 30, 31, 45, 86400])
async def test_flood_wait_is_bounded_without_early_retry(wire, monkeypatch, retry_after):
    calls, outcomes = wire
    sleep = AsyncMock()
    monkeypatch.setattr(tg.asyncio, "sleep", sleep)
    outcomes.append((429, {"parameters": {"retry_after": retry_after}}))
    result = await tg.TelegramAdapter()._send_message("test-token", "2", "hello")
    if retry_after <= 30:
        sleep.assert_awaited_once_with(retry_after)
        assert len(calls) == 2 and result
    else:
        sleep.assert_not_awaited()
        assert len(calls) == 1 and result is None


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [(403, {"ok": False}), httpx.ReadTimeout("uncertain")])
async def test_proactive_failure_stops_before_suffix(wire, monkeypatch, failure):
    from unittest.mock import MagicMock
    from services import proactive_message_service as pm
    calls, outcomes = wire
    database = MagicMock()
    database.get_telegram_binding.return_value = {"id": "synthetic-binding", "bot_id": "123"}
    database.get_telegram_chat_link_by_verified_email.return_value = {"telegram_user_id": "321"}
    database.get_telegram_bot_token.return_value = "test-token"
    monkeypatch.setattr(pm, "db", database)
    outcomes.extend([(200, {"ok": True, "result": {"message_id": 1}}), failure])
    result = await pm.ProactiveMessageService()._deliver_telegram("test-agent", "user@example.com", "a" * 10000)
    assert len(calls) == 2
    assert result.success is False
    assert result.message_id is None


@pytest.mark.asyncio
async def test_failure_log_keeps_reason_without_token_or_url(wire, caplog):
    _, outcomes = wire
    outcomes.append((403, {"ok": False, "description":
        "Forbidden: bot blocked test-token https://api.telegram.org/bottest-token/sendMessage"}))
    assert await tg.TelegramAdapter()._send_message("test-token", "2", "hello") is None
    assert "bot blocked" in caplog.text
    assert "test-token" not in caplog.text
    assert "https://" not in caplog.text


@pytest.mark.asyncio
async def test_proactive_auto_falls_back_after_partial_telegram_failure(wire, monkeypatch):
    """#3746: stopping Telegram chunks preserves the existing auto fallback policy."""
    from unittest.mock import MagicMock
    from services import proactive_message_service as pm
    calls, outcomes = wire
    database = MagicMock()
    database.get_telegram_binding.return_value = {"id": "synthetic-binding", "bot_id": "123"}
    database.get_telegram_chat_link_by_verified_email.return_value = {"telegram_user_id": "321"}
    database.get_telegram_bot_token.return_value = "test-token"
    monkeypatch.setattr(pm, "db", database)
    service = pm.ProactiveMessageService()
    monkeypatch.setattr(service, "_check_rate_limit", lambda *a: True)
    monkeypatch.setattr(service, "_increment_rate_limit", MagicMock())
    monkeypatch.setattr(service, "_persist_outbound", MagicMock())
    monkeypatch.setattr(service, "_audit_send", AsyncMock())
    slack = AsyncMock(return_value=pm.DeliveryResult(success=True, channel="slack", message_id="s1"))
    monkeypatch.setattr(service, "_deliver_slack", slack)
    outcomes.extend([(200, {"ok": True, "result": {"message_id": 1}}), (403, {"ok": False})])
    text = "a" * 10000
    result = await service._rate_limit_and_deliver("test-agent", "user@example.com", text, "auto", False, "preview")
    assert len(calls) == 2
    assert result.success and result.channel == "slack"
    slack.assert_awaited_once_with("test-agent", "user@example.com", text)
