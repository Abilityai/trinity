"""Channel upload ownership across dispatch, delivery and waiter failures."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from adapters import message_router as router_module
import importlib


@pytest.fixture
def flow(monkeypatch):
    task_execution_service = importlib.import_module("services.task_execution_service")
    router = router_module.ChannelMessageRouter()
    message = SimpleNamespace(sender_id='sender', channel_id='channel', thread_id=None,
        text='question', files=[object()], metadata={'_group_unrecorded': True})
    adapter = SimpleNamespace(channel_type='telegram', enrich_message=AsyncMock(),
        handle_verification=AsyncMock(return_value=True), record_inbound_activity=AsyncMock(),
        get_session_identifier=lambda m: 'session', get_source_identifier=lambda m: 'sender',
        indicate_processing=AsyncMock(), send_response=AsyncMock(), on_response_sent=AsyncMock())
    for name, value in [('_resolve_agent_and_token', ('agent', 'token')),
                        ('_enforce_rate_limits', True), ('_ensure_agent_available', object()),
                        ('_enforce_access_policy', (True, None))]:
        monkeypatch.setattr(router, name, AsyncMock(return_value=value))
    monkeypatch.setattr(router, '_maybe_transcribe_voice', AsyncMock(return_value=message))
    monkeypatch.setattr(router, '_handle_file_uploads', AsyncMock(return_value=(['image'], '/home/developer/uploads/dispatch', False, [])))
    monkeypatch.setattr(router, '_resolve_indicator', AsyncMock())
    monkeypatch.setattr(router, '_arm_progress_driver', Mock())
    monkeypatch.setattr(router, '_cancel_progress_driver', AsyncMock())
    cleanup = AsyncMock()
    monkeypatch.setattr(router, '_cleanup_uploads', cleanup)
    db = Mock()
    db.get_or_create_public_chat_session.return_value = SimpleNamespace(id='session')
    db.build_public_chat_context.return_value = 'question'
    monkeypatch.setattr(router_module, 'db', db)
    monkeypatch.setattr(router_module, 'build_voice_capability_prompt', lambda *a: '')
    monkeypatch.setattr(router_module, 'build_public_channel_caller_prompt', lambda *a: '')
    monkeypatch.setattr(router_module, '_agent_avatar_url', lambda *a: None)
    monkeypatch.setattr(router_module, '_format_channel_identity', lambda *a: '')
    result = SimpleNamespace(status='success', response='answer', error=None, error_code=None, cost=0, execution_id='execution')
    dispatch = AsyncMock(return_value=result)
    monkeypatch.setattr(task_execution_service, 'dispatch_and_await_terminal', dispatch)
    monkeypatch.setattr(task_execution_service, 'result_from_execution_row', lambda _: result)
    return router, message, adapter, cleanup, result, dispatch


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['success', 'send', 'hook', 'cancel', 'task_failed', 'task_cancelled', 'failed_reply', 'no_reply'])
async def test_completed_dispatch_cleans_exact_directory_even_when_delivery_fails(flow, failure):
    router, message, adapter, cleanup, result, dispatch = flow
    if failure == 'no_reply':
        result.response = '[NO_REPLY]'
    if failure == 'task_cancelled':
        result.status, result.error = 'cancelled', 'synthetic cancellation'
    if failure in ('task_failed', 'failed_reply'):
        result.status, result.error = 'failed', 'synthetic failure'
    if failure in ('send', 'failed_reply'):
        adapter.send_response.side_effect = RuntimeError('synthetic delivery failure')
    if failure == 'hook':
        adapter.on_response_sent.side_effect = RuntimeError('synthetic hook failure')
    if failure == 'cancel':
        adapter.send_response.side_effect = asyncio.CancelledError()
    try:
        await router._handle_message_inner(adapter, message)
    except (RuntimeError, asyncio.CancelledError):
        pass
    cleanup.assert_awaited_once()
    assert cleanup.await_args.args[1] == '/home/developer/uploads/dispatch'


@pytest.mark.asyncio
@pytest.mark.parametrize('actual', ['running', 'queued', None, 'unreadable', 'success'])
async def test_waiter_timeout_does_not_delete_active_or_unknown_execution_files(flow, monkeypatch, actual):
    router, message, adapter, cleanup, result, dispatch = flow
    result.status, result.error_code = 'failed', 'timeout'
    def read(_):
        if actual == 'unreadable':
            raise RuntimeError('synthetic DB outage')
        return SimpleNamespace(status=actual) if actual else None
    monkeypatch.setattr(importlib.import_module('services.task_execution_service'), 'result_from_execution_row', read)
    await router._handle_message_inner(adapter, message)
    assert cleanup.await_count == (1 if actual == 'success' else 0)


@pytest.mark.asyncio
async def test_cancelled_dispatch_retains_files_when_execution_may_survive(flow):
    router, message, adapter, cleanup, result, dispatch = flow
    dispatch.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await router._handle_message_inner(adapter, message)
    cleanup.assert_not_awaited()


@pytest.mark.asyncio
async def test_predispatch_indicator_cancellation_cleans_completed_upload(flow):
    router, message, adapter, cleanup, result, dispatch = flow
    adapter.indicate_processing.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await router._handle_message_inner(adapter, message)
    dispatch.assert_not_awaited()
    cleanup.assert_awaited_once()


@pytest.mark.asyncio
async def test_interrupted_upload_settles_write_before_removing_exact_directory(monkeypatch):
    from services import upload_service
    entered, release = asyncio.Event(), asyncio.Event()
    events = []
    async def execute(container, command, **kwargs):
        events.append(command)
        return 0, b''
    async def archive(container, directory, data):
        events.append('write-start:' + directory)
        entered.set()
        await release.wait()
        events.append('write-finished:' + directory)
        return True
    monkeypatch.setattr(upload_service, '_MAGIC_AVAILABLE', False)
    monkeypatch.setattr(upload_service, 'container_exec_run', execute)
    monkeypatch.setattr(upload_service, 'container_put_archive', archive)
    monkeypatch.setattr(upload_service.platform_audit_service, 'log', AsyncMock())
    task = asyncio.create_task(upload_service.process_file_uploads(
        [{'name': 'photo.png', 'mimetype': 'image/png', 'data': b'synthetic'}],
        'agent', object(), 'session', 'user'))
    await entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    assert not any(command.startswith('rm ') for command in events)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    directory = events[1].split(':', 1)[1]
    assert events[-2:] == ['write-finished:' + directory, 'rm -rf ' + directory]


@pytest.mark.asyncio
async def test_terminal_cleanup_survives_repeated_cancellation_and_propagates_it(flow, monkeypatch):
    router, message, adapter, cleanup, result, dispatch = flow
    entered, release = asyncio.Event(), asyncio.Event()
    async def delayed_cleanup(*args):
        entered.set()
        await release.wait()
    cleanup.side_effect = delayed_cleanup
    request = asyncio.create_task(router._handle_message_inner(adapter, message))
    await entered.wait()
    request.cancel()
    await asyncio.sleep(0)
    request.cancel()
    await asyncio.sleep(0)
    assert not request.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await request
    cleanup.assert_awaited_once()


@pytest.mark.asyncio
async def test_upload_failure_after_directory_creation_cleans_and_preserves_error(monkeypatch):
    from services import upload_service
    calls = []
    async def execute(container, command, **kwargs):
        calls.append(command)
        return 0, b''
    monkeypatch.setattr(upload_service, '_MAGIC_AVAILABLE', False)
    monkeypatch.setattr(upload_service, 'container_exec_run', execute)
    monkeypatch.setattr(upload_service, 'container_put_archive', AsyncMock(return_value=True))
    failure = RuntimeError('synthetic next-file failure')
    class FailedAttachment(dict):
        def get(self, *args):
            raise failure
    monkeypatch.setattr(upload_service.platform_audit_service, 'log', AsyncMock())
    with pytest.raises(RuntimeError) as error:
        await upload_service.process_file_uploads(
            [{'name': 'photo.png', 'mimetype': 'image/png', 'data': b'synthetic'}, FailedAttachment()],
            'agent', object(), 'session', 'user')
    assert error.value is failure
    assert calls[-1].startswith('rm -rf /home/developer/uploads/')


@pytest.mark.asyncio
async def test_live_dispatch_keeps_uploads_until_terminal(flow):
    router, message, adapter, cleanup, result, dispatch = flow
    entered, release = asyncio.Event(), asyncio.Event()
    async def active(**kwargs):
        entered.set()
        await release.wait()
        return result
    dispatch.side_effect = active
    request = asyncio.create_task(router._handle_message_inner(adapter, message))
    await entered.wait()
    cleanup.assert_not_awaited()
    release.set()
    await request
    cleanup.assert_awaited_once()


@pytest.mark.asyncio
async def test_router_cleanup_nonzero_is_reported_without_raw_output(monkeypatch, caplog):
    monkeypatch.setattr(router_module, 'container_exec_run', AsyncMock(return_value=(1, b'synthetic-private-output')))
    await router_module.ChannelMessageRouter._cleanup_uploads(object(), '/home/developer/uploads/example')
    assert 'nonzero exit status' in caplog.text
    assert 'synthetic-private-output' not in caplog.text


@pytest.mark.asyncio
async def test_preparation_cleanup_nonzero_preserves_original_failure(monkeypatch, caplog):
    from services import upload_service
    failure = RuntimeError('synthetic producer failure')
    monkeypatch.setattr(upload_service, '_write_file_uploads', AsyncMock(side_effect=failure))
    monkeypatch.setattr(upload_service, 'container_exec_run', AsyncMock(return_value=(1, b'synthetic-private-output')))
    with pytest.raises(RuntimeError) as error:
        await upload_service.process_file_uploads([], 'agent', object(), 'session', 'user')
    assert error.value is failure
    assert 'nonzero exit status' in caplog.text
    assert 'synthetic-private-output' not in caplog.text
