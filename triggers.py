# Общие голосовые триггеры, чтобы assistant и навыки не расходились.
#
# Таксономия фраз (у «тишина» ровно одно значение — стоп музыки):
#   emergency  «стоп»              — полная авария: TTS, медиа, сессия
#   hold       «замолчи» и др.     — стоп TTS, сессия жива, музыка приглушена
#   sleep      «спать» / «отбой»   — сессия в idle, громкость плеера назад
#   media stop «тишина» и др.      — навык Audacious, не путать с hold

import re

QUICK_TRIGGERS = [
    "громче",
    "тише",
    "громкость плюс",
    "громкость минус",
    "следующий",
    "предыдущий",
    "вперед",
    "назад",
    "дальше",
    "прошлый трек",
    "следующий трек",
    "трек",
    "пауза",
    "плей",
    "играй",
    "возобнови",
    "выключи музыку",
    "выруби музыку",
    "останови музыку",
    "тишина",
]

# Полная авария. Не класть сюда «замолчи» — это hold, сессия должна жить.
EMERGENCY_TRIGGERS = ["стоп"]

# Прервать озвучку, остаться в сессии. «тишина» сюда не входит.
HOLD_TRIGGERS = ["замолчи", "подожди", "хватит говорить"]

# Уснуть. Не использовать голое «выключись» — путается с «выключи» у Audacious/System.
SLEEP_TRIGGERS = ["спать", "отбой", "все хватит"]

MUSIC_VOLUME_HINTS = [
    "музыка", "музыку", "музыки", "плеер", "плеера", "плеере",
    "трек", "трека", "песн", "радио",
]

_COMPOUND_SPLIT = re.compile(r"\s+(?:и|а также|потом|затем)\s+")

MEDIA_CONTROL_HINTS = [
    "следующий", "предыдущий", "вперед", "назад", "дальше",
    "трек", "пауза", "плей", "играй", "возобнови", "тишина",
]

FILLER_PHRASES = {
    "а",
    "м",
    "мм",
    "эм",
    "э",
    "ну",
    "угу",
    "ээ",
    "м-м",
    "э-э",
}


def normalize_utterance(text: str) -> str:
    """Нижний регистр, ё→е, без пунктуации (дефис в «м-м» сохраняем)."""
    lowered = (text or "").lower().replace("ё", "е")
    cleaned = re.sub(r"[^\w\s-]", " ", lowered, flags=re.UNICODE)
    return re.sub(r"\s+", " ", cleaned).strip()


def _contains_any(text: str, phrases) -> bool:
    lowered = normalize_utterance(text)
    if not lowered:
        return False
    return any(phrase in lowered for phrase in phrases)


def is_quick_command(text: str) -> bool:
    return _contains_any(text, QUICK_TRIGGERS)


def is_emergency_stop(text: str) -> bool:
    return _contains_any(text, EMERGENCY_TRIGGERS)


def is_hold_interrupt(text: str) -> bool:
    return _contains_any(text, HOLD_TRIGGERS)


def is_sleep_command(text: str) -> bool:
    return _contains_any(text, SLEEP_TRIGGERS)


def is_stop_command(text: str) -> bool:
    """Совместимость: только аварийный «стоп», без «замолчи»."""
    return is_emergency_stop(text)


def is_music_volume_command(text: str) -> bool:
    lowered = normalize_utterance(text)
    has_volume = any(word in lowered for word in ["громче", "тише", "громкость"])
    has_music = any(word in lowered for word in MUSIC_VOLUME_HINTS)
    return has_volume and has_music


def is_bare_volume_command(text: str) -> bool:
    lowered = normalize_utterance(text)
    has_volume = any(word in lowered for word in ["громче", "тише", "громкость"])
    return has_volume and not is_music_volume_command(lowered)


def is_media_control_command(text: str) -> bool:
    return _contains_any(text, MEDIA_CONTROL_HINTS)


def split_quick_compound(text: str) -> list[str]:
    """
    Делит «следующий трек и сделай громче» на отдельные быстрые команды.
    Если в связке есть управление треком и голое «громче», громкость относится к плееру.
    """
    lowered = normalize_utterance(text)
    parts = [part.strip() for part in _COMPOUND_SPLIT.split(lowered) if part.strip()]
    if len(parts) < 2 or not all(is_quick_command(part) for part in parts):
        return [lowered]

    if any(is_media_control_command(part) for part in parts):
        parts = [
            f"{part} музыку" if is_bare_volume_command(part) else part
            for part in parts
        ]
    return parts


def is_filler(text: str) -> bool:
    """Пусто, один символ или короткое междометие — не отправлять в навыки/Groq."""
    lowered = normalize_utterance(text)
    if not lowered:
        return True
    if len(lowered) < 2:
        return True
    return lowered in FILLER_PHRASES
