from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from zoneinfo import ZoneInfo

import pytest
from telethon.errors import FloodWaitError

from src.activity_observer import ActivityObserver, MessageSnapshot
from src.ai_sender import AISender
from src.config import Settings
from src.message_generator import MessageGenerator
from src.participation_policy import CandidateKind, ParticipationCandidate, ParticipationPolicy
from src.safety_guard import SafetyGuard
from src.state_store import PauseKind, StateStore


TZ = ZoneInfo("Asia/Shanghai")


def settings(**overrides) -> Settings:
    values = dict(api_id=1, api_hash="x" * 32, phone="+1", target_groups=["group"])
    values.update(overrides)
    return Settings(**values)


def test_policy_uses_kind_probability(tmp_path) -> None:
    store = StateStore(tmp_path / "state.db")
    state = store.get_group_state("group")
    policy = ParticipationPolicy(randint=lambda _a, _b: 50)
    question = policy.evaluate(
        ParticipationCandidate("group", CandidateKind.QUESTION), state, settings()
    )
    discussion = policy.evaluate(
        ParticipationCandidate("group", CandidateKind.DISCUSSION), state, settings()
    )
    assert question.allowed
    assert not discussion.allowed
    assert discussion.reason == "probability"


def test_policy_rejects_paused_and_irrelevant(tmp_path) -> None:
    store = StateStore(tmp_path / "state.db")
    paused = store.pause_group("group", PauseKind.SAFETY, "complaint")
    policy = ParticipationPolicy(randint=lambda _a, _b: 1)
    assert not policy.evaluate(
        ParticipationCandidate("group", CandidateKind.QUESTION), paused, settings()
    ).allowed
    active = store.resume_group("group")
    assert policy.evaluate(
        ParticipationCandidate("group", CandidateKind.QUESTION, relevant=False),
        active,
        settings(),
    ).reason == "not_relevant"


def test_safety_guard_detects_explicit_complaint_and_admin_warning() -> None:
    now = datetime.now(TZ)
    guard = SafetyGuard()
    complaint = MessageSnapshot(1, 2, "别发了，太刷屏", now)
    warning = MessageSnapshot(2, 3, "管理员提醒：违反群规", now, sender_is_admin=True)
    assert guard.inspect((complaint,)).reason == "explicit complaint"
    result = guard.inspect((warning,))
    assert result.should_pause and result.severe


def test_observer_returns_new_non_self_messages_and_idle(tmp_path) -> None:
    now = datetime(2026, 7, 28, 12, 0, tzinfo=TZ)
    store = StateStore(tmp_path / "state.db", now=lambda: now)
    sender = AsyncMock()
    sender.get_recent_messages.return_value = [
        SimpleNamespace(id=2, text="hello", date=now - timedelta(minutes=11), out=False, sender_id=2, sender=None),
        SimpleNamespace(id=1, text="old", date=now - timedelta(minutes=12), out=True, sender_id=1, sender=None),
    ]
    observer = ActivityObserver(store, 10, now=lambda: now)
    first = asyncio.run(observer.observe(sender, "group"))
    assert not first.new_messages
    assert not first.idle
    sender.get_recent_messages.return_value.insert(
        0,
        SimpleNamespace(
            id=3,
            text="new question?",
            date=now,
            out=False,
            sender_id=3,
            sender=None,
        ),
    )
    second = asyncio.run(observer.observe(sender, "group"))
    assert [message.message_id for message in second.new_messages] == [3]


def test_message_generator_ai_then_txt_fallback() -> None:
    ai = AsyncMock()
    ai.generate_message.side_effect = RuntimeError("offline")
    manager = Mock()
    manager.get_message.return_value = "fallback"
    generator = MessageGenerator(ai, manager)
    result = asyncio.run(generator.generate("group", "prompt", 5, True))
    assert result.text == "fallback"
    assert result.source == "txt"


def test_message_generator_skips_irrelevant_ai_result() -> None:
    ai = AsyncMock()
    ai.generate_message.return_value = "[SKIP]"
    manager = Mock()
    manager.get_message.return_value = "fallback"
    generator = MessageGenerator(ai, manager)
    result = asyncio.run(generator.generate("group", "prompt", 5, True))
    assert result.text is None
    assert result.reason == "not_relevant"
    manager.get_message.assert_not_called()


