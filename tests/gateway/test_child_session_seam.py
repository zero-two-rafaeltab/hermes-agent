import asyncio

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
    def __init__(
        self,
        *,
        thread_id: str | None = "thread-999",
        allowed=True,
        create_delay: float = 0,
        announce: bool = False,
    ):
        self.created = []
        self.events = []
        self.thread_id = thread_id
        self.allowed = allowed
        self.create_delay = create_delay
        self.marked_threads = []
        self.announce = announce
        self.announcements = []
        self.child_backlinks = []
        if not announce:
            self.__dict__["announce_child_session"] = None

    def is_child_session_parent_allowed(self, source):
        return self.allowed

    async def create_handoff_thread(self, parent_chat_id, name):
        if self.create_delay:
            await asyncio.sleep(self.create_delay)
        self.created.append((parent_chat_id, name))
        return self.thread_id

    async def create_child_session_thread(self, parent_chat_id, name):
        thread_id = await self.create_handoff_thread(parent_chat_id, name)
        if thread_id:
            self.marked_threads.append(str(thread_id))
        return thread_id

    async def announce_child_session(self, *, parent_source, parent_channel_id, thread_name, metadata=None):
        if self.create_delay:
            await asyncio.sleep(self.create_delay)
        self.created.append((parent_channel_id, thread_name))
        if not self.thread_id:
            return None
        announcement_channel_id = (
            parent_source.chat_id if parent_source.chat_type == "thread" else parent_channel_id
        )
        message_id = f"ann-{len(self.announcements) + 1}"
        announcement = {
            "child_channel_id": self.thread_id,
            "thread_id": self.thread_id,
            "announcement_channel_id": announcement_channel_id,
            "announcement_message_id": message_id,
            "announcement_url": (
                f"https://discord.com/channels/{parent_source.guild_id}/"
                f"{announcement_channel_id}/{message_id}"
            ),
            "text": f"Started child session {thread_name}: <#{self.thread_id}>",
            "anchored_to_announcement": parent_source.chat_type != "thread",
        }
        self.announcements.append(announcement)
        self.child_backlinks.append(parent_source.chat_id)
        self.marked_threads.append(str(self.thread_id))
        return announcement

    async def handle_message(self, event):
        self.events.append(event)


def make_runner(*, authorized=True, adapter=None):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.DISCORD: PlatformConfig(enabled=True, extra={})}
    )
    runner.session_store = FakeSessionStore()
    runner.adapters = {Platform.DISCORD: adapter or FakeAdapter()}
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


def test_discord_child_session_parent_policy_reuses_channel_gates():
    from plugins.platforms.discord.adapter import DiscordAdapter

    adapter = object.__new__(DiscordAdapter)
    adapter.config = PlatformConfig(
        enabled=True,
        extra={"allowed_channels": ["allowed-parent", "ignored"], "ignored_channels": ["ignored"]},
    )

    assert adapter.is_child_session_parent_allowed(
        parent_event(chat_id="thread", parent_chat_id="allowed-parent", thread_id="thread").source
    )
    assert not adapter.is_child_session_parent_allowed(parent_event(chat_id="other").source)
    assert not adapter.is_child_session_parent_allowed(parent_event(chat_id="ignored").source)


@pytest.mark.asyncio
async def test_discord_create_child_session_thread_marks_participation():
    from plugins.platforms.discord.adapter import DiscordAdapter

    class Threads:
        def __init__(self):
            self.marked = []

        def mark(self, thread_id):
            self.marked.append(thread_id)

    adapter = object.__new__(DiscordAdapter)
    adapter._threads = Threads()

    async def fake_create(parent_chat_id, name):
        return "child-thread-1"

    adapter.create_handoff_thread = fake_create

    assert await adapter.create_child_session_thread("parent", "Child") == "child-thread-1"
    assert adapter._threads.marked == ["child-thread-1"]


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
    assert result.dispatched is True
    assert result.idempotent_replay is False
    assert adapter.marked_threads == ["thread-999"]
    assert result.metadata["plugin"] == "test-harness"
    assert result.metadata["parent_message_id"] == "msg-1"
    assert result.session_key == build_session_key(
        child_event.source,
        group_sessions_per_user=True,
        thread_sessions_per_user=False,
    )


