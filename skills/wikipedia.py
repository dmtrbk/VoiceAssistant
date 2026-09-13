# skills/wikipedia.py

import logging
import re

from skills.base import BaseSkill, RequestContext

logger = logging.getLogger(__name__)

_TRIGGERS = ("википедия", "что такое", "кто такой", "кто такая")
_IDENTITY = ("кто ты", "кто вы", "ты кто", "как тебя зовут")


def _wiki_client():
    import wikipediaapi
    return wikipediaapi.Wikipedia(user_agent="VoiceAssistantBot/1.0", language="ru")


def is_wiki_command(text: str) -> bool:
    lowered = str(text or "").lower().strip()
    if any(phrase in lowered for phrase in _IDENTITY):
        return False
    return any(trigger in lowered for trigger in _TRIGGERS)


def extract_wiki_query(text: str) -> str:
    lowered = str(text or "").lower().strip()
    for trigger in _TRIGGERS:
        if trigger in lowered:
            return lowered.split(trigger, 1)[-1].strip()
    return ""


class WikipediaSkill(BaseSkill):
    """Краткая справка из русской Википедии."""

    def can_handle(self, context: RequestContext) -> bool:
        return is_wiki_command(context.raw_text)

    def execute(self, context: RequestContext) -> None:
        speak = context.speak
        if speak is None:
            return

        query = extract_wiki_query(context.raw_text)
        if not query:
            speak("Что найти?")
            return

        speak("Ищу.")
        try:
            wiki = _wiki_client()
            page = wiki.page(query)
            if page.exists():
                sentences = re.split(r"(?<=[.!?])\s+", page.summary)
                speak(" ".join(sentences[:2]))
                return
            speak("Не нашёл.")
        except Exception as exc:
            logger.error("[Википедия] Ошибка: %s", exc)
            speak("Не нашёл.")
