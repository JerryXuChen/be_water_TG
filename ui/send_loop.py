from __future__ import annotations

import asyncio
import logging
import random
import re
from time import monotonic
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, time
from enum import Enum
from typing import TYPE_CHECKING, Any

from telethon.errors import ChannelPrivateError, FloodWaitError, RPCError

from src.activity_observer import ActivityObserver, Observation
from src.config import Settings
from src.message_generator import MessageGenerator
from src.participation_policy import (
    CandidateKind,
    ParticipationCandidate,
    ParticipationPolicy,
)
from src.safety_guard import SafetyGuard
from src.sender import TelegramSender
from src.state_store import APP_TIMEZONE, PauseKind, StateStore
from ui.message_manager import MessageManager

if TYPE_CHECKING:
    from src.ai_sender import AISender
    from web_manager import EventBus, SendLoopManager

logger = logging.getLogger(__name__)

RETRY_DELAYS = [30, 60, 120]
MAX_RETRIES = 3
OBSERVATION_INTERVAL = 60


class SendState(Enum):
    IDLE = "idle"
    STARTING = "starting"
    RUNNING = "running"
    PAUSING = "pausing"
    PAUSED = "paused"
    STOPPING = "stopping"
    STOPPED = "stopped"
    WAITING_CODE = "waiting_code"


@dataclass
class SendRuntime:
    stopped: bool = False
    paused: bool = False
    total_count: int = 0
    per_group_counts: dict[str, int] = field(default_factory=dict)
    on_paused_callback: Callable[..., Any] | None = field(default=None)


@dataclass(frozen=True)
class PendingCandidate:
    candidate: ParticipationCandidate
    text: str
    source: str
    due_at: float


def _in_schedule(settings: Settings, now: time | None = None) -> bool:
    if not settings.schedule_enabled:
        return True
    current = now or datetime.now().time()
    return (
        time.fromisoformat(settings.schedule_morning_start)
        <= current
        <= time.fromisoformat(settings.schedule_morning_end)
    ) or (
        time.fromisoformat(settings.schedule_afternoon_start)
        <= current
        <= time.fromisoformat(settings.schedule_afternoon_end)
    )


async def _interruptible_wait(
    seconds: float,
    stop_event: Any,
    event_bus: EventBus | None = None,
) -> bool:
    """Wait in short slices. Return False when stop was requested."""
    remaining = max(0.0, float(seconds))
    while remaining > 0 and not stop_event.is_set():
        step = min(0.5, remaining)
        await asyncio.sleep(step)
        remaining -= step
        if event_bus and remaining >= 0:
            await event_bus.emit_countdown(int(remaining))
    return not stop_event.is_set()


def _candidate_from_observation(observation: Observation) -> ParticipationCandidate | None:
    if observation.new_messages:
        latest = observation.new_messages[-1]
        text = latest.text.strip()
        question_markers = (
            "?",
            "？",
            "吗",
            "如何",
            "怎么",
            "为什么",
            "谁",
            "哪",
            "求解决",
            "求助",
            "请教",
            "怎么办",
            "能否",
            "可不可以",
            "有没有人知道",
        )
        kind = (
            CandidateKind.QUESTION
            if any(marker in text for marker in question_markers)
            else CandidateKind.DISCUSSION
        )
        compact = re.sub(r"\s+", "", text.casefold())
        low_signal = {
            "哈", "哈哈", "哈哈哈", "嗯", "哦", "ok", "好的", "收到", "谢谢", "早", "晚安",
        }
        relevant = (
            kind is CandidateKind.QUESTION
            or (
                len(compact) >= 6
                and compact not in low_signal
                and not compact.startswith(("http://", "https://"))
            )
        )
        return ParticipationCandidate(
            group=observation.group,
            kind=kind,
            relevant=relevant,
            trigger_message_id=latest.message_id,
        )
    if observation.idle and not observation.latest_message_is_self:
        return ParticipationCandidate(observation.group, CandidateKind.IDLE)
    return None


# 文本超过该字数（字符数）直接判为 IRRELEVANT，不再调用 AI 分类。
MAX_CLASSIFY_TEXT_LEN = 10


