import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from mautrix.types import EventType, MessageType
from ruamel.yaml import YAML

from maubot_llm.backends import BackendError, ChatCompletion
from maubot_llm.bot import LlmBot, _validate_id_list
from maubot_llm.db import Room


class Content(dict):
    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError as err:
            raise AttributeError(key) from err


def make_bot():
    bot = object.__new__(LlmBot)
    bot.config = {
        "allowlist": ["@alice:example.org"],
        "admin_users": ["@admin:example.org"],
    }
    bot.client = SimpleNamespace(mxid="@llm:example.org")
    return bot


def test_allowlist_and_plugin_metadata():
    bot = make_bot()
    assert bot.is_allowed("@alice:example.org")
    assert bot.is_allowed("@admin:example.org")
    assert not bot.is_allowed("@stranger:example.org")
    bot.config["allowlist"] = False
    assert bot.is_allowed("@stranger:example.org")

    root = Path(__file__).parents[1]
    yaml = YAML(typ="safe")
    metadata = yaml.load((root / "maubot.yaml").read_text())
    config = yaml.load((root / "base-config.yaml").read_text())
    assert metadata["maubot"] == "0.6.0"
    assert metadata["main_class"] == "LlmBot"
    assert metadata["database_type"] == "asyncpg"
    assert config["default_backend"] in config["backends"]
    assert config["allowlist"] == []
    assert LlmBot.get_config_class().__name__ == "Config"


@pytest.mark.parametrize(
    ("name", "value", "allow_false", "valid"),
    [
        ("allowlist", [], True, True),
        ("allowlist", ["@alice:example.org"], True, True),
        ("allowlist", False, True, True),
        ("allowlist", None, True, False),
        ("allowlist", [None], True, False),
        ("admin_users", False, False, False),
        ("ignored_senders", ["@bot:example.org"], False, True),
    ],
)
def test_validate_id_list(name, value, allow_false, valid):
    if valid:
        _validate_id_list(name, value, allow_false=allow_false)
    else:
        with pytest.raises(ValueError, match=name):
            _validate_id_list(name, value, allow_false=allow_false)


def test_mentions_edits_and_thread_scopes():
    bot = make_bot()
    mentioned = SimpleNamespace(
        content=Content(
            {
                "body": "Hello bot",
                "m.mentions": {"user_ids": ["@llm:example.org"]},
            }
        )
    )
    assert bot._is_mentioned(mentioned)
    not_mentioned = SimpleNamespace(content=Content({"body": "Hello"}))
    assert not bot._is_mentioned(not_mentioned)

    thread_event = SimpleNamespace(
        content=Content(
            {
                "body": "reply",
                "m.relates_to": {"rel_type": "m.thread", "event_id": "$root"},
            }
        )
    )
    assert bot._thread_scope(thread_event) == "thread:$root"
    assert not bot._is_edit(thread_event)
    edited = SimpleNamespace(
        content=Content(
            {"body": "replacement", "m.relates_to": {"rel_type": "m.replace"}}
        )
    )
    assert bot._is_edit(edited)


@pytest.mark.asyncio
async def test_dm_context_and_room_admin_permissions():
    bot = make_bot()
    event = SimpleNamespace(
        room_id="!room:example.org",
        sender="@alice:example.org",
        content=Content({"body": "message"}),
    )
    assert await bot._context_scope(event, is_dm=True) == "dm:@alice:example.org"
    assert await bot._context_scope(event, is_dm=False) == ""
    thread_event = SimpleNamespace(
        room_id=event.room_id,
        sender=event.sender,
        content=Content(
            {
                "body": "thread message",
                "m.relates_to": {"rel_type": "m.thread", "event_id": "$root"},
            }
        ),
    )
    assert await bot._context_scope(thread_event, is_dm=True) == (
        "dm:@alice:example.org:thread:$root"
    )

    bot.client.get_state_event = AsyncMock(
        return_value={"users": {"@alice:example.org": 50}, "users_default": 0}
    )
    assert await bot._can_manage_room(event)
    bot.client.get_state_event.assert_awaited_once_with(
        event.room_id, EventType.ROOM_POWER_LEVELS
    )
    bot.client.get_state_event = AsyncMock(
        return_value={"users": {}, "users_default": 0}
    )
    assert not await bot._can_manage_room(event)


