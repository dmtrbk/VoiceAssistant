# runtime_state.py
# Поколение речи: «замолчи» / стоп увеличивает счётчик, стрим Groq это видит
# без циклического импорта assistant ↔ skills.

from __future__ import annotations

import threading

_lock = threading.Lock()
_speak_epoch = 0
_extra_tts_stops: list = []


def bump_speak_epoch() -> int:
    global _speak_epoch
    with _lock:
        _speak_epoch += 1
        return _speak_epoch


def speak_epoch() -> int:
    with _lock:
        return _speak_epoch


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
