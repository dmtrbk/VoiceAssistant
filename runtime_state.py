# runtime_state.py
# Поколение речи: «замолчи» / стоп увеличивает счётчик, стрим Groq это видит
# без циклического импорта assistant ↔ skills.
# Telegram/CLI через voice_interrupt гасят Piper в assistant.py.

from __future__ import annotations

import threading

_lock = threading.Lock()
_speak_epoch = 0
_session_epoch = 0
_extra_tts_stops: list = []
_voice_interrupts: list = []


def bump_speak_epoch() -> int:
    global _speak_epoch
    with _lock:
        _speak_epoch += 1
        return _speak_epoch


def speak_epoch() -> int:
    with _lock:
        return _speak_epoch


def bump_session_epoch() -> int:
    """Сон / тайм-аут / авария: фоновые команды после этого не трогают сессию."""
    global _session_epoch
    with _lock:
        _session_epoch += 1
        return _session_epoch


def session_epoch() -> int:
    with _lock:
        return _session_epoch


def register_extra_tts_stop(callback) -> None:
    """CLI и другие очереди TTS, которые нужно гасить вместе со «стоп»."""
    with _lock:
        if callback not in _extra_tts_stops:
            _extra_tts_stops.append(callback)


def stop_extra_tts() -> None:
    with _lock:
        hooks = list(_extra_tts_stops)
    for hook in hooks:
        try:
            hook()
        except Exception:
            pass


def register_voice_interrupt(callback) -> None:
    """assistant.py: «стоп» / «замолчи» / «спать» с Telegram и CLI гасят Piper."""
    with _lock:
        if callback not in _voice_interrupts:
            _voice_interrupts.append(callback)


def voice_interrupt(kind: str) -> None:
    """kind: stop | hold | sleep. Без хука (CLI без assistant) — тихий no-op."""
    with _lock:
        hooks = list(_voice_interrupts)
    for hook in hooks:
        try:
            hook(kind)
        except Exception:
            pass


def reset_interrupt_hooks() -> None:
    """Только тесты: не оставлять чужие хуки между кейсами."""
    global _extra_tts_stops, _voice_interrupts
    with _lock:
        _extra_tts_stops.clear()
        _voice_interrupts.clear()
