# skills/assistant_settings.py
# Каркасный навык: открытие настроек и голосовое переключение характера.

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


def _detect_persona_switch(text: str) -> tuple[str, str] | None:
    clean = text.lower().strip()

    # Брутальный / без цензуры
    if (
        re.search(
            r"\b(включи|вруби|поставь|смени|переключи|активируй|выбери|сделай)\b.*?\b(брутальн\w*|без\s+цензур\w*|матерн\w*|мат\b)",
            clean,
        )
        or re.search(r"\b(брутальн\w+|без\s+цензур\w+|матерн\w+)\s+(режим|характер|стиль|ассистент|помощник)\b", clean)
        or re.search(r"\bбудь\s+(?:предельно\s+)?брутальн\w+\b", clean)
    ):
        return "brutal", "Базара ноль, врубил брутальный режим без цензуры."

    # Саркастичный
    if (
        re.search(
            r"\b(включи|поставь|смени|переключи|активируй|выбери|сделай)\b.*?\b(саркастичн\w*|ироничн\w*|сарказм\w*)",
            clean,
        )
        or re.search(r"\b(саркастичн\w+|ироничн\w+|сарказм\w*)\s+(режим|характер|стиль|ассистент|помощник)\b", clean)
        or re.search(r"\bбудь\s+(?:более\s+)?саркастичн\w+\b", clean)
    ):
        return "sarcastic", "О, наконец-то можно перестать притворяться пай-мальчиком. Саркастичный режим включён."

    # Свой парень (Бро)
    if (
        re.search(
            r"\b(включи|поставь|смени|переключи|активируй|выбери|сделай)\b.*?\b(режим\s+бро|режим\s+свой\s+парень|характер\s+бро|стиль\s+бро|свой\s+парень|бро\b)",
            clean,
        )
        or re.search(r"\b(режим|характер|стиль)\s+(бро|свой\s+парень)\b", clean)
        or re.search(r"\bбудь\s+(?:как\s+)?(бро|свой\s+парень)\b", clean)
    ):
        return "buddy", "Без проблем, бро, теперь общаемся по-свойски."

    # Классический Джарвис
    if (
        re.search(
            r"\b(включи|верни|поставь|смени|переключи|активируй|выбери|сделай)\b.*?\b(классическ\w*|обычн\w*|стандартн\w*|дефолтн\w*|джарвис\w*)",
            clean,
        )
        or re.search(r"\b(классическ\w+|обычн\w+|стандартн\w+|дефолтн\w+)\s+(режим|характер|стиль|ассистент|помощник)\b", clean)
        or re.search(r"\bверни\s+джарвиса\b", clean)
    ):
        return "jarvis", "Вернул классический характер Джарвиса."

    return None


class AssistantSettingsSkill(BaseSkill):
    """Открывает окно настроек Джарвиса или переключает характер ассистента голосом."""

    def can_handle(self, context: RequestContext) -> bool:
        text = context.raw_text.lower().strip()
        if any(marker in text for marker in _SYSTEMISH):
            return False
        if _detect_persona_switch(text) is not None:
            return True
        if any(phrase in text for phrase in _NAMED):
            return True
        if re.search(r"\b(открой|открыть|покажи)\s+настройки\b", text):
            rest = re.sub(r".*?\bнастройки\b", "", text).strip()
            return not rest or rest in {"пожалуйста", "ассистента", "джарвиса"}
        return False

    def execute(self, context: RequestContext) -> None:
        text = context.raw_text.lower().strip()
        switch = _detect_persona_switch(text)
        if switch is not None:
            preset_id, reply = switch
            from skill_settings import set_persona_preset

            set_persona_preset(preset_id)
            context.speak(reply)
            return

        request_open_settings()
        context.speak("Открываю.")
