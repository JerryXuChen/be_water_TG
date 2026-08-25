from __future__ import annotations

import inspect
import logging
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from time import monotonic
from typing import Any, Callable

from telethon.errors import FloodWaitError

from src.state_store import APP_TIMEZONE, StateStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MessageSnapshot:
    message_id: int
    sender_id: int | None
    text: str
    occurred_at: datetime
    is_self: bool = False
    sender_is_admin: bool = False
    reply_to_message_id: int | None = None
    reply_to_is_self: bool = False


@dataclass(frozen=True)
class Observation:
    group: str
    new_messages: tuple[MessageSnapshot, ...]
    idle: bool
    latest_message_is_self: bool


class ActivityObserver:
    """Convert Telethon messages into stable snapshots and activity candidates."""

    def __init__(
        self,
        store: StateStore,
        idle_minutes: int,
        now: Callable[[], datetime] | None = None,
        permission_cache_ttl: float = 300.0,
        monotonic_clock: Callable[[], float] | None = None,
    ) -> None:
        self._store = store
        self._idle_delta = timedelta(minutes=idle_minutes)
        self._now = now or (lambda: datetime.now(APP_TIMEZONE))
        self._permission_cache_ttl = permission_cache_ttl
        self._monotonic = monotonic_clock or monotonic
        self._admin_cache: dict[tuple[str, int], tuple[bool, float]] = {}

    @staticmethod
    def snapshot(message: Any) -> MessageSnapshot:
        sender = getattr(message, "sender", None)
        sender_id = getattr(message, "sender_id", None)
        if sender_id is None and sender is not None:
            sender_id = getattr(sender, "id", None)
        occurred_at = getattr(message, "date", None) or datetime.now(APP_TIMEZONE)
        if occurred_at.tzinfo is None:
            occurred_at = occurred_at.replace(tzinfo=APP_TIMEZONE)
        reply_to = getattr(message, "reply_to", None)
        reply_to_message_id = (
            getattr(reply_to, "reply_to_msg_id", None)
            or getattr(message, "reply_to_msg_id", None)
        )
        return MessageSnapshot(
            message_id=int(getattr(message, "id", 0)),
            sender_id=sender_id,
            text=(getattr(message, "text", None) or "").strip(),
            occurred_at=occurred_at.astimezone(APP_TIMEZONE),
            is_self=bool(getattr(message, "out", False) or getattr(sender, "is_self", False)),
            sender_is_admin=bool(
                getattr(sender, "admin_rights", None)
                or getattr(sender, "creator", False)
            ),
            reply_to_message_id=reply_to_message_id,
        )

    async def _with_admin_flags(
        self, sender: Any, group: str, snapshots: list[MessageSnapshot]
    ) -> list[MessageSnapshot]:
        """Resolve current group permissions when the sender supports it.

        Telethon message senders are normally User objects and do not reliably
        include group-role information, so their fields are only a fallback.
        """
        resolver = getattr(sender, "is_group_admin", None)
        if not callable(resolver):
            return snapshots
        enriched: list[MessageSnapshot] = []
        for item in snapshots:
            is_admin = item.sender_is_admin
            if not is_admin and item.sender_id is not None and not item.is_self:
                cache_key = (group, item.sender_id)
                cached = self._admin_cache.get(cache_key)
                if cached and cached[1] > self._monotonic():
                    enriched.append(replace(item, sender_is_admin=cached[0]))
                    continue
                try:
                    resolved = resolver(group, item.sender_id)
                    if inspect.isawaitable(resolved):
                        resolved = await resolved
                    is_admin = bool(resolved)
                    self._admin_cache[cache_key] = (
                        is_admin,
                        self._monotonic() + self._permission_cache_ttl,
                    )
                except FloodWaitError:
                    raise
                except Exception:
                    # Permission lookup is a safety enhancement; the normal
                    # complaint classifier still handles explicit warnings.
                    is_admin = False
            enriched.append(replace(item, sender_is_admin=is_admin))
        return enriched

    async def observe(self, sender: Any, group: str, limit: int = 20) -> Observation:
        state = self._store.get_group_state(group)
        raw_messages = await sender.get_recent_messages(group, limit=limit)
        logger.debug(
            "[拉取] %s 原始消息条数=%d 历史last_message_id=%s",
            group, len(raw_messages), state.last_message_id,
        )
        snapshots = sorted(
            (self.snapshot(message) for message in raw_messages),
            key=lambda item: item.message_id,
        )
        self_message_ids = {item.message_id for item in snapshots if item.is_self}
        snapshots = [
            replace(
                item,
                reply_to_is_self=bool(
                    item.reply_to_message_id
                    and item.reply_to_message_id in self_message_ids
                ),
            )
            for item in snapshots
        ]
        if state.last_message_id is None and snapshots:
            latest = snapshots[-1]
            non_self = [item for item in snapshots if not item.is_self and item.text]
            activity_time = non_self[-1].occurred_at if non_self else latest.occurred_at
            self._store.touch_activity(group, activity_time, latest.message_id)
            now = self._now()
            if now.tzinfo is None:
                now = now.replace(tzinfo=APP_TIMEZONE)
            # The initial pass establishes a durable baseline only. Historical
            # quiet time must not turn into an immediate cold-start candidate.
            return Observation(group, (), False, latest.is_self)
        latest_id = state.last_message_id or 0
        new_messages = [
            item
            for item in snapshots
            if item.message_id > latest_id and item.text and not item.is_self
        ]
        new_messages = await self._with_admin_flags(sender, group, new_messages)
        new_messages_tuple = tuple(new_messages)
        latest = snapshots[-1] if snapshots else None
        if new_messages_tuple:
            newest = new_messages_tuple[-1]
            self._store.touch_activity(group, newest.occurred_at, newest.message_id)
            state = self._store.get_group_state(group)
        elif latest and latest.message_id > latest_id:
            # Advance the cursor for self-authored or non-text messages without
            # treating them as fresh group activity.
            self._store.touch_activity(
                group,
                state.last_activity_at and datetime.fromisoformat(state.last_activity_at)
                or latest.occurred_at,
                latest.message_id,
            )
            state = self._store.get_group_state(group)

        now = self._now()
        if now.tzinfo is None:
            now = now.replace(tzinfo=APP_TIMEZONE)
        last_activity = (
            datetime.fromisoformat(state.last_activity_at)
            if state.last_activity_at
            else None
        )
        latest_is_self = bool(latest and latest.is_self)
        idle = bool(
            not new_messages_tuple
            and last_activity is not None
            and now.astimezone(APP_TIMEZONE) - last_activity >= self._idle_delta
            and not latest_is_self
        )
        return Observation(group, new_messages_tuple, idle, latest_is_self)
