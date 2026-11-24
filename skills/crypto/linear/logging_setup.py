"""Консоль + файл логов linear-стола (ротация)."""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import LinearConfig

_FMT = "%(asctime)s %(levelname)s %(name)s %(message)s"
_MAX_BYTES = 8 * 1024 * 1024
_BACKUP = 12  # ~96 МБ истории desk-логов


def log_dir_for(cfg: LinearConfig) -> Path:
    raw = (os.getenv("LINEAR_LOG_DIR") or "").strip()
    if raw:
        path = Path(raw).expanduser()
    else:
        path = cfg.runtime_dir / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def setup_logging(cfg: LinearConfig, *, level: int = logging.INFO) -> Path:
    """Пишем в runtime/logs/desk.log + stdout. Возвращает путь к файлу."""
    directory = log_dir_for(cfg)
    log_path = directory / "desk.log"
    root = logging.getLogger()
    root.setLevel(level)
    # не дублировать при повторном вызове
    for handler in list(root.handlers):
        if getattr(handler, "_linear_desk_log", False):
            root.removeHandler(handler)
            handler.close()

    formatter = logging.Formatter(_FMT)

    if not any(isinstance(h, logging.StreamHandler) and not isinstance(h, RotatingFileHandler) for h in root.handlers):
        stream = logging.StreamHandler()
        stream.setFormatter(formatter)
        stream.setLevel(level)
        root.addHandler(stream)

    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=_MAX_BYTES,
        backupCount=_BACKUP,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(level)
    file_handler._linear_desk_log = True  # type: ignore[attr-defined]
    root.addHandler(file_handler)

    logging.getLogger(__name__).info(
        "[linear] логи → %s (journals → %s)",
        log_path,
        cfg.runtime_dir,
    )
    return log_path