async def _classify_candidate_with_ai(
    ai_sender: AISender | None,
    candidate: ParticipationCandidate,
    text: str,
    context_count: int,
) -> ParticipationCandidate:
    """Prefer semantic classification and retain a deterministic fallback."""
    # 超长文本直接判不相关，跳过 AI 调用。
    if len(text.strip()) > MAX_CLASSIFY_TEXT_LEN:
        logger.debug(
            "[分类] %s 文本超 %d 字(共%d)，直接 IRRELEVANT 跳过AI",
            candidate.group, MAX_CLASSIFY_TEXT_LEN, len(text.strip()),
        )
        return replace(candidate, relevant=False)
    if ai_sender is None or candidate.kind is CandidateKind.IDLE:
        return candidate
    classifier = getattr(ai_sender, "classify_message", None)
    if not callable(classifier):
        return candidate
    try:
        classification = await classifier(candidate.group, text, context_count)
    except Exception:
        logger.warning(
            "AI classification failed; using deterministic fallback [%s]",
            candidate.group,
            exc_info=True,
        )
        return candidate
    if isinstance(classification, CandidateKind):
        classification = classification.value
    normalized = str(classification).strip().casefold()
    if normalized == CandidateKind.QUESTION.value:
        return replace(candidate, kind=CandidateKind.QUESTION, relevant=True)
    if normalized == CandidateKind.DISCUSSION.value:
        return replace(candidate, kind=CandidateKind.DISCUSSION, relevant=True)
    if normalized == "irrelevant":
        # 放宽判断：AI 判为 IRRELEVANT 不再一票否决，回退到本地关键词判定
        # （含 吗/如何/怎么 等关键词 -> QUESTION 且 relevant=True），保留对纯垃圾的过滤。
        logger.debug(
            "[分类] %s AI 判 IRRELEVANT，回退本地关键词判定 (relevant=%s)",
            candidate.group, candidate.relevant,
        )
        return candidate
    logger.warning(
        "Unknown AI classification %r; using deterministic fallback [%s]",
        classification,
        candidate.group,
    )
    return candidate


async def _send_with_retry(
    sender: TelegramSender,
    group: str,
    message: str,
    stop_event: Any,
    event_bus: EventBus | None,
) -> bool:
    for attempt in range(MAX_RETRIES):
        try:
            await sender.send_message(message, target_group=group)
            return True
        except FloodWaitError as exc:
            logger.warning("FloodWait %ds; entering global cooldown", exc.seconds)
            if event_bus:
                await event_bus.emit_alert(
                    group, "flood_wait", f"Telegram requested {exc.seconds}s cooldown"
                )
            if not await _interruptible_wait(exc.seconds, stop_event, event_bus):
                return False
        except (ConnectionError, OSError) as exc:
            if attempt >= MAX_RETRIES - 1:
                logger.error("Network retries exhausted [%s]: %s", group, exc)
                return False
            delay = RETRY_DELAYS[attempt]
            logger.warning("Network error [%s], retrying in %ds: %s", group, delay, exc)
            if not await _interruptible_wait(delay, stop_event, event_bus):
                return False
        except RPCError as exc:
            logger.error("Telegram RPC error [%s]: %s", group, exc)
            return False
        except Exception:
            logger.exception("Unexpected send error [%s]", group)
            return False
    return False


