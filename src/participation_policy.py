from __future__ import annotations

import random
from dataclasses import dataclass
from enum import Enum
from typing import Callable

from src.config import Settings
from src.state_store import GroupState


class CandidateKind(str, Enum):
    QUESTION = "question"
    DISCUSSION = "discussion"
    IDLE = "idle"


@dataclass(frozen=True)
class ParticipationCandidate:
    group: str
    kind: CandidateKind
    relevant: bool = True
    trigger_message_id: int | None = None


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    reason: str
    probability: int = 0


class ParticipationPolicy:
    def __init__(self, randint: Callable[[int, int], int] | None = None) -> None:
        self._randint = randint or random.randint

    def evaluate(
        self,
        candidate: ParticipationCandidate,
        state: GroupState,
        settings: Settings,
    ) -> PolicyDecision:
        if state.paused:
            return PolicyDecision(False, f"paused:{state.pause_kind.value}")
        if state.sent_count >= settings.daily_limit:
            return PolicyDecision(False, "daily_limit")
        if not candidate.relevant:
            return PolicyDecision(False, "not_relevant")
        probability = {
            CandidateKind.QUESTION: settings.question_reply_pct,
            CandidateKind.DISCUSSION: settings.discussion_reply_pct,
            CandidateKind.IDLE: settings.discussion_reply_pct,
        }[candidate.kind]
        if self._randint(1, 100) > probability:
            return PolicyDecision(False, "probability", probability)
        return PolicyDecision(True, "allowed", probability)

    def revalidate(self, state: GroupState, settings: Settings) -> PolicyDecision:
        if state.paused:
            return PolicyDecision(False, f"paused:{state.pause_kind.value}")
        if state.sent_count >= settings.daily_limit:
            return PolicyDecision(False, "daily_limit")
        return PolicyDecision(True, "allowed")
