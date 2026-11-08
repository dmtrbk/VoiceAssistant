# skills/crypto/bybit_log.py
# Отдельный журнал сырых ответов Bybit (REST + WS), без ключей и без раздува ленты.
# Куски ~8 МБ → gzip в archive/, храним последние 60 частей.

from __future__ import annotations

import gzip
import json
import logging
import os
import shutil
import threading
import time
from logging.handlers import RotatingFileHandler
from typing import Any

_LOG_NAME = "bybit.raw"

# ~/Документы/stol/bybit/current.jsonl + archive/bybit-YYYYMMDD-HHMMSS.jsonl.gz
_STOL_BYBIT_DIR = os.path.join(os.path.expanduser("~"), "Документы", "stol", "bybit")
_DEFAULT_PATH = os.path.join(_STOL_BYBIT_DIR, "current.jsonl")
_DEFAULT_ARCHIVE = os.path.join(_STOL_BYBIT_DIR, "archive")
# 8 МБ — удобно открывать в less/rg; при активной ленте кусок на ~0.5–2 ч.
_MAX_BYTES = 8 * 1024 * 1024
# 60 × 8 МБ ≈ 480 МБ сырых → обычно 40–120 МБ в gzip.
_KEEP_ARCHIVES = 60
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


def _paths() -> tuple[str, str]:
    path = (os.getenv("CRYPTO_BYBIT_LOG_PATH") or "").strip() or _DEFAULT_PATH
    archive = (os.getenv("CRYPTO_BYBIT_LOG_ARCHIVE") or "").strip()
    if not archive:
        archive = os.path.join(os.path.dirname(path) or ".", "archive")
    return path, archive


def _migrate_legacy(current: str) -> None:
    """Старый плоский stol/bybit.jsonl → current.jsonl при первом запуске."""
    legacy = os.path.join(os.path.expanduser("~"), "Документы", "stol", "bybit.jsonl")
    if os.path.isfile(current) or not os.path.isfile(legacy):
        return
    try:
        os.makedirs(os.path.dirname(current) or ".", exist_ok=True)
        os.replace(legacy, current)
    except Exception:
        try:
            shutil.copy2(legacy, current)
        except Exception:
            pass


def _prune_archives(archive_dir: str, keep: int = _KEEP_ARCHIVES) -> None:
    try:
        names = [
            os.path.join(archive_dir, name)
            for name in os.listdir(archive_dir)
            if name.startswith("bybit-") and name.endswith(".jsonl.gz")
        ]
    except FileNotFoundError:
        return
    except Exception:
        return
    if len(names) <= keep:
        return
    names.sort(key=lambda p: os.path.getmtime(p))
    for path in names[: max(0, len(names) - keep)]:
        try:
            os.remove(path)
        except Exception:
            pass


def _gzip_archive(source: str, archive_dir: str) -> str | None:
    if not os.path.isfile(source) or os.path.getsize(source) <= 0:
        try:
            if os.path.isfile(source):
                os.remove(source)
        except Exception:
            pass
        return None
    os.makedirs(archive_dir, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = os.path.join(archive_dir, f"bybit-{stamp}.jsonl.gz")
    if os.path.exists(dest):
        dest = os.path.join(archive_dir, f"bybit-{stamp}-{os.getpid()}.jsonl.gz")
    try:
        with open(source, "rb") as src, gzip.open(dest, "wb", compresslevel=6) as out:
            shutil.copyfileobj(src, out, length=1024 * 1024)
        os.remove(source)
        _prune_archives(archive_dir)
        return dest
    except Exception:
        return None


class _ArchiveRotatingHandler(RotatingFileHandler):
    """При переполнении — gzip в archive/, без цепочки .1/.2."""

    def __init__(self, filename: str, archive_dir: str, max_bytes: int) -> None:
        self._archive_dir = archive_dir
        super().__init__(
            filename,
            maxBytes=max(1024, int(max_bytes)),
            backupCount=1,
            encoding="utf-8",
        )

    def doRollover(self) -> None:
        if self.stream:
            self.stream.close()
            self.stream = None
        _gzip_archive(self.baseFilename, self._archive_dir)
        if not self.delay:
            self.stream = self._open()


def _ensure() -> logging.Logger | None:
    global _ready, _logger
    if not _enabled():
        return None
    with _lock:
        if _ready:
            return _logger
        path, archive = _paths()
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            os.makedirs(archive, exist_ok=True)
            _migrate_legacy(path)
            log = logging.getLogger(_LOG_NAME)
            log.setLevel(logging.INFO)
            log.propagate = False
            if not any(isinstance(h, _ArchiveRotatingHandler) for h in log.handlers):
                for old in list(log.handlers):
                    log.removeHandler(old)
                    try:
                        old.close()
                    except Exception:
                        pass
                handler = _ArchiveRotatingHandler(path, archive, _MAX_BYTES)
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