@pytest.mark.asyncio
async def test_start_child_session_announces_normal_channel_child_with_metadata():
    adapter = FakeAdapter(announce=True)
    runner = make_runner(adapter=adapter)
    req = GatewayChildSessionRequest(
        parent_event=parent_event(),
        child_title="Issue child",
        starter_prompt="Begin child run",
        idempotency_key="issue-15-normal",
        metadata={"issue_number": 15, "attempt": 2, "plugin": "tests"},
    )

    first = await runner.start_child_session(req)
    replay = await runner.start_child_session(req)

    assert adapter.created == [("chan-123", "Issue child")]
    assert len(adapter.announcements) == 1
    announcement = adapter.announcements[0]
    assert announcement["announcement_channel_id"] == "chan-123"
    assert announcement["anchored_to_announcement"] is True
    assert "<#thread-999>" in announcement["text"]
    assert adapter.child_backlinks == ["chan-123"]
    assert first.announcement_channel_id == "chan-123"
    assert first.announcement_message_id == "ann-1"
    assert first.announcement_url == "https://discord.com/channels/guild-1/chan-123/ann-1"
    assert first.metadata["announcement_channel_id"] == "chan-123"
    assert first.metadata["announcement_message_id"] == "ann-1"
    assert first.metadata["announcement_url"] == first.announcement_url
    assert replay.idempotent_replay is True
    assert replay.announcement_message_id == "ann-1"
    assert len(adapter.events) == 1


@pytest.mark.asyncio
async def test_start_child_session_announces_existing_thread_sibling_with_backlink():
    adapter = FakeAdapter(announce=True)
    runner = make_runner(adapter=adapter)
    req = GatewayChildSessionRequest(
        parent_event=parent_event(
            chat_type="thread",
            chat_id="control-thread",
            parent_chat_id="chan-parent",
            thread_id="control-thread",
        ),
        child_title="Sibling child",
        starter_prompt="Begin sibling child",
        metadata={"plugin": "tests"},
    )

    result = await runner.start_child_session(req)

    assert adapter.created == [("chan-parent", "Sibling child")]
    assert len(adapter.announcements) == 1
    announcement = adapter.announcements[0]
    assert announcement["announcement_channel_id"] == "control-thread"
    assert announcement["anchored_to_announcement"] is False
    assert "<#thread-999>" in announcement["text"]
    assert adapter.child_backlinks == ["control-thread"]
    assert result.parent_channel_id == "chan-parent"
    assert result.child_channel_id == "thread-999"
    assert result.announcement_channel_id == "control-thread"
    assert result.announcement_message_id == "ann-1"
    assert result.announcement_url == "https://discord.com/channels/guild-1/control-thread/ann-1"
    assert result.metadata["announcement_url"] == result.announcement_url


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
    assert first.dispatched is True
    assert first.idempotent_replay is False
    assert second.child_channel_id == first.child_channel_id
    assert second.session_key == first.session_key
    assert second.dispatched is False
    assert second.idempotent_replay is True


@pytest.mark.asyncio
async def test_start_child_session_concurrent_idempotency_only_dispatches_once():
    adapter = FakeAdapter(create_delay=0.05)
    runner = make_runner(adapter=adapter)
    req = GatewayChildSessionRequest(
        parent_event=parent_event(),
        child_title="Concurrent child",
        starter_prompt="Run once despite concurrency",
        idempotency_key="attempt-concurrent",
    )

    first, second = await asyncio.gather(
        runner.start_child_session(req),
        runner.start_child_session(req),
    )

    assert adapter.created == [("chan-123", "Concurrent child")]
    assert len(adapter.events) == 1
    assert adapter.marked_threads == ["thread-999"]
    assert {first.dispatched, second.dispatched} == {True, False}
    assert {first.idempotent_replay, second.idempotent_replay} == {False, True}
    assert first.child_channel_id == second.child_channel_id == "thread-999"


@pytest.mark.asyncio
async def test_start_child_session_discord_thread_failure_does_not_dispatch_to_parent():
    adapter = FakeAdapter(thread_id=None)
    runner = make_runner(adapter=adapter)
    req = GatewayChildSessionRequest(
        parent_event=parent_event(),
        child_title="No thread",
        starter_prompt="Do not leak to parent",
    )

    with pytest.raises(RuntimeError, match="Discord child session thread"):
        await runner.start_child_session(req)

    assert adapter.created == [("chan-123", "No thread")]
    assert adapter.events == []


@pytest.mark.asyncio
async def test_start_child_session_enforces_adapter_channel_policy_before_create():
    adapter = FakeAdapter(allowed=False)
    runner = make_runner(adapter=adapter)
    req = GatewayChildSessionRequest(
        parent_event=parent_event(),
        child_title="Blocked child",
        starter_prompt="Do not dispatch",
    )

    with pytest.raises(PermissionError, match="adapter channel policy"):
        await runner.start_child_session(req)

    assert adapter.created == []
    assert adapter.events == []


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
