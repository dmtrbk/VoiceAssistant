# skills/assistant_settings.py
# Каркасный навык: в окне тумблеров не показывается.

import re

from skill_settings import request_open_settings
from skills.base import BaseSkill, RequestContext

_NAMED = (
    "настройки ассистента",
    "настройки джарвиса",
    "параметры ассистента",
    "параметры джарвиса",
    "окно настроек",
)
_SYSTEMISH = (
    "системн",
    "gnome",
    "компьютера",
    "сети",
    "экрана",
    "звука",
    "монитора",
)


class AssistantSettingsSkill(BaseSkill):
    """Открывает окно настроек Джарвиса, не системные параметры GNOME."""

    def can_handle(self, context: RequestContext) -> bool:
        text = context.raw_text.lower().strip()
        if any(marker in text for marker in _SYSTEMISH):
            return False
        if any(phrase in text for phrase in _NAMED):
            return True
        if re.search(r"\b(открой|открыть|покажи)\s+настройки\b", text):
            rest = re.sub(r".*?\bнастройки\b", "", text).strip()
            return not rest or rest in {"пожалуйста", "ассистента", "джарвиса"}
        return False

    def execute(self, context: RequestContext) -> None:
        request_open_settings()
        context.speak("Открываю настройки.")
