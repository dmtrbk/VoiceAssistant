# Ремонт диалога после сбоя понимания.
# Пауза «думаю» — не срыв. «что?» / «а?» — повтор последней мысли, не новый запрос.
# Каша STT — уточнить недостающее, не «не понял, перефразируй». На третьем сбое — отпустить сессию.

from __future__ import annotations

import random
import threading

from triggers import FILLER_PHRASES, normalize_utterance

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
    "object": ("Что именно?", "Что сделать?"),
    "include": ("Что включить?", "Включить что?"),
    "disable": ("Что выключить?", "Выключить что?"),
    "open": ("Что открыть?", "Открыть что?"),
    "close": ("Что закрыть?", "Закрыть что?"),
    "find": ("Что найти?", "Что искать?"),
    "draw": ("Что нарисовать?",),
    "say": ("Про что?", "О чём рассказать?"),
    "put": ("Что поставить?", "На сколько?"),
}

# Голый глагол без объекта — не угадывать и не слать в Groq.
BARE_ACTIONS = {
    "включи": "include",
    "выключи": "disable",
    "открой": "open",
    "закрой": "close",
    "найди": "find",
    "запусти": "include",
    "поставь": "put",
    "нарисуй": "draw",
    "скажи": "say",
    "расскажи": "say",
    "покажи": "find",
}

_YES_EXACT = frozenset({"да", "ага", "угу", "давай", "хорошо", "ок", "окей", "yes"})
_NO_EXACT = frozenset({"нет", "не надо", "отмена", "не", "no"})

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
_pending_prefix = ""
_pending_confirm = ""


def reset(*, keep_replayable: bool = False, keep_pending: bool = False) -> None:
    global _repair_count, _last_replayable, _close_spoken, _pending_prefix, _pending_confirm
    with _lock:
        _repair_count = 0
        _close_spoken = False
        if not keep_pending:
            _pending_prefix = ""
            _pending_confirm = ""
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
    stripped = text.strip()
    with _lock:
        if stripped != _last_replayable:
            _last_replayable = stripped
        _repair_count = 0
        _close_spoken = False


def current_replayable() -> str:
    with _lock:
        return _last_replayable


def take_no_match() -> tuple[str, bool]:
    """Реплика ремонта и нужно ли отпустить сессию. Один захват замка."""
    global _repair_count, _close_spoken
    with _lock:
        if _repair_count >= MAX_REPAIR_TURNS:
            _close_spoken = True
            return random.choice(REPAIR_CLOSE), True
        _repair_count += 1
        if _repair_count == 1:
            return random.choice(REPAIR_FIRST), False
        if _repair_count == 2:
            return random.choice(REPAIR_SECOND), False
        _close_spoken = True
        return random.choice(REPAIR_CLOSE), True


def next_no_match_line() -> str:
    """Следующая реплика ремонта. После лимита — закрытие, дальше та же закрывающая."""
    line, _release = take_no_match()
    return line


def should_release_session() -> bool:
    with _lock:
        return _close_spoken and _repair_count >= MAX_REPAIR_TURNS


def has_pending() -> bool:
    with _lock:
        return bool(_pending_prefix or _pending_confirm)


def drop_pending() -> None:
    global _pending_prefix, _pending_confirm
    with _lock:
        _pending_prefix = ""
        _pending_confirm = ""


def is_bare_action(text: str) -> bool:
    return normalize_utterance(text) in BARE_ACTIONS


def ask_bare_action(text: str) -> str:
    kind = BARE_ACTIONS.get(normalize_utterance(text), "object")
    return slot_clarify(kind)


def set_pending_prefix(prefix: str) -> None:
    global _pending_prefix, _pending_confirm
    with _lock:
        _pending_prefix = normalize_utterance(prefix)
        _pending_confirm = ""


def set_pending_confirm(candidate: str) -> str:
    """«You mean X?» — повторяем только спорный кусок."""
    global _pending_confirm, _pending_prefix
    clean = (candidate or "").strip()
    with _lock:
        _pending_confirm = clean
        _pending_prefix = ""
    short = clean
    if len(short) > 40:
        short = short[:37].rsplit(" ", 1)[0] + "…"
    return f"Ты про {short}?"


def is_yes(text: str) -> bool:
    return normalize_utterance(text) in _YES_EXACT


def is_no(text: str) -> bool:
    return normalize_utterance(text) in _NO_EXACT


def take_pending_rewrite(text: str) -> str | None:
    """
    Если ждём объект после «включи» — склеить. Если ждём да/нет на «ты про X?» — вернуть кандидата.
    None — pending нет или его надо бросить (новая полная команда разберёт маршрутизатор).
    """
    global _pending_prefix, _pending_confirm
    lowered = normalize_utterance(text)
    if not lowered:
        return None
    with _lock:
        prefix = _pending_prefix
        confirm = _pending_confirm
        if confirm:
            if lowered in _YES_EXACT:
                _pending_confirm = ""
                return confirm
            if lowered in _NO_EXACT:
                _pending_confirm = ""
                return ""
            _pending_confirm = ""
            return None
        if not prefix:
            return None
        if lowered in BARE_ACTIONS:
            _pending_prefix = lowered
            return None
        if lowered.startswith(prefix + " "):
            _pending_prefix = ""
            return lowered
        _pending_prefix = ""
        return f"{prefix} {lowered}"


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
    if lowered in THINKING_EXACT:
        # «угу» — и пауза, и «да». Если ждём подтверждение, не глушить.
        if lowered in _YES_EXACT:
            with _lock:
                if _pending_confirm:
                    return None
        return ("silent", None)
    if lowered in OIR_EXACT:
        replay = current_replayable()
        if replay:
            return ("speak", replay)
        if lowered == "а":
            return ("silent", None)
        return ("speak", random.choice(LISTEN_ACKS))
    if lowered in FILLER_PHRASES or len(lowered) < 2:
        return ("silent", None)
    return None