async def _loose_send(
    sender: TelegramSender,
    store: StateStore,
    manager: SendLoopManager,
    event_bus: EventBus | None,
    settings: Settings,
    group: str,
    observation_limit: int,
    message_manager: MessageManager,
) -> None:
    """宽松群铺量发送逻辑。

    规则：拉取最近 observation_limit 条消息，若其中没有任何一条是自己发送的，
    则从语料库随机取一条，按逗号拆分为 2-5 句，逐条发送（每条间隔随机 5-10 秒）。
    距离上次发送需冷却 loose_cooldown_min 分钟，且计入 daily_limit。
    """
    state = store.get_group_state(group)
    if state.paused:
        return

    # 冷却检查：距上次发送不足冷却时间则不触发
    if state.last_outbound_at is not None:
        elapsed = (datetime.now(APP_TIMEZONE) - state.last_outbound_at).total_seconds()
        if elapsed < settings.loose_cooldown_min * 60:
            logger.debug(
                "[宽松] %s 冷却中（剩余 %.0fs），跳过",
                group, settings.loose_cooldown_min * 60 - elapsed,
            )
            return

    # 日限额检查
    if state.sent_count >= settings.daily_limit:
        logger.debug("[宽松] %s 已达日限额 %d，跳过", group, settings.daily_limit)
        return

    try:
        raw_messages = await sender.get_recent_messages(group, limit=observation_limit)
    except ChannelPrivateError:
        logger.warning("[宽松] %s 无法读取（可能未加入/被移出）", group)
        return
    except Exception:
        logger.exception("[宽松] %s 拉取消息失败", group)
        return

    # 最近 N 条是否含自己发送
    contains_self = any(getattr(m, "is_self", False) for m in raw_messages)
    logger.debug(
        "[宽松] %s 最近%d条 含自己发送=%s",
        group, len(raw_messages), contains_self,
    )
    if contains_self:
        return

    # 取语料并拆分
    corpus = message_manager.load_loose_messages(settings.loose_message_file)
    if not corpus:
        logger.warning("[宽松] %s 语料库为空，跳过", group)
        return
    chosen = random.choice(corpus)
    parts = [p.strip() for p in chosen.split(",") if p.strip()]
    if not parts:
        return
    count = min(
        max(random.randint(settings.loose_min_parts, settings.loose_max_parts), 1),
        len(parts),
    )
    selected = random.sample(parts, count) if count < len(parts) else parts

    logger.info(
        "[宽松] %s 触发铺量：发送 %d 条（语料: %s）",
        group, count, chosen[:30],
    )

    for i, text in enumerate(selected):
        if manager.state in (SendState.PAUSING, SendState.PAUSED):
            break
        # 逐条发送前再次检查总额（避免超额）
        cur = store.get_group_state(group)
        if cur.sent_count >= settings.daily_limit:
            logger.debug("[宽松] %s 发送中途达日限额，停止", group)
            break
        try:
            store.reserve_send(group, settings.daily_limit)
        except Exception as exc:
            logger.warning("[宽松] %s 无法预留额度: %s", group, exc)
            break
        sent = await _send_with_retry(sender, group, text, manager.stop_event, event_bus)
        if not sent:
            try:
                store.release_send_reservation(group)
            except Exception:
                logger.exception("[宽松] %s 释放额度失败", group)
            break
        try:
            persisted = store.confirm_send(group, settings.daily_limit)
        except Exception as exc:
            logger.exception("[宽松] %s 发送后状态确认失败", group)
            break
        total, per_group = manager.increment_count(group, persisted.sent_count)
        store.record_audit(group, "loose_sent", "loose", {"count": persisted.sent_count})
        if event_bus:
            await event_bus.emit_counter(total, per_group)
            await event_bus.emit_group_state(StateStore.serialize_state(persisted))
        # 每条之间随机间隔 5-10 秒（最后一条后无需等待）
        if i < count - 1:
            gap = random.randint(settings.loose_part_gap_min, settings.loose_part_gap_max)
            logger.debug("[宽松] %s 第%d条已发，间隔 %ds", group, i + 1, gap)
            await _interruptible_wait(gap, manager.stop_event, event_bus)


