# context_manager.py

import time
import logging
from typing import Callable, Optional

logger = logging.getLogger(__name__)

EXIT_COMMANDS = {
    "хватит", "стоп", "выход", "отмена", "отбой", "закрой",
    "прекрати", "закончить", "выйти", "выйди", "хватит играть", "сдаюсь"
}


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


_active_context: Optional[DialogContext] = None


def set_active_context(
    name: str,
    handler: Callable[[str, Callable[[str], None]], bool],
    timeout_sec: float = 45.0,
    on_exit: Optional[Callable[[Callable[[str], None]], None]] = None
) -> None:
    """Устанавливает активный контекст диалога."""
    global _active_context
    logger.info(f"[Контекст] Установлен интерактивный контекст: '{name}' (таймаут {timeout_sec}с)")
    _active_context = DialogContext(
        name=name,
        handler=handler,
        timeout_sec=timeout_sec,
        on_exit=on_exit
    )


def clear_active_context(call_on_exit: bool = False, speak_callback: Optional[Callable[[str], None]] = None) -> None:
    """Сбрасывает текущий контекст диалога."""
    global _active_context
    if _active_context is not None:
        logger.info(f"[Контекст] Завершён контекст: '{_active_context.name}'")
        if call_on_exit and _active_context.on_exit and speak_callback:
            try:
                _active_context.on_exit(speak_callback)
            except Exception as e:
                logger.error(f"[Контекст] Ошибка в on_exit для '{_active_context.name}': {e}")
        _active_context = None


def get_active_context() -> Optional[DialogContext]:
    """Возвращает текущий активный контекст, если он не истёк по таймауту."""
    global _active_context
    if _active_context is not None:
        if _active_context.is_expired():
            logger.info(f"[Контекст] Контекст '{_active_context.name}' истёк по таймауту.")
            _active_context = None
    return _active_context


def is_in_context() -> bool:
    """Проверяет, находится ли ассистент в интерактивном контексте."""
    return get_active_context() is not None


def handle_context_input(text: str, speak_callback: Callable[[str], None]) -> tuple[bool, bool]:
    """
    Пытается обработать входящий текст в рамках активного контекста.
    Возвращает (handled: bool, should_sleep: bool).
    """
    ctx = get_active_context()
    if ctx is None:
        return False, False

    clean_text = text.lower().strip()

    # Проверка выхода из контекста
    if any(cmd in clean_text.split() or clean_text == cmd for cmd in EXIT_COMMANDS):
        clear_active_context(call_on_exit=True, speak_callback=speak_callback)
        return True, False

    ctx.touch()
    try:
        should_close = ctx.handler(clean_text, speak_callback)
        if should_close:
            clear_active_context()
        return True, False
    except Exception as e:
        logger.error(f"[Контекст] Ошибка обработки контекста '{ctx.name}': {e}")
        clear_active_context()
        speak_callback("Произошла ошибка при обработке контекста. Возвращаюсь в обычный режим.")
        return True, False
