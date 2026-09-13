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
    "так",
    "так-так",
    "секунду",
    "секундочку",
    "минутку",
    "дай подумать",
    "погоди",
    "сейчас",
    "щас",
})

# Фразы возобновления речи после паузы / перебивания (Full-Duplex Resumption).
RESUME_EXACT = frozenset({
    "продолжай",
    "продолжи",
    "договори",
    "дальше",
    "продолжай говорить",
    "на чем мы остановились",
    "на чем остановились",
    "что дальше",
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
_last_interrupted = ""
_close_spoken = False
_pending_prefix = ""
_pending_confirm = ""
_pending_choice_prompt = ""
_pending_choices: dict[str, str] = {}


def reset(*, keep_replayable: bool = False, keep_pending: bool = False, keep_interrupted: bool = False) -> None:
    global _repair_count, _last_replayable, _close_spoken, _pending_prefix, _pending_confirm
    global _pending_choice_prompt, _pending_choices, _last_interrupted
    with _lock:
        _repair_count = 0
        _close_spoken = False
        if not keep_pending:
            _pending_prefix = ""
            _pending_confirm = ""
            _pending_choice_prompt = ""
            _pending_choices = {}
        if not keep_replayable:
            _last_replayable = ""
        if not keep_interrupted:
            _last_interrupted = ""


def repair_count() -> int:
    with _lock:
        return _repair_count


def is_thinking_pause(text: str) -> bool:
    return normalize_utterance(text) in THINKING_EXACT


def is_oir_phrase(text: str) -> bool:
    return normalize_utterance(text) in OIR_EXACT


def is_resume_phrase(text: str) -> bool:
    return normalize_utterance(text) in RESUME_EXACT


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


def remember_interrupted(text: str) -> None:
    """Запоминает недочитанный фрагмент речи при перебивании ('замолчи' / barge-in)."""
    global _last_interrupted
    stripped = (text or "").strip()
    if not stripped or not is_replayable(stripped):
        return
    with _lock:
        _last_interrupted = stripped


def current_interrupted() -> str:
    with _lock:
        return _last_interrupted


def clear_interrupted() -> None:
    global _last_interrupted
    with _lock:
        _last_interrupted = ""


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
        return bool(_pending_prefix or _pending_confirm or _pending_choices)


def drop_pending() -> None:
    global _pending_prefix, _pending_confirm, _pending_choice_prompt, _pending_choices
    with _lock:
        _pending_prefix = ""
        _pending_confirm = ""
        _pending_choice_prompt = ""
        _pending_choices = {}


def is_bare_action(text: str) -> bool:
    return normalize_utterance(text) in BARE_ACTIONS


def ask_bare_action(text: str) -> str:
    kind = BARE_ACTIONS.get(normalize_utterance(text), "object")
    return slot_clarify(kind)


def set_pending_prefix(prefix: str) -> None:
    global _pending_prefix, _pending_confirm, _pending_choice_prompt, _pending_choices
    with _lock:
        _pending_prefix = normalize_utterance(prefix)
        _pending_confirm = ""
        _pending_choice_prompt = ""
        _pending_choices = {}


def set_pending_confirm(candidate: str) -> str:
    """«You mean X?» — повторяем только спорный кусок."""
    global _pending_confirm, _pending_prefix, _pending_choice_prompt, _pending_choices
    clean = (candidate or "").strip()
    with _lock:
        _pending_confirm = clean
        _pending_prefix = ""
        _pending_choice_prompt = ""
        _pending_choices = {}
    short = clean
    if len(short) > 40:
        short = short[:37].rsplit(" ", 1)[0] + "…"
    return f"Ты про {short}?"


def set_pending_choice(prompt: str, choices: dict[str, str]) -> str:
    """Уточнение с выбором варианта («Включить фильм или песню?»)."""
    global _pending_choice_prompt, _pending_choices, _pending_prefix, _pending_confirm
    with _lock:
        _pending_choice_prompt = prompt
        _pending_choices = {normalize_utterance(k): v for k, v in choices.items()}
        _pending_prefix = ""
        _pending_confirm = ""
    return prompt


def is_yes(text: str) -> bool:
    return normalize_utterance(text) in _YES_EXACT


def is_no(text: str) -> bool:
    return normalize_utterance(text) in _NO_EXACT


def take_pending_rewrite(text: str) -> str | None:
    """
    Если ждём объект после «включи» — склеить.
    Если ждём выбор («фильм или песню») — вернуть выбранный вариант.
    Если ждём да/нет на «ты про X?» — вернуть кандидата.
    None — pending нет или его надо бросить (новая полная команда разберёт маршрутизатор).
    """
    global _pending_prefix, _pending_confirm, _pending_choice_prompt, _pending_choices
    lowered = normalize_utterance(text)
    if not lowered:
        return None
    with _lock:
        if _pending_choices:
            if lowered in _NO_EXACT:
                _pending_choices = {}
                _pending_choice_prompt = ""
                return ""
            words = lowered.split()
            for key, target_cmd in _pending_choices.items():
                if key == lowered or key in words or (len(key) >= 4 and key in lowered):
                    _pending_choices = {}
                    _pending_choice_prompt = ""
                    return target_cmd
            _pending_choices = {}
            _pending_choice_prompt = ""
            return None

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
    ('silent', None) — слушать дальше (пауза обдумывания).
    ('speak', фраза) — сказать и не маршрутизировать (повтор, продолжение, подтверждение).
    None — обычный разбор.
    """
    lowered = normalize_utterance(text)
    if not lowered:
        return ("silent", None)
    if lowered in THINKING_EXACT:
        # «угу» — и пауза, и «да». Если ждём подтверждение или выбор, не глушить.
        if lowered in _YES_EXACT:
            with _lock:
                if _pending_confirm or _pending_choices:
                    return None
        return ("silent", None)
    if lowered in RESUME_EXACT:
        interrupted = current_interrupted()
        if interrupted:
            clear_interrupted()
            return ("speak", interrupted)
        replay = current_replayable()
        if replay:
            return ("speak", f"Мы говорили: {replay}")
        return ("speak", "Слушаю, о чём продолжить?")
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
