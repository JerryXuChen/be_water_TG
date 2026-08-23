from __future__ import annotations

from dataclasses import dataclass

from src.activity_observer import MessageSnapshot


DIRECT_COMPLAINT_TERMS = (
    "别发了",
    "不要再发",
    "停止发送",
    "stop posting",
)
LOW_CONFIDENCE_COMPLAINT_TERMS = ("刷屏", "打扰", "spam")
WARNING_TERMS = ("警告", "禁言", "移出群", "违反群规", "管理员提醒")
DIRECT_TARGET_PREFIXES = (
    "你",
    "你们",
    "这个账号",
    "这个号",
    "别刷",
    "不要刷",
)


@dataclass(frozen=True)
class SafetyDecision:
    should_pause: bool
    reason: str = ""
    severe: bool = False
    should_audit: bool = False


class SafetyGuard:
    """Conservative complaint/admin-warning detector with auditable reasons."""

    def inspect(self, messages: tuple[MessageSnapshot, ...]) -> SafetyDecision:
        for message in messages:
            normalized = message.text.casefold()
            if message.sender_is_admin and any(term in normalized for term in WARNING_TERMS):
                return SafetyDecision(True, "administrator warning", True)
            if any(term in normalized for term in DIRECT_COMPLAINT_TERMS):
                return SafetyDecision(True, "explicit complaint", True)
            if any(term in normalized for term in LOW_CONFIDENCE_COMPLAINT_TERMS):
                targeted = message.reply_to_is_self or normalized.startswith(
                    DIRECT_TARGET_PREFIXES
                )
                if targeted:
                    return SafetyDecision(True, "explicit complaint", True)
                return SafetyDecision(False, "possible complaint", False, True)
        return SafetyDecision(False)
