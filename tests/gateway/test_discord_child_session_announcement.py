from types import SimpleNamespace
from typing import cast

import pytest

from gateway.config import Platform


class FakeThreads:
    def __init__(self):
        self.marked = []

    def mark(self, thread_id):
        self.marked.append(thread_id)


class FakeClient:
    def __init__(self, channels):
        self.channels = {int(channel_id): channel for channel_id, channel in channels.items()}

    def get_channel(self, channel_id):
        return self.channels.get(int(channel_id))

    async def fetch_channel(self, channel_id):
        return self.channels.get(int(channel_id))


class FakeThread:
    def __init__(self, thread_id="300"):
        self.id = thread_id
        self.sent = []
        self.fail_send = False

    async def send(self, content, **kwargs):
        if getattr(self, "fail_send", False):
            raise RuntimeError("send failed")
        message = FakeMessage(f"msg-{len(self.sent) + 1}", self.id, self)
        self.sent.append((content, kwargs, message))
        return message


class FakeMessage:
    def __init__(self, message_id, channel_id, channel, *, fail_edit=False):
        self.id = message_id
        self.channel_id = channel_id
        self.channel = channel
        self.jump_url = f"https://discord.test/{channel_id}/{message_id}"
        self.fail_edit = fail_edit
        self.edits = []

    async def create_thread(self, **kwargs):
        self.create_thread_kwargs = kwargs
        thread = getattr(self.channel, "next_thread", None) or FakeThread("300")
        self.created_thread = thread
        return thread

    async def edit(self, **kwargs):
        if self.fail_edit:
            raise RuntimeError("edit failed")
        self.edits.append(kwargs)
        return self


class FakeTextChannel:
    def __init__(self, channel_id="100", *, fail_seed_edit=False):
        self.id = channel_id
        self.sent = []
        self.created = []
        self.next_thread = FakeThread("300")
        self.fail_seed_edit = fail_seed_edit

    async def send(self, content, **kwargs):
        if getattr(self, "fail_send", False):
            raise RuntimeError("send failed")
        message = FakeMessage(
            f"msg-{len(self.sent) + 1}",
            self.id,
            self,
            fail_edit=self.fail_seed_edit and not self.sent,
        )
        self.sent.append((content, kwargs, message))
        return message

    async def create_thread(self, **kwargs):
        self.created.append(kwargs)
        return self.next_thread


def make_adapter(channels):
    from plugins.platforms.discord.adapter import DiscordAdapter

    adapter = object.__new__(DiscordAdapter)
    adapter._client = FakeClient(channels)
    adapter._threads = FakeThreads()  # type: ignore[assignment]
    adapter.platform = Platform.DISCORD
    return adapter


def source(*, chat_type="group", chat_id="100", thread_id=None, guild_id="guild-1"):
    return SimpleNamespace(
        chat_type=chat_type,
        chat_id=chat_id,
        thread_id=thread_id,
        guild_id=guild_id,
    )


@pytest.mark.asyncio
async def test_announce_child_session_normal_text_channel_edits_seed_with_child_link():
    parent = FakeTextChannel("100")
    adapter = make_adapter({"100": parent})

    result = await adapter.announce_child_session(
        parent_source=source(chat_id="100"),
        parent_channel_id="100",
        thread_name="Child issue",
        metadata={"plugin": "tests"},
    )
    assert result is not None

    seed_content, seed_kwargs, seed = parent.sent[0]
    assert "Starting child session" in seed_content
    assert "allowed_mentions" in seed_kwargs
    assert seed.create_thread_kwargs["name"] == "Child issue"
    assert seed.edits[0]["content"].endswith("<#300>")
    assert "allowed_mentions" in seed.edits[0]
    assert result["child_channel_id"] == "300"
    assert result["announcement_channel_id"] == "100"
    assert result["announcement_message_id"] == "msg-1"
    assert result["announcement_url"] == "https://discord.test/100/msg-1"
    assert parent.next_thread.sent[0][0] == "↩️ Parent/control context: <#100>"
    assert "allowed_mentions" in parent.next_thread.sent[0][1]
    assert cast(FakeThreads, adapter._threads).marked == ["300"]


@pytest.mark.asyncio
async def test_announce_child_session_existing_thread_creates_sibling_and_posts_link():
    parent = FakeTextChannel("100")
    invoking = FakeThread("200")
    adapter = make_adapter({"100": parent, "200": invoking})

    result = await adapter.announce_child_session(
        parent_source=source(chat_type="thread", chat_id="200", thread_id="200"),
        parent_channel_id="100",
        thread_name="Sibling child",
        metadata={"plugin": "tests"},
    )
    assert result is not None

    assert parent.created[0]["name"] == "Sibling child"
    assert invoking.sent[0][0].endswith("<#300>")
    assert "allowed_mentions" in invoking.sent[0][1]
    assert result["child_channel_id"] == "300"
    assert result["announcement_channel_id"] == "200"
    assert result["announcement_message_id"] == "msg-1"
    assert result["announcement_url"] == "https://discord.test/200/msg-1"


@pytest.mark.asyncio
async def test_announce_child_session_existing_thread_requires_sendable_invoking_thread():
    parent = FakeTextChannel("100")
    adapter = make_adapter({"100": parent})

    with pytest.raises(RuntimeError, match="invoking thread"):
        await adapter.announce_child_session(
            parent_source=source(chat_type="thread", chat_id="200", thread_id="200"),
            parent_channel_id="100",
            thread_name="Sibling child",
        )

    invoking = FakeThread("200")
    invoking.fail_send = True
    adapter = make_adapter({"100": parent, "200": invoking})
    with pytest.raises(RuntimeError, match="invoking thread"):
        await adapter.announce_child_session(
            parent_source=source(chat_type="thread", chat_id="200", thread_id="200"),
            parent_channel_id="100",
            thread_name="Sibling child",
        )


@pytest.mark.asyncio
async def test_announce_child_session_seed_edit_failure_sends_fallback_link_metadata():
    parent = FakeTextChannel("100", fail_seed_edit=True)
    adapter = make_adapter({"100": parent})

    result = await adapter.announce_child_session(
        parent_source=source(chat_id="100"),
        parent_channel_id="100",
        thread_name="Fallback child",
        metadata={"issue_number": 15},
    )
    assert result is not None

    assert len(parent.sent) == 2
    fallback_content, fallback_kwargs, fallback = parent.sent[1]
    assert fallback_content.endswith("<#300>")
    assert "allowed_mentions" in fallback_kwargs
    assert result["child_channel_id"] == "300"
    assert result["announcement_channel_id"] == "100"
    assert result["announcement_message_id"] == fallback.id == "msg-2"
    assert result["announcement_url"] == "https://discord.test/100/msg-2"
