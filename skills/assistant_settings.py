# skills/assistant_settings.py
# Каркасный навык: в окне тумблеров не показывается.

from skill_settings import request_open_settings
from skills.base import BaseSkill, RequestContext


class AssistantSettingsSkill(BaseSkill):
    """Открывает окно настроек Джарвиса, не системные параметры GNOME."""

    def can_handle(self, context: RequestContext) -> bool:
        text = context.raw_text.lower().strip()
        return any(
            phrase in text
            for phrase in (
                "настройки ассистента",
                "настройки джарвиса",
                "параметры ассистента",
                "параметры джарвиса",
                "окно настроек",
            )
        )

    def execute(self, context: RequestContext) -> None:
        request_open_settings()
        context.speak("Открываю настройки.")
