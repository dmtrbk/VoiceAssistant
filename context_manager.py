# context_manager.py
# Управление состоянием диалога (State Management & FSM).
# Поддерживает:
# 1. Стек состояний (FSM Stack): вложенные контексты и переходы по шагам.
# 2. Сессионное состояние (Session State): хранение переменных в рамках сессии (Alice SDK style).
# 3. Graceful Fallback: возможность задавать вопросы LLM во время паузы в сценарии.

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

EXIT_EXACT = frozenset({
    "хватит", "стоп", "выход", "отмена", "прекрати",
    "закончить", "выйти", "выйди", "сдаюсь", "надоело",
})
EXIT_PHRASES = (
    "хватит играть", "я сдаюсь", "закончить игру",
    "отмени это", "прекрати диалог", "выйди из режима",
)

_ctx_lock = threading.RLock()
_context_stack: list[DialogContext] = []
_session_state: dict[str, Any] = {}


class DialogContext:
    """
    Интерактивный контекст диалога (FSM шаг, игра, опрос, подтверждение).
    """

    def __init__(
        self,
        name: str,
        handler: Callable[..., Any],
        timeout_sec: float = 45.0,
        on_exit: Optional[Callable[[Callable[[str], None]], None]] = None,
        state: str = "",
        data: Optional[dict[str, Any]] = None,
        allow_fallback: bool = False,
    ):
        self.name = name
        self.handler = handler
        self.timeout_sec = timeout_sec
        self.on_exit = on_exit
        self.expire_speak: Optional[Callable[[str], None]] = None
        self.last_active_time = time.time()
        self.state = state
        self.data: dict[str, Any] = dict(data) if data is not None else {}
        self.allow_fallback = allow_fallback

    def is_expired(self) -> bool:
        return (time.time() - self.last_active_time) > self.timeout_sec

    def touch(self) -> None:
        self.last_active_time = time.time()

    def set_state(self, state: str) -> None:
        self.state = state

    def get_state(self) -> str:
        return self.state

    def set_data(self, key: str, value: Any) -> None:
        self.data[key] = value

    def get_data(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def update_data(self, mapping: dict[str, Any]) -> None:
        self.data.update(mapping)


class FSMHandler:
    """
    Декларативный конечный автомат (FSM) для пошаговых сценариев.
    Позволяет регистрировать обработчики для каждого состояния через декоратор:

    fsm = FSMHandler(initial_state="ask_name")

    @fsm.on("ask_name")
    def handle_name(text, speak, ctx):
        ctx.set_data("name", text)
        speak(f"Приятно познакомиться, {text}! Какой у вас город?")
        ctx.set_state("ask_city")
        return False  # продолжить контекст

    @fsm.on("ask_city")
    def handle_city(text, speak, ctx):
        name = ctx.get_data("name")
        speak(f"{name} из {text}, принято!")
        return True  # завершить контекст
    """

    def __init__(self, initial_state: str = "default"):
        self.initial_state = initial_state
        self._handlers: dict[str, Callable[..., Any]] = {}

    def on(self, state: str):
        def decorator(func: Callable[..., Any]):
            self._handlers[state] = func
            return func
        return decorator

    def __call__(self, text: str, speak: Callable[[str], None], ctx: DialogContext | None = None) -> Any:
        current_state = ctx.get_state() if ctx else self.initial_state
        handler = self._handlers.get(current_state) or self._handlers.get("*")
        if handler is None:
            logger.warning("[FSM] Нет обработчика для состояния '%s'", current_state)
            return True

        # Проверяем количество аргументов у обработчика
        import inspect
        sig = inspect.signature(handler)
        params_count = len(sig.parameters)
        if params_count >= 3:
            return handler(text, speak, ctx)
        return handler(text, speak)


# ==============================================================================
# Управление сессионным состоянием (Session State / Alice SDK style)
# ==============================================================================

def get_session_data(key: str | None = None, default: Any = None) -> Any:
    """Возвращает всю сессионную память или значение по ключу."""
    with _ctx_lock:
        if key is None:
            return dict(_session_state)
        return _session_state.get(key, default)


def set_session_data(key: str, value: Any) -> None:
    """Записывает переменную в сессионную память."""
    with _ctx_lock:
        _session_state[key] = value


def update_session_data(mapping: dict[str, Any]) -> None:
    """Обновляет словарь сессионных переменных."""
    with _ctx_lock:
        _session_state.update(mapping)


def clear_session_data() -> None:
    """Очищает сессионную память."""
    with _ctx_lock:
        _session_state.clear()


# ==============================================================================
# Управление стеком контекстов (FSM Stack)
# ==============================================================================

def _call_on_exit(ctx: DialogContext, speak_callback: Optional[Callable[[str], None]]) -> None:
    if not ctx.on_exit:
        return
    cb = speak_callback or ctx.expire_speak or (lambda _text: None)
    try:
        import inspect
        sig = inspect.signature(ctx.on_exit)
        if len(sig.parameters) >= 1:
            ctx.on_exit(cb)
        else:
            ctx.on_exit()
    except Exception as exc:
        logger.error("[Контекст] Ошибка в on_exit для '%s': %s", ctx.name, exc)


def _expire_if_needed(speak_callback: Optional[Callable[[str], None]] = None) -> Optional[DialogContext]:
    """Проверяет протухание контекстов с вершины стека."""
    global _context_stack
    while _context_stack:
        top = _context_stack[-1]
        if top.is_expired():
            popped = _context_stack.pop()
            logger.info("[Контекст] Контекст '%s' истёк по таймауту.", popped.name)
            _call_on_exit(popped, speak_callback or popped.expire_speak)
        else:
            return top
    return None


def push_context(
    name: str,
    handler: Callable[..., Any],
    timeout_sec: float = 45.0,
    on_exit: Optional[Callable[[Callable[[str], None]], None]] = None,
    expire_speak: Optional[Callable[[str], None]] = None,
    state: str = "",
    data: Optional[dict[str, Any]] = None,
    allow_fallback: bool = False,
) -> DialogContext:
    """
    Кладёт новый контекст на вершину стека (например, шаг подтверждения или вложенный диалог).
    Предыдущий контекст не уничтожается и возобновляется после завершения нового.
    """
    global _context_stack
    new_ctx = DialogContext(
        name=name,
        handler=handler,
        timeout_sec=timeout_sec,
        on_exit=on_exit,
        state=state,
        data=data,
        allow_fallback=allow_fallback,
    )
    new_ctx.expire_speak = expire_speak
    with _ctx_lock:
        _context_stack.append(new_ctx)
        logger.info(
            "[Контекст] Push контекста: '%s' (стек: %d, таймаут %sс)",
            name, len(_context_stack), timeout_sec,
        )
    return new_ctx


def pop_context(
    call_on_exit: bool = True,
    speak_callback: Optional[Callable[[str], None]] = None,
) -> Optional[DialogContext]:
    """
    Снимает верхний контекст со стека и возвращает родительский (если есть).
    """
    global _context_stack
    with _ctx_lock:
        if not _context_stack:
            return None
        popped = _context_stack.pop()
        logger.info("[Контекст] Pop контекста: '%s' (осталось в стеке: %d)", popped.name, len(_context_stack))
        parent = _context_stack[-1] if _context_stack else None

    if call_on_exit:
        _call_on_exit(popped, speak_callback)
    return parent


def set_active_context(
    name: str,
    handler: Callable[..., Any],
    timeout_sec: float = 45.0,
    on_exit: Optional[Callable[[Callable[[str], None]], None]] = None,
    expire_speak: Optional[Callable[[str], None]] = None,
    state: str = "",
    data: Optional[dict[str, Any]] = None,
    allow_fallback: bool = False,
) -> DialogContext:
    """
    Устанавливает единственный активный контекст диалога.
    Предыдущие контексты в стеке очищаются без озвучки.
    """
    global _context_stack
    new_ctx = DialogContext(
        name=name,
        handler=handler,
        timeout_sec=timeout_sec,
        on_exit=on_exit,
        state=state,
        data=data,
        allow_fallback=allow_fallback,
    )
    new_ctx.expire_speak = expire_speak
    with _ctx_lock:
        old_stack = list(_context_stack)
        _context_stack = [new_ctx]
        logger.info("[Контекст] Установлен контекст: '%s' (таймаут %sс)", name, timeout_sec)

    for old in old_stack:
        _call_on_exit(old, lambda _text: None)
    return new_ctx


def clear_active_context(
    call_on_exit: bool = False,
    speak_callback: Optional[Callable[[str], None]] = None,
) -> None:
    """Сбрасывает весь стек контекстов диалога."""
    global _context_stack
    with _ctx_lock:
        stack_to_clear = list(_context_stack)
        _context_stack.clear()

    if not stack_to_clear:
        return
    logger.info("[Контекст] Очищен стек контекстов (%d шт)", len(stack_to_clear))
    if call_on_exit:
        for ctx in reversed(stack_to_clear):
            _call_on_exit(ctx, speak_callback)


def get_active_context() -> Optional[DialogContext]:
    """Возвращает текущий активный контекст на вершине стека."""
    with _ctx_lock:
        return _expire_if_needed()


def get_context_state() -> str:
    """Возвращает строковое состояние FSM активного контекста."""
    ctx = get_active_context()
    return ctx.state if ctx else ""


def set_context_state(state: str) -> None:
    """Переключает состояние FSM активного контекста."""
    ctx = get_active_context()
    if ctx:
        ctx.set_state(state)


def get_context_data(key: str, default: Any = None) -> Any:
    """Возвращает переменную из активного контекста."""
    ctx = get_active_context()
    return ctx.get_data(key, default) if ctx else default


def set_context_data(key: str, value: Any) -> None:
    """Записывает переменную в активный контекст."""
    ctx = get_active_context()
    if ctx:
        ctx.set_data(key, value)


def is_in_context() -> bool:
    """Проверяет, находится ли ассистент в интерактивном контексте."""
    return get_active_context() is not None


def _is_context_exit(text: str) -> bool:
    if text in EXIT_EXACT:
        return True
    return any(phrase in text for phrase in EXIT_PHRASES)


def handle_context_input(
    text: str,
    speak_callback: Callable[[str], None],
) -> tuple[bool, bool]:
    """
    Пытается обработать входящий текст в рамках активного контекста (вершины стека).
    Возвращает (handled: bool, should_sleep: bool).
    Если handled=False, маршрутизатор переходит к узким навыкам / Groq, сохраняя контекст!
    """
    global _context_stack
    clean_text = text.lower().strip()
    with _ctx_lock:
        ctx = _expire_if_needed(speak_callback)
        if ctx is None:
            return False, False
        if ctx.expire_speak is None:
            ctx.expire_speak = speak_callback
        if _is_context_exit(clean_text):
            popped = _context_stack.pop() if _context_stack else ctx
            exiting = popped
        else:
            ctx.touch()
            exiting = None

    if exiting is not None:
        logger.info("[Контекст] Завершён контекст по фразе выхода: '%s'", exiting.name)
        _call_on_exit(exiting, speak_callback)
        return True, False

    try:
        import inspect
        sig = inspect.signature(ctx.handler)
        if len(sig.parameters) >= 3:
            result = ctx.handler(clean_text, speak_callback, ctx)
        else:
            result = ctx.handler(clean_text, speak_callback)

        # Разбор вариантов возврата обработчика:
        # 1. True -> завершить данный шаг/контекст (pop)
        # 2. False ->
        #    - если allow_fallback=True: handled=False (пропустить в общий роутер/Groq, но контекст НЕ закрывать)
        #    - если allow_fallback=False: handled=True (ход обработан, остаться в контексте)
        # 3. Tuple (handled, should_close) -> точный контроль
        if isinstance(result, tuple) and len(result) == 2:
            handled, should_close = result
            if should_close:
                pop_context(call_on_exit=False)
            return bool(handled), False

        if result is True:
            pop_context(call_on_exit=False)
            return True, False

        if result is False:
            if ctx.allow_fallback:
                return False, False
            return True, False

        # None или другое значение по умолчанию считает шаг обработанным
        return True, False

    except Exception as exc:
        logger.error("[Контекст] Ошибка обработки контекста '%s': %s", ctx.name, exc)
        pop_context(call_on_exit=False)
        speak_callback("Ошибка.")
        return True, False