async def send_loop(
    sender: TelegramSender,
    settings: Settings,
    manager: SendLoopManager,
    message_manager: MessageManager,
    event_bus: EventBus | None = None,
    ai_sender: AISender | None = None,
    state_store: StateStore | None = None,
) -> None:
    """Observe authorized groups and send only policy-approved candidates."""
    stop_event = manager.stop_event
    store = state_store or StateStore(settings.state_db_path)
    observer = ActivityObserver(store, settings.idle_threshold_minutes)
    policy = ParticipationPolicy()
    safety = SafetyGuard()
    generator = MessageGenerator(ai_sender, message_manager)

    store.reconcile_pending_reservations(
        settings.target_groups,
        settings.daily_limit,
    )
    for state in store.list_group_states(settings.target_groups):
        manager.set_persisted_count(state.group, state.sent_count)

    # 宽松群集合（与白名单群互斥：出现在 LOOSE_GROUPS 中的群走铺量逻辑）
    loose_groups = set(settings.loose_groups) & set(settings.target_groups)
    for lg in settings.loose_groups:
        if lg not in settings.target_groups:
            logger.warning("宽松群未出现在 TARGET_GROUPS 中，将被忽略: %s", lg)
    LOOSE_OBSERVE_LIMIT = 10  # 宽松群观察最近 N 条以判断“是否含自己发送”

    pending: dict[str, PendingCandidate] = {}

    async def apply_safety(group: str, observation: Observation) -> bool:
        """Persist a safety decision and return whether the group is blocked."""
        safety_decision = safety.inspect(observation.new_messages)
        if safety_decision.should_pause:
            paused = store.pause_group(group, PauseKind.SAFETY, safety_decision.reason)
            if event_bus:
                await event_bus.emit_alert(group, "safety_pause", safety_decision.reason)
                await event_bus.emit_group_state(StateStore.serialize_state(paused))
            return True
        if safety_decision.should_audit:
            store.record_audit(group, "safety_notice", safety_decision.reason)
            if event_bus:
                await event_bus.emit_decision(group, "notice", safety_decision.reason)
        return False

    async def pause_inaccessible(group: str) -> None:
        reason = "账号无法读取该群：可能未加入、已被移出，或群组链接为私有"
        paused = store.pause_group(group, PauseKind.MANUAL, reason)
        logger.warning("Pausing inaccessible group [%s]: %s", group, reason)
        if event_bus:
            await event_bus.emit_alert(group, "group_inaccessible", reason)
            await event_bus.emit_group_state(StateStore.serialize_state(paused))

    async def apply_flood_wait(group: str, exc: FloodWaitError) -> None:
        """Enter the same global cooldown for send and permission rate limits."""
        logger.warning(
            "FloodWait %ds while checking permissions [%s]",
            exc.seconds,
            group,
        )
        if event_bus:
            await event_bus.emit_alert(
                group,
                "flood_wait",
                f"Telegram requested {exc.seconds}s cooldown",
            )
        await _interruptible_wait(exc.seconds, stop_event, event_bus)

    while not stop_event.is_set():
        if manager.state == SendState.PAUSING:
            manager.transition(SendState.PAUSED)
            if event_bus:
                await event_bus.emit_status("paused")
        while manager.state == SendState.PAUSED and not stop_event.is_set():
            await asyncio.sleep(0.5)
        if stop_event.is_set():
            break

        if not _in_schedule(settings):
            if event_bus:
                await event_bus.emit_health("scheduled_wait", "Outside configured work window")
            await _interruptible_wait(60, stop_event, event_bus)
            continue

        # Resolve due candidates first. Their wait is represented by due_at,
        # so other groups continue to be observed while one group is waiting.
        for group, item in list(pending.items()):
            if stop_event.is_set() or manager.state in (SendState.PAUSING, SendState.PAUSED):
                break
            if item.due_at > monotonic():
                continue
            pending.pop(group, None)
            try:
                recheck = await observer.observe(sender, group)
            except FloodWaitError as exc:
                await apply_flood_wait(group, exc)
                continue
            except ChannelPrivateError:
                await pause_inaccessible(group)
                continue
            except Exception:
                logger.exception("Failed to re-observe group [%s]", group)
                store.record_audit(group, "candidate_cancelled", "recheck_failed")
                continue
            if await apply_safety(group, recheck):
                continue
            if recheck.new_messages or recheck.latest_message_is_self:
                store.record_audit(group, "candidate_cancelled", "activity_changed_during_wait")
                if event_bus:
                    await event_bus.emit_decision(group, "cancel", "activity_changed_during_wait")
                continue
            if not policy.revalidate(store.get_group_state(group), settings).allowed:
                store.record_audit(group, "candidate_cancelled", "revalidation_failed")
                if event_bus:
                    await event_bus.emit_decision(group, "cancel", "revalidation_failed")
                continue
            try:
                store.reserve_send(group, settings.daily_limit)
            except Exception as exc:
                logger.warning("Unable to reserve send quota [%s]: %s", group, exc)
                store.record_audit(group, "candidate_cancelled", "quota_reservation_failed")
                if event_bus:
                    await event_bus.emit_group_state(
                        StateStore.serialize_state(store.get_group_state(group))
                    )
                continue
            sent = await _send_with_retry(sender, group, item.text, stop_event, event_bus)
            if not sent:
                try:
                    store.release_send_reservation(group)
                except Exception:
                    logger.exception("Failed to release send reservation [%s]", group)
                    manager.request_emergency_stop()
                store.record_audit(group, "send_failed", "network_or_rpc")
                continue
            try:
                persisted = store.confirm_send(group, settings.daily_limit)
            except Exception as exc:
                logger.exception("State confirmation failed after send [%s]", group)
                if event_bus:
                    await event_bus.emit_alert(group, "persistence_failure", str(exc))
                manager.request_emergency_stop()
                break
            try:
                generator.commit(group, item.text, item.source)
            except Exception:
                logger.exception("Failed to commit sent-message history [%s]", group)
                store.record_audit(group, "history_commit_failed", item.source)
            total, per_group = manager.increment_count(group, persisted.sent_count)
            store.record_audit(
                group, "sent", item.source, {"count": persisted.sent_count}
            )
            if event_bus:
                await event_bus.emit_counter(total, per_group)
                await event_bus.emit_group_state(StateStore.serialize_state(persisted))

        for group in settings.target_groups:
            if stop_event.is_set() or manager.state in (SendState.PAUSING, SendState.PAUSED):
                break
            if group in pending:
                continue
            persisted_state = store.get_group_state(group)
            if persisted_state.paused:
                continue

            # 宽松群走独立的铺量发送逻辑
            if group in loose_groups:
                try:
                    await _loose_send(
                        sender, store, manager, event_bus, settings,
                        group, observation_limit=LOOSE_OBSERVE_LIMIT,
                        message_manager=message_manager,
                    )
                except Exception:
                    logger.exception("宽松群处理异常 [%s]", group)
                continue
            try:
                observation = await observer.observe(sender, group)
            except FloodWaitError as exc:
                await apply_flood_wait(group, exc)
                continue
            except ChannelPrivateError:
                await pause_inaccessible(group)
                continue
            except Exception:
                logger.exception("Failed to observe group [%s]", group)
                if event_bus:
                    await event_bus.emit_decision(group, "skip", "observation_failed")
                continue

            logger.debug(
                "[观察] %s 拉到消息=%d 最新是否自己=%s idle=%s 新消息=%s",
                group,
                len(observation.new_messages),
                observation.latest_message_is_self,
                observation.idle,
                [m.text[:40] for m in observation.new_messages][-5:],
            )

            if await apply_safety(group, observation):
                continue

            current_state = store.get_group_state(group)
            if (
                not observation.new_messages
                and observation.latest_message_is_self
                and current_state.last_outbound_at
            ):
                previous_check = current_state.last_unanswered_checked_at
                unanswered = store.mark_unanswered(
                    group, window_minutes=settings.idle_threshold_minutes
                )
                if unanswered.last_unanswered_checked_at != previous_check:
                    store.record_audit(
                        group,
                        "unanswered",
                        f"consecutive:{unanswered.consecutive_unanswered}",
                    )
                    if event_bus:
                        await event_bus.emit_group_state(
                            StateStore.serialize_state(unanswered)
                        )

            candidate = _candidate_from_observation(observation)
            if candidate is None:
                continue
            if settings.ai_enabled and observation.new_messages:
                candidate = await _classify_candidate_with_ai(
                    ai_sender,
                    candidate,
                    observation.new_messages[-1].text,
                    settings.ai_context_count,
                )
            state = store.get_group_state(group)
            decision = policy.evaluate(candidate, state, settings)
            logger.debug(
                "[决策] %s 候选类型=%s relevant=%s 是否允许=%s 原因=%s 已发=%d/日限=%d",
                group,
                candidate.kind.value,
                candidate.relevant,
                decision.allowed,
                decision.reason,
                state.sent_count,
                settings.daily_limit,
            )
            store.record_audit(
                group,
                "decision",
                decision.reason,
                {"kind": candidate.kind.value, "allowed": decision.allowed},
            )
            if event_bus:
                await event_bus.emit_decision(
                    group, "allow" if decision.allowed else "skip", decision.reason
                )
            if not decision.allowed:
                continue

            if settings.ai_enabled and ai_sender is not None:
                try:
                    if await ai_sender.should_skip(group, settings.ai_context_count):
                        store.record_audit(
                            group, "decision", "last_message_still_recent",
                            {"kind": candidate.kind.value, "allowed": False},
                        )
                        if event_bus:
                            await event_bus.emit_decision(
                                group, "skip", "last_message_still_recent"
                            )
                        continue
                except ChannelPrivateError:
                    await pause_inaccessible(group)
                    continue
                except Exception:
                    logger.warning(
                        "Unable to check recent own message [%s]", group,
                        exc_info=True,
                    )

            result = await generator.generate(
                group,
                settings.ai_prompt,
                settings.ai_context_count,
                settings.ai_enabled,
            )
            if result.text is None:
                store.record_audit(group, "generation_skipped", result.reason)
                if event_bus:
                    await event_bus.emit_decision(group, "skip", result.reason)
                continue

            delay = random.randint(settings.reply_delay_min, settings.reply_delay_max)
            pending[group] = PendingCandidate(
                candidate=candidate,
                text=result.text,
                source=result.source or "unknown",
                due_at=monotonic() + delay,
            )
            if event_bus:
                await event_bus.emit_decision(group, "waiting", f"delay:{delay}")

        if stop_event.is_set():
            break
        if pending:
            next_due = min(item.due_at for item in pending.values())
            wait_seconds = min(OBSERVATION_INTERVAL, max(0.0, next_due - monotonic()))
            if event_bus:
                await event_bus.emit_countdown(int(max(0.0, next_due - monotonic())))
        else:
            wait_seconds = OBSERVATION_INTERVAL
        await _interruptible_wait(wait_seconds, stop_event)

    total, _ = manager.runtime_counts_snapshot()
    logger.info("Participation loop exited (session total: %d)", total)