@pytest.mark.asyncio
async def test_message_filters_and_duplicate_event():
    bot = make_bot()
    bot.config.update(
        ignored_senders=[],
        group_mode="mention",
        direct_rooms=[],
    )
    bot.log = Mock()
    bot._tasks = set()
    bot.database = object()

    def message(sender, content):
        return SimpleNamespace(
            sender=sender,
            room_id="!room:example.org",
            event_id="$event",
            content=content,
            mark_read=AsyncMock(),
        )

    own_message = message(
        bot.client.mxid, Content({"body": "loop", "msgtype": MessageType.TEXT})
    )
    edited_message = message(
        "@alice:example.org",
        Content(
            {
                "body": "edited",
                "msgtype": MessageType.TEXT,
                "m.relates_to": {"rel_type": "m.replace"},
            }
        ),
    )
    with patch("maubot_llm.bot.db.claim_event", new_callable=AsyncMock) as claim:
        await bot.handle_msg(own_message)
        await bot.handle_msg(edited_message)
        claim.assert_not_awaited()

    bot._is_direct_message = AsyncMock(return_value=False)
    bot.get_room = AsyncMock(return_value=Room())
    bot._is_mentioned = Mock(return_value=True)
    evt = message(
        "@alice:example.org",
        Content(
            {
                "body": "Hello @llm:example.org",
                "msgtype": MessageType.TEXT,
                "m.mentions": {"user_ids": ["@llm:example.org"]},
            }
        ),
    )
    with patch(
        "maubot_llm.bot.db.claim_event", new_callable=AsyncMock, return_value=False
    ) as claim:
        await bot.handle_msg(evt)
        claim.assert_awaited_once_with(bot.database, evt.event_id)
    evt.mark_read.assert_not_awaited()
    assert not bot._tasks

    bot.config["group_mode"] = "all"
    bot._is_mentioned = Mock(return_value=False)
    all_mode_event = message(
        "@alice:example.org",
        Content({"body": "Hello", "msgtype": MessageType.TEXT}),
    )
    all_mode_event.event_id = "$all-mode-event"
    with patch(
        "maubot_llm.bot.db.claim_event", new_callable=AsyncMock, return_value=False
    ) as claim:
        await bot.handle_msg(all_mode_event)
        claim.assert_awaited_once_with(bot.database, all_mode_event.event_id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "expected"),
    [("YES", True), (" yes. ", True), ("NO", False)],
)
async def test_group_intent_classifier_parses_verdict(response, expected):
    bot = make_bot()
    bot._request_semaphore = asyncio.Semaphore(1)
    bot.http = object()
    backend = Mock()
    backend.create_chat_completion = AsyncMock(
        return_value=ChatCompletion({"content": response}, "stop", "mock")
    )
    bot.get_backend = Mock(return_value=backend)
    room = Room()
    context = [{"role": "user", "content": "Could the bot help?"}]

    assert await bot._classify_group_message(room, context) is expected
    backend.create_chat_completion.assert_awaited_once()
    assert backend.create_chat_completion.await_args.kwargs["context"] == context
    classifier_prompt = backend.create_chat_completion.await_args.kwargs["system"]
    assert "asks Whereminator not to answer" in classifier_prompt
    assert "explicit mention alone is not enough" in classifier_prompt


