# runtime_state.py
# Поколение речи: «замолчи» / стоп увеличивает счётчик, стрим Groq это видит
# без циклического импорта assistant ↔ skills.

from __future__ import annotations

import threading

_lock = threading.Lock()
_speak_epoch = 0


def bump_speak_epoch() -> int:
    global _speak_epoch
    with _lock:
        _speak_epoch += 1
        return _speak_epoch


def speak_epoch() -> int:
    with _lock:
        return _speak_epoch
