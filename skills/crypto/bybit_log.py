# skills/crypto/bybit_log.py
# Отдельный журнал сырых ответов Bybit (REST + WS), без ключей и без раздува ленты.

from __future__ import annotations

import json
import logging
import os
import threading
import time
from logging.handlers import RotatingFileHandler
from typing import Any

from .common import _PROJECT_DIR

_LOG_NAME = "bybit.raw"
_DEFAULT_PATH = os.path.join(_PROJECT_DIR, "logs", "bybit.jsonl")
_MAX_BYTES = 32 * 1024 * 1024
_BACKUP_COUNT = 5
_LIST_KEEP = 8
_STR_KEEP = 800
_LINE_KEEP = 12_000

_lock = threading.Lock()
_ready = False
_logger: logging.Logger | None = None


def _enabled() -> bool:
    raw = (os.getenv("CRYPTO_BYBIT_LOG") or "on").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _ws_tickers_enabled() -> bool:
    """Тикеры WS очень частые — по умолчанию краткий срез, полный — CRYPTO_BYBIT_LOG_WS=full."""
    raw = (os.getenv("CRYPTO_BYBIT_LOG_WS") or "summary").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _ws_full() -> bool:
    return (os.getenv("CRYPTO_BYBIT_LOG_WS") or "").strip().lower() in {"full", "1", "true", "on"}


def _ensure() -> logging.Logger | None:
    global _ready, _logger
    if not _enabled():
        return None
    with _lock:
        if _ready:
            return _logger
        path = (os.getenv("CRYPTO_BYBIT_LOG_PATH") or "").strip() or _DEFAULT_PATH
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            log = logging.getLogger(_LOG_NAME)
            log.setLevel(logging.INFO)
            log.propagate = False
            if not any(isinstance(h, RotatingFileHandler) for h in log.handlers):
                handler = RotatingFileHandler(
                    path,
                    maxBytes=_MAX_BYTES,
                    backupCount=_BACKUP_COUNT,
                    encoding="utf-8",
                )
                handler.setFormatter(logging.Formatter("%(message)s"))
                log.addHandler(handler)
            _logger = log
            _ready = True
            return _logger
        except Exception:
            _ready = True
            _logger = None
            return None


def _shrink(value: Any, *, depth: int = 0) -> Any:
    if depth > 6:
        return "…"
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            low = str(key).lower()
            if any(mark in low for mark in ("secret", "sign", "api-key", "apikey", "token")):
                out[key] = "***"
                continue
            out[key] = _shrink(item, depth=depth + 1)
        return out
    if isinstance(value, list):
        if len(value) > _LIST_KEEP:
            head = [_shrink(item, depth=depth + 1) for item in value[:_LIST_KEEP]]
            return {"_len": len(value), "_head": head}
        return [_shrink(item, depth=depth + 1) for item in value]
    if isinstance(value, str) and len(value) > _STR_KEEP:
        return value[:_STR_KEEP] + f"…(+{len(value) - _STR_KEEP})"
    return value


def _emit(row: dict[str, Any]) -> None:
    log = _ensure()
    if log is None:
        return
    row.setdefault("ts", time.time())
    try:
        line = json.dumps(row, ensure_ascii=False, separators=(",", ":"), default=str)
    except Exception:
        return
    if len(line) > _LINE_KEEP:
        line = line[:_LINE_KEEP] + f"…(+{len(line) - _LINE_KEEP})"
    try:
        log.info(line)
    except Exception:
        pass


def log_rest(
    *,
    method: str,
    path: str,
    params: dict[str, Any] | None = None,
    data: dict[str, Any] | None = None,
    error: str | None = None,
    cached: bool = False,
    signed: bool = False,
) -> None:
    """Ответ REST Bybit (или ошибка парсинга)."""
    if not _enabled():
        return
    body = _shrink(data) if isinstance(data, dict) else None
    row: dict[str, Any] = {
        "ch": "rest",
        "method": str(method or "GET").upper(),
        "path": str(path or ""),
        "signed": bool(signed),
        "cached": bool(cached),
    }
    if params:
        row["params"] = _shrink(params)
    if body is not None:
        row["retCode"] = body.get("retCode") if isinstance(body, dict) else None
        row["retMsg"] = body.get("retMsg") if isinstance(body, dict) else None
        row["body"] = body
    if error:
        row["error"] = str(error)[:400]
    _emit(row)


def log_ws(raw: str | dict[str, Any]) -> None:
    """Сообщение публичного WS. Тикеры по умолчанию — краткий срез."""
    if not _enabled() or not _ws_tickers_enabled():
        return
    try:
        msg = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        _emit({"ch": "ws", "raw": str(raw)[:_STR_KEEP]})
        return
    if not isinstance(msg, dict):
        _emit({"ch": "ws", "raw": str(msg)[:_STR_KEEP]})
        return
    topic = str(msg.get("topic") or "")
    op = str(msg.get("op") or "")
    if topic.startswith("tickers.") and not _ws_full():
        data = msg.get("data") if isinstance(msg.get("data"), dict) else {}
        _emit(
            {
                "ch": "ws",
                "topic": topic,
                "symbol": data.get("symbol"),
                "last": data.get("lastPrice"),
                "bid": data.get("bid1Price"),
                "ask": data.get("ask1Price"),
                "chg24": data.get("price24hPcnt"),
            }
        )
        return
    if op in {"ping", "pong"} and not _ws_full():
        _emit({"ch": "ws", "op": op})
        return
    _emit({"ch": "ws", "body": _shrink(msg)})


__all__ = ["log_rest", "log_ws"]