@pytest.mark.asyncio
async def test_group_intent_classifier_rejects_ambiguous_verdict():
    bot = make_bot()
    bot._request_semaphore = asyncio.Semaphore(1)
    bot.http = object()
    backend = Mock()
    backend.create_chat_completion = AsyncMock(
        return_value=ChatCompletion({"content": "Probably yes"}, "stop", "mock")
    )
    bot.get_backend = Mock(return_value=backend)

    with pytest.raises(BackendError, match="invalid decision"):
        await bot._classify_group_message(Room(), [])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "mentioned"),
    [
        ("A message not for the bot", False),
        ("Whereminator, bitte antworte nicht auf diese Nachricht", True),
    ],
)
async def test_intent_mode_stores_messages_without_expected_reply(body, mentioned):
    bot = make_bot()
    bot.config.update(
        ignored_senders=[],
        group_mode="intent",
        direct_rooms=[],
        max_context_messages=40,
        context_ttl_seconds=604800,
    )
    bot.log = Mock()
    bot._context_locks = [asyncio.Lock() for _ in range(64)]
    bot.database = object()
    bot.client.set_typing = AsyncMock()
    bot._is_direct_message = AsyncMock(return_value=False)
    bot._is_mentioned = Mock(return_value=mentioned)
    room = Room()
    room.room_id = "!room:example.org"
    bot.get_room = AsyncMock(return_value=room)
    context = [{"role": "user", "content": body}]
    bot._classify_group_message = AsyncMock(return_value=False)
    evt = SimpleNamespace(
        sender="@alice:example.org",
        room_id=room.room_id,
        event_id="$intent-event",
        content=Content(
            {
                "body": body,
                "msgtype": MessageType.TEXT,
            }
        ),
        mark_read=AsyncMock(),
        reply=AsyncMock(),
    )
    tasks = []
    bot._track_task = tasks.append

    with (
        patch(
            "maubot_llm.bot.db.claim_event",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch("maubot_llm.bot.db.prune_expired_contexts", new_callable=AsyncMock),
        patch(
            "maubot_llm.bot.db.append_context", new_callable=AsyncMock
        ) as append_context,
        patch(
            "maubot_llm.bot.db.fetch_context",
            new_callable=AsyncMock,
            return_value=context,
        ),
    ):
        await bot.handle_msg(evt)
        await asyncio.gather(*tasks)

    append_context.assert_awaited_once_with(
        bot.database,
        room.room_id,
        "user",
        body,
        scope_id="",
        max_messages=40,
    )
    bot._classify_group_message.assert_awaited_once_with(room, context)
    evt.mark_read.assert_awaited_once()
    evt.reply.assert_not_awaited()
    bot.client.set_typing.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("mentioned", [False, True])
async def test_intent_mode_replies_when_classifier_accepts(mentioned):
    bot = make_bot()
    bot.config.update(
        ignored_senders=[],
        group_mode="intent",
        direct_rooms=[],
        max_context_messages=40,
        context_ttl_seconds=604800,
        max_response_chars=12000,
    )
    bot.log = Mock()
    bot._context_locks = [asyncio.Lock() for _ in range(64)]
    bot._request_semaphore = asyncio.Semaphore(1)
    bot.database = object()
    bot.http = object()
    bot.client.set_typing = AsyncMock()
    bot._is_direct_message = AsyncMock(return_value=False)
    room = Room()
    room.room_id = "!room:example.org"
    bot.get_room = AsyncMock(return_value=room)
    bot._classify_group_message = AsyncMock(return_value=True)
    backend = Mock()
    backend.default_system_prompt = "default system"
    backend.default_model = "mock"
    backend.create_chat_completion = AsyncMock(
        return_value=ChatCompletion(
            {"content": "Answer to the latest question"}, "stop", "mock"
        )
    )
    bot.get_backend = Mock(return_value=backend)
    context = [
        {"role": "user", "content": "Earlier question"},
        {"role": "assistant", "content": "Earlier answer"},
        {"role": "user", "content": "What about this latest question?"},
    ]
    content = {
        "body": "What about this latest question?",
        "msgtype": MessageType.TEXT,
    }
    if mentioned:
        content["m.mentions"] = {"user_ids": [bot.client.mxid]}
    evt = SimpleNamespace(
        sender="@alice:example.org",
        room_id=room.room_id,
        event_id="$intent-reply-event",
        content=Content(content),
        mark_read=AsyncMock(),
        reply=AsyncMock(),
    )
    tasks = []
    bot._track_task = tasks.append

    with (
        patch(
            "maubot_llm.bot.db.claim_event",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch("maubot_llm.bot.db.prune_expired_contexts", new_callable=AsyncMock),
        patch(
            "maubot_llm.bot.db.append_context", new_callable=AsyncMock
        ) as append_context,
        patch(
            "maubot_llm.bot.db.fetch_context",
            new_callable=AsyncMock,
            return_value=context,
        ),
    ):
        await bot.handle_msg(evt)
        await asyncio.gather(*tasks)

    bot._classify_group_message.assert_awaited_once_with(room, context)
    assert append_context.await_count == 2
    assert append_context.await_args_list[0].args[2:4] == (
        "user",
        "What about this latest question?",
    )
    assert append_context.await_args_list[1].args[2:4] == (
        "assistant",
        "Answer to the latest question",
    )
    backend.create_chat_completion.assert_awaited_once()
    assert backend.create_chat_completion.await_args.kwargs["context"] == context
    evt.reply.assert_awaited_once()
    assert evt.reply.await_args.args[0] == "Answer to the latest question"


@pytest.mark.asyncio
async def test_parallel_requests_respect_configured_limit():
    bot = make_bot()
    bot.config.update(
        context_ttl_seconds=86400,
        max_context_messages=40,
        max_response_chars=12000,
    )
    bot._context_locks = [asyncio.Lock() for _ in range(64)]
    bot._request_semaphore = asyncio.Semaphore(2)
    bot.client.set_typing = AsyncMock()
    bot.database = object()
    bot.http = object()
    bot.log = Mock()
    active = 0
    maximum_active = 0

    class MockBackend:
        default_system_prompt = None
        default_model = "mock"

        async def create_chat_completion(self, *_args, **_kwargs):
            nonlocal active, maximum_active
            active += 1
            maximum_active = max(maximum_active, active)
            await asyncio.sleep(0.01)
            active -= 1
            return ChatCompletion(
                {"role": "assistant", "content": "answer"}, "stop", "mock"
            )

    bot.get_backend = Mock(return_value=MockBackend())
    room = Room()
    room.room_id = "!room:example.org"
    events = [
        SimpleNamespace(
            room_id=room.room_id,
            reply=AsyncMock(),
        )
        for _ in range(4)
    ]
    scopes = []
    lock_slots = set()
    candidate = 0
    while len(scopes) < len(events):
        scope = f"scope-{candidate}"
        slot = hash((room.room_id, scope)) % len(bot._context_locks)
        if slot not in lock_slots:
            scopes.append(scope)
            lock_slots.add(slot)
        candidate += 1

    async def fetch_context(*_args, **_kwargs):
        return [{"role": "user", "content": "hello"}]

    with (
        patch("maubot_llm.bot.db.append_context", new_callable=AsyncMock),
        patch(
            "maubot_llm.bot.db.fetch_context",
            new_callable=AsyncMock,
            side_effect=fetch_context,
        ),
    ):
        await asyncio.gather(
            *(
                bot._generate_reply(evt, room, scope, "hello")
                for evt, scope in zip(events, scopes)
            )
        )
    assert maximum_active == 2
    assert all(evt.reply.await_count == 1 for evt in events)
