from __future__ import annotations

from dataclasses import dataclass

from ui.message_manager import MessageManager


@dataclass(frozen=True)
class GenerationResult:
    text: str | None
    source: str | None
    reason: str = ""


class MessageGenerator:
    """AI-first message generation with validated TXT fallback."""

    def __init__(self, ai_sender: object | None, message_manager: MessageManager) -> None:
        self._ai_sender = ai_sender
        self._message_manager = message_manager
        self._recent: dict[str, list[str]] = {}

    def _validate(self, group: str, text: str | None) -> GenerationResult:
        normalized = (text or "").strip()
        if not normalized:
            return GenerationResult(None, None, "empty")
        if normalized.casefold() in {"skip", "[skip]"}:
            return GenerationResult(None, None, "not_relevant")
        if len(normalized) > 4000:
            return GenerationResult(None, None, "too_long")
        history = self._recent.setdefault(group, [])
        if normalized in history:
            return GenerationResult(None, None, "duplicate")
        return GenerationResult(normalized, None)

    def commit(self, group: str, text: str, source: str) -> None:
        """Record history only after Telegram send and quota confirmation succeed."""
        normalized = text.strip()
        if not normalized:
            return
        history = self._recent.setdefault(group, [])
        history.append(normalized)
        del history[:-20]
        if source == "ai" and self._ai_sender is not None:
            commit_sent = getattr(self._ai_sender, "commit_sent", None)
            if callable(commit_sent):
                commit_sent(group, normalized)

    async def generate(
        self,
        group: str,
        prompt: str,
        context_count: int,
        ai_enabled: bool,
    ) -> GenerationResult:
        if ai_enabled and self._ai_sender is not None:
            try:
                text = await self._ai_sender.generate_message(group, prompt, context_count)
                validated = self._validate(group, text)
                if validated.text is not None:
                    return GenerationResult(validated.text, "ai")
                if validated.reason in {"not_relevant", "duplicate", "too_long"}:
                    return validated
            except Exception:
                pass
        try:
            text = self._message_manager.get_message(group)
        except Exception as exc:
            return GenerationResult(None, None, f"fallback_failed:{type(exc).__name__}")
        validated = self._validate(group, text)
        if validated.text is None:
            return validated
        return GenerationResult(validated.text, "txt")
