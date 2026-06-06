import asyncio
from types import SimpleNamespace

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.child_session import GatewayChildSessionRequest
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource, build_session_key


class FakeEntry:
    session_id = "sess-child"


class FakeSessionStore:
    def __init__(self):
        self.sources = []

    def get_or_create_session(self, source):
        self.sources.append(source)
        return FakeEntry()


class FakeAdapter:
    def __init__(self):
        self.created = []
        self.events = []

    async def create_handoff_thread(self, parent_chat_id, name):
        self.created.append((parent_chat_id, name))
        return "thread-999"

    async def handle_message(self, event):
        self.events.append(event)


def make_runner(*, authorized=True):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.DISCORD: PlatformConfig(enabled=True, extra={})}
    )
    runner.session_store = FakeSessionStore()
    runner.adapters = {Platform.DISCORD: FakeAdapter()}
    runner._child_session_results = {}
    runner._child_session_lock = asyncio.Lock()
    runner._is_user_authorized = lambda source: authorized
    return runner


def parent_event(*, chat_type="group", chat_id="chan-123", parent_chat_id=None, thread_id=None):
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id=chat_id,
        chat_name="general",
        chat_type=chat_type,
        user_id="user-1",
        user_name="Rafa",
        thread_id=thread_id,
        guild_id="guild-1",
        parent_chat_id=parent_chat_id,
        message_id="msg-1",
    )
    return MessageEvent(
        text="start child",
        message_type=MessageType.TEXT,
        source=source,
        message_id="msg-1",
    )


@pytest.mark.asyncio
async def test_start_child_session_rechecks_authorization_before_thread_creation():
    runner = make_runner(authorized=False)
    req = GatewayChildSessionRequest(
        parent_event=parent_event(),
        child_title="Child task",
        starter_prompt="Do the task",
    )

    with pytest.raises(PermissionError):
        await runner.start_child_session(req)

    assert runner.adapters[Platform.DISCORD].created == []
    assert runner.adapters[Platform.DISCORD].events == []


@pytest.mark.asyncio
async def test_start_child_session_creates_discord_thread_under_parent_channel_from_thread():
    runner = make_runner()
    req = GatewayChildSessionRequest(
        parent_event=parent_event(
            chat_type="thread",
            chat_id="thread-parent-invocation",
            parent_chat_id="chan-parent",
            thread_id="thread-parent-invocation",
        ),
        child_title="Child from existing thread",
        starter_prompt="Begin child run",
        metadata={"plugin": "test-harness"},
    )

    result = await runner.start_child_session(req)

    adapter = runner.adapters[Platform.DISCORD]
    assert adapter.created == [("chan-parent", "Child from existing thread")]
    assert len(adapter.events) == 1
    child_event = adapter.events[0]
    assert child_event.text == "Begin child run"
    assert child_event.internal is False
    assert child_event.source.chat_type == "thread"
    assert child_event.source.chat_id == "thread-999"
    assert child_event.source.thread_id == "thread-999"
    assert child_event.source.parent_chat_id == "chan-parent"
    assert child_event.source.user_id == "user-1"

    assert result.platform == "discord"
    assert result.parent_channel_id == "chan-parent"
    assert result.child_channel_id == "thread-999"
    assert result.thread_name == "Child from existing thread"
    assert result.session_id == "sess-child"
    assert result.scheduled_started is True
    assert result.idempotent_replay is False
    assert result.metadata["plugin"] == "test-harness"
    assert result.metadata["parent_message_id"] == "msg-1"
    assert result.session_key == build_session_key(
        child_event.source,
        group_sessions_per_user=True,
        thread_sessions_per_user=False,
    )


@pytest.mark.asyncio
async def test_start_child_session_idempotency_replays_without_duplicate_dispatch():
    runner = make_runner()
    req = GatewayChildSessionRequest(
        parent_event=parent_event(),
        child_title="Idempotent child",
        starter_prompt="Run once",
        idempotency_key="attempt-1",
    )

    first = await runner.start_child_session(req)
    second = await runner.start_child_session(req)

    adapter = runner.adapters[Platform.DISCORD]
    assert adapter.created == [("chan-123", "Idempotent child")]
    assert len(adapter.events) == 1
    assert first.scheduled_started is True
    assert first.idempotent_replay is False
    assert second.child_channel_id == first.child_channel_id
    assert second.session_key == first.session_key
    assert second.scheduled_started is False
    assert second.idempotent_replay is True


@pytest.mark.asyncio
async def test_public_seam_can_be_called_from_minimal_plugin_harness():
    runner = make_runner()

    async def plugin_hook(event, gateway):
        return await gateway.start_child_session(
            GatewayChildSessionRequest(
                parent_event=event,
                child_title="Plugin child",
                starter_prompt="Started by plugin",
                idempotency_key="plugin-attempt",
                metadata={"audit": "ok"},
            )
        )

    result = await plugin_hook(parent_event(), runner)

    assert result.child_channel_id == "thread-999"
    assert result.metadata["audit"] == "ok"
    assert runner.adapters[Platform.DISCORD].events[0].text == "Started by plugin"
