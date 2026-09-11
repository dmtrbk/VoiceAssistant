# context_manager.py

import logging
import threading
import time
from typing import Callable, Optional

logger = logging.getLogger(__name__)

EXIT_EXACT = {
    "хватит", "стоп", "выход", "отмена", "прекрати",
    "закончить", "выйти", "выйди", "сдаюсь",
}
EXIT_PHRASES = ("хватит играть", "я сдаюсь", "закончить игру")

_ctx_lock = threading.RLock()
_active_context: Optional["DialogContext"] = None


class DialogContext:
    """Интерактивный контекст диалога (для игр, пошаговых опросов, подтверждений)."""

    def __init__(
        self,
        name: str,
        handler: Callable[[str, Callable[[str], None]], bool],
        timeout_sec: float = 45.0,
        on_exit: Optional[Callable[[Callable[[str], None]], None]] = None
    ):
        self.name = name
        self.handler = handler
        self.timeout_sec = timeout_sec
        self.on_exit = on_exit
        self.last_active_time = time.time()

    def is_expired(self) -> bool:
        return (time.time() - self.last_active_time) > self.timeout_sec

    def touch(self) -> None:
        self.last_active_time = time.time()


def _call_on_exit(ctx: DialogContext, speak_callback: Optional[Callable[[str], None]]) -> None:
    if not ctx.on_exit or speak_callback is None:
        return
    try:
        ctx.on_exit(speak_callback)
    except Exception as exc:
        logger.error("[Контекст] Ошибка в on_exit для '%s': %s", ctx.name, exc)


def _expire_if_needed() -> Optional[DialogContext]:
    global _active_context
    if _active_context is not None and _active_context.is_expired():
        logger.info("[Контекст] Контекст '%s' истёк по таймауту.", _active_context.name)
        _active_context = None
    return _active_context


def set_active_context(
    name: str,
    handler: Callable[[str, Callable[[str], None]], bool],
    timeout_sec: float = 45.0,
    on_exit: Optional[Callable[[Callable[[str], None]], None]] = None
) -> None:
    """Устанавливает активный контекст диалога. Предыдущий закрывается без озвучки."""
    global _active_context
    new_ctx = DialogContext(
        name=name,
        handler=handler,
        timeout_sec=timeout_sec,
        on_exit=on_exit,
    )
    with _ctx_lock:
        old = _active_context
        _active_context = new_ctx
        logger.info("[Контекст] Установлен интерактивный контекст: '%s' (таймаут %sс)", name, timeout_sec)
    if old is not None:
        logger.info("[Контекст] Заменён контекст: '%s'", old.name)
        _call_on_exit(old, lambda _text: None)


def clear_active_context(call_on_exit: bool = False, speak_callback: Optional[Callable[[str], None]] = None) -> None:
    """Сбрасывает текущий контекст диалога."""
    global _active_context
    with _ctx_lock:
        ctx = _active_context
        _active_context = None
    if ctx is None:
        return
    logger.info("[Контекст] Завершён контекст: '%s'", ctx.name)
    if call_on_exit:
        _call_on_exit(ctx, speak_callback)


def get_active_context() -> Optional[DialogContext]:
    """Возвращает текущий активный контекст, если он не истёк по таймауту."""
    with _ctx_lock:
        return _expire_if_needed()


def is_in_context() -> bool:
    """Проверяет, находится ли ассистент в интерактивном контексте."""
    return get_active_context() is not None


def _is_context_exit(text: str) -> bool:
    if text in EXIT_EXACT:
        return True
    return any(phrase in text for phrase in EXIT_PHRASES)


def handle_context_input(text: str, speak_callback: Callable[[str], None]) -> tuple[bool, bool]:
    """
    Пытается обработать входящий текст в рамках активного контекста.
    Возвращает (handled: bool, should_sleep: bool).
    """
    global _active_context
    clean_text = text.lower().strip()
    with _ctx_lock:
        ctx = _expire_if_needed()
        if ctx is None:
            return False, False
        if _is_context_exit(clean_text):
            _active_context = None
            exiting = ctx
        else:
            ctx.touch()
            exiting = None

    if exiting is not None:
        logger.info("[Контекст] Завершён контекст: '%s'", exiting.name)
        _call_on_exit(exiting, speak_callback)
        return True, False

    try:
        should_close = ctx.handler(clean_text, speak_callback)
        if should_close:
            with _ctx_lock:
                if _active_context is ctx:
                    _active_context = None
                    logger.info("[Контекст] Завершён контекст: '%s'", ctx.name)
        return True, False
    except Exception as exc:
        logger.error("[Контекст] Ошибка обработки контекста '%s': %s", ctx.name, exc)
        with _ctx_lock:
            if _active_context is ctx:
                _active_context = None
        speak_callback("Произошла ошибка при обработке контекста. Возвращаюсь в обычный режим.")
        return True, False
