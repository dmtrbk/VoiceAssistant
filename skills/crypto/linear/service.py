"""Фон linear-стола: старт вместе с ассистентом (не с Cursor).

`crypto_skill.start_background()` → ensure_started(), если desk_enabled.
Закрытие Cursor не нужно: процесс живёт в процессе Джарвиса.
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .desk_linear import LinearDesk

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_thread: threading.Thread | None = None
_desk: LinearDesk | None = None


def is_running() -> bool:
    t = _thread
    return bool(t is not None and t.is_alive())


def ensure_started() -> None:
    """Поднять paper/live desk, если в настройках desk_enabled."""
    from .settings_store import current

    if not current().desk_enabled:
        ensure_stopped()
        return
    with _lock:
        global _thread
        if _thread is not None and _thread.is_alive():
            return
        _thread = threading.Thread(
            target=_run_desk,
            name="linear-desk",
            daemon=True,
        )
        _thread.start()
        logger.info("[linear] фон: поток стартовал (desk_enabled)")


def ensure_stopped() -> None:
    global _thread, _desk
    with _lock:
        desk = _desk
        _desk = None
    if desk is not None:
        try:
            desk.stop()
        except Exception as exc:
            logger.debug("[linear] stop: %s", exc)
        logger.info("[linear] фон: остановлен")
    t = _thread
    if t is not None and t.is_alive() and t is not threading.current_thread():
        t.join(timeout=3.0)
    with _lock:
        if _thread is t:
            _thread = None


def _run_desk() -> None:
    global _desk
    try:
        from .config import default_config
        from .desk_linear import LinearDesk
        from .logging_setup import setup_logging
        from .settings_store import current

        data = current()
        if not data.desk_enabled:
            return
        cfg = default_config(paper=bool(data.paper))
        # пол kill ниже старта (paper $120)
        if cfg.kill_equity_floor_usd >= cfg.start_equity_usd:
            cfg.kill_equity_floor_usd = max(50.0, cfg.start_equity_usd * 0.8)
        setup_logging(cfg)
        desk = LinearDesk(cfg)
        with _lock:
            _desk = desk
        logger.info(
            "[linear] фон desk: paper=%s equity=%.0f symbols=%s",
            cfg.paper,
            cfg.start_equity_usd,
            cfg.symbols,
        )
        desk.start()
    except Exception:
        logger.exception("[linear] фон desk упал")
    finally:
        with _lock:
            _desk = None