def test_message_generator_commits_duplicate_history_only_after_send() -> None:
    ai = SimpleNamespace(
        generate_message=AsyncMock(return_value="候选消息"),
        commit_sent=Mock(),
    )
    manager = Mock()
    generator = MessageGenerator(ai, manager)

    first = asyncio.run(generator.generate("group", "prompt", 5, True))
    cancelled_retry = asyncio.run(generator.generate("group", "prompt", 5, True))
    assert first.text == cancelled_retry.text == "候选消息"
    ai.commit_sent.assert_not_called()

    generator.commit("group", first.text, first.source or "ai")
    duplicate = asyncio.run(generator.generate("group", "prompt", 5, True))
    assert duplicate.reason == "duplicate"
    ai.commit_sent.assert_called_once_with("group", "候选消息")


def test_ai_sender_semantic_classifier_accepts_only_known_label() -> None:
    client = Mock()
    client.chat.return_value = "QUESTION"
    ai_sender = AISender(AsyncMock(), client)

    result = asyncio.run(ai_sender.classify_message("group", "这个问题求解决"))

    assert result == "question"
    with pytest.raises(ValueError, match="Unexpected classification"):
        client.chat.return_value = "可能是问题，也可能是讨论"
        asyncio.run(ai_sender.classify_message("group", "模糊内容"))


def test_safety_guard_audits_generic_spam_but_does_not_pause() -> None:
    now = datetime.now(TZ)
    guard = SafetyGuard()
    mention = MessageSnapshot(1, 2, "隔壁机器人太刷屏了", now)
    result = guard.inspect((mention,))
    assert not result.should_pause
    assert result.should_audit


def test_safety_guard_pauses_targeted_low_confidence_complaint() -> None:
    now = datetime.now(TZ)
    guard = SafetyGuard()
    direct = guard.inspect((MessageSnapshot(1, 2, "你太刷屏了", now),))
    reply = guard.inspect(
        (MessageSnapshot(2, 3, "有点打扰", now, reply_to_is_self=True),)
    )
    assert direct.should_pause and direct.reason == "explicit complaint"
    assert reply.should_pause and reply.reason == "explicit complaint"


def test_observer_resolves_admin_role_from_sender_permissions(tmp_path) -> None:
    now = datetime.now(TZ)
    store = StateStore(tmp_path / "state.db", now=lambda: now)
    sender = AsyncMock()
    sender.get_recent_messages.return_value = [
        SimpleNamespace(
            id=1, text="管理员提醒：违反群规", date=now, out=False,
            sender_id=22, sender=None,
        )
    ]
    sender.is_group_admin.return_value = True
    store.touch_activity("group", now, message_id=0)
    observation = asyncio.run(ActivityObserver(store, 10, now=lambda: now).observe(sender, "group"))
    assert observation.new_messages[0].sender_is_admin


def test_observer_queries_unique_new_senders_and_caches_permissions(tmp_path) -> None:
    now = datetime.now(TZ)
    store = StateStore(tmp_path / "state.db", now=lambda: now)
    store.touch_activity("group", now - timedelta(minutes=1), message_id=5)
    sender = AsyncMock()
    sender.get_recent_messages.return_value = [
        SimpleNamespace(id=7, text="new two", date=now, out=False, sender_id=22, sender=None),
        SimpleNamespace(id=6, text="new one", date=now, out=False, sender_id=22, sender=None),
        SimpleNamespace(id=5, text="old", date=now, out=False, sender_id=11, sender=None),
    ]
    sender.is_group_admin.return_value = True
    observer = ActivityObserver(store, 10, now=lambda: now)

    first = asyncio.run(observer.observe(sender, "group"))
    assert all(item.sender_is_admin for item in first.new_messages)
    sender.is_group_admin.assert_awaited_once_with("group", 22)

    store.touch_activity("group", now, message_id=5)
    second = asyncio.run(observer.observe(sender, "group"))
    assert all(item.sender_is_admin for item in second.new_messages)
    assert sender.is_group_admin.await_count == 1


def test_observer_propagates_flood_wait_from_permission_lookup(tmp_path) -> None:
    now = datetime.now(TZ)
    store = StateStore(tmp_path / "state.db", now=lambda: now)
    store.touch_activity("group", now, message_id=0)
    sender = AsyncMock()
    sender.get_recent_messages.return_value = [
        SimpleNamespace(id=1, text="管理员提醒", date=now, out=False, sender_id=22, sender=None)
    ]
    sender.is_group_admin.side_effect = FloodWaitError(request=Mock(), capture=30)

    with pytest.raises(FloodWaitError):
        asyncio.run(ActivityObserver(store, 10, now=lambda: now).observe(sender, "group"))
