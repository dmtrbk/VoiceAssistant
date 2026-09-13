# Ремонт диалога после сбоя понимания.
# Пауза «думаю» — не срыв. «что?» / «а?» — повтор последней мысли, не новый запрос.
# Каша STT — уточнить недостающее, не «не понял, перефразируй». На третьем сбое — отпустить сессию.

from __future__ import annotations

import random
import threading

from triggers import is_filler, normalize_utterance

MAX_REPAIR_TURNS = 3

# Точные фразы other-initiated repair. Не класть «повтори» / «ещё раз» — это follow-up навыков.
OIR_EXACT = frozenset({
    "что",
    "а",
    "как",
    "huh",
    "what",
    "pardon",
    "не понял",
    "не поняла",
    "что сказал",
    "что ты сказал",
    "а что",
})

# Человек набирает мысль. Молчим и слушаем (Speak / GPT-Live: не влезать в паузу).
THINKING_EXACT = frozenset({
    "м",
    "мм",
    "эм",
    "э",
    "ну",
    "угу",
    "ээ",
    "м-м",
    "э-э",
})

LISTEN_ACKS = (
    "Слушаю.",
    "Да?",
    "На связи.",
)

REPAIR_FIRST = (
    "Ещё раз?",
    "Повтори?",
    "Не уловил.",
)

REPAIR_SECOND = (
    "Какую команду?",
    "Что сделать?",
    "Повтори чуть громче.",
)

REPAIR_CLOSE = (
    "Если что — позови.",
    "На связи, позови.",
    "Ладно, позови.",
)

SLOT_CLARIFY = {
    "destination": ("Куда?", "Куда ехать?", "В какое место?"),
}

_NON_REPLAYABLE = frozenset(
    normalize_utterance(phrase)
    for phrase in (
        *LISTEN_ACKS,
        *REPAIR_FIRST,
        *REPAIR_SECOND,
        *REPAIR_CLOSE,
        "Здесь.",
        "Не расслышал.",
        "Не понял.",
        *(phrase for phrases in SLOT_CLARIFY.values() for phrase in phrases),
    )
)

_lock = threading.Lock()
_repair_count = 0
_last_replayable = ""
_close_spoken = False


def reset(*, keep_replayable: bool = False) -> None:
    global _repair_count, _last_replayable, _close_spoken
    with _lock:
        _repair_count = 0
        _close_spoken = False
        if not keep_replayable:
            _last_replayable = ""


def repair_count() -> int:
    with _lock:
        return _repair_count


def is_thinking_pause(text: str) -> bool:
    return normalize_utterance(text) in THINKING_EXACT


def is_oir_phrase(text: str) -> bool:
    return normalize_utterance(text) in OIR_EXACT


def is_replayable(text: str) -> bool:
    lowered = normalize_utterance(text)
    return bool(lowered) and lowered not in _NON_REPLAYABLE


def remember_spoken(text: str) -> None:
    """Запоминает последнюю содержательную реплику. Служебные и уточнения не пишем."""
    global _last_replayable, _repair_count, _close_spoken
    if not text or not is_replayable(text):
        return
    with _lock:
        _last_replayable = text.strip()
        _repair_count = 0
        _close_spoken = False


def current_replayable() -> str:
    with _lock:
        return _last_replayable


def next_no_match_line() -> str:
    """Следующая реплика ремонта. После лимита — закрытие, дальше та же закрывающая."""
    global _repair_count, _close_spoken
    with _lock:
        if _repair_count >= MAX_REPAIR_TURNS:
            _close_spoken = True
            return random.choice(REPAIR_CLOSE)
        _repair_count += 1
        if _repair_count == 1:
            return random.choice(REPAIR_FIRST)
        if _repair_count == 2:
            return random.choice(REPAIR_SECOND)
        _close_spoken = True
        return random.choice(REPAIR_CLOSE)


def should_release_session() -> bool:
    with _lock:
        return _close_spoken and _repair_count >= MAX_REPAIR_TURNS


def slot_clarify(kind: str) -> str:
    """Уточнить только недостающий слот, без «перефразируй»."""
    options = SLOT_CLARIFY.get(kind)
    if not options:
        return random.choice(REPAIR_FIRST)
    return random.choice(options)


def early_dialogue_turn(text: str) -> tuple[str, str | None] | None:
    """
    Ход до навыков.
    ('silent', None) — слушать дальше.
    ('speak', фраза) — сказать и не маршрутизировать.
    None — обычный разбор.
    """
    lowered = normalize_utterance(text)
    if not lowered:
        return ("silent", None)
    if is_thinking_pause(lowered):
        return ("silent", None)
    if is_oir_phrase(lowered):
        replay = current_replayable()
        if replay:
            return ("speak", replay)
        if lowered == "а":
            return ("silent", None)
        return ("speak", random.choice(LISTEN_ACKS))
    if is_filler(text):
        return ("silent", None)
    return None
