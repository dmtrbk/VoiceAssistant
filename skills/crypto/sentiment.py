# skills/crypto/sentiment.py
# Внешний фон для советника (не для автостола). Fear & Greed с дисковым кэшем.

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

import requests

from .common import _PROJECT_DIR

logger = logging.getLogger(__name__)

_FNG_URL = "https://api.alternative.me/fng/?limit=1"
_FNG_CACHE_PATH = os.path.join(_PROJECT_DIR, "jarvis_crypto_fng.json")
_FNG_CACHE_SEC = 6 * 60 * 60
_FNG_TIMEOUT = 8.0


def _load_raw(path: str) -> dict[str, Any] | None:
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
    except Exception:
        return None
    if not isinstance(raw, dict):
        return None
    try:
        value = int(raw["value"])
        label = str(raw.get("label") or "").strip()
        ts = float(raw.get("ts") or 0)
    except (KeyError, TypeError, ValueError):
        return None
    if not label:
        return None
    return {"value": value, "label": label, "ts": ts}


def _write_cache(value: int, label: str, path: str = _FNG_CACHE_PATH) -> None:
    payload = {"value": int(value), "label": str(label), "ts": time.time()}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)
    os.replace(tmp, path)


def fetch_fear_greed(*, session: requests.Session | None = None) -> dict[str, Any] | None:
    """Свежий индекс alternative.me. Без кэша. При ошибке — None."""
    http = session or requests
    try:
        resp = http.get(_FNG_URL, timeout=_FNG_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.info("[Крипта] Fear & Greed недоступен: %s", exc)
        return None
    rows = data.get("data") if isinstance(data, dict) else None
    if not isinstance(rows, list) or not rows:
        return None
    row = rows[0]
    try:
        value = int(row.get("value"))
        label = str(row.get("value_classification") or "").strip()
    except (TypeError, ValueError):
        return None
    if not label:
        return None
    return {"value": value, "label": label}


def fear_greed_snapshot(
    *,
    cache_path: str = _FNG_CACHE_PATH,
    session: requests.Session | None = None,
    force: bool = False,
) -> dict[str, Any] | None:
    """Кэш 6 ч, иначе сеть. Просроченный кэш — запас при сбое сети. Автостол не вызывает."""
    raw = _load_raw(cache_path)
    if raw is not None and not force and time.time() - float(raw["ts"]) <= _FNG_CACHE_SEC:
        return {"value": int(raw["value"]), "label": str(raw["label"])}
    fresh = fetch_fear_greed(session=session)
    if fresh is not None:
        try:
            _write_cache(fresh["value"], fresh["label"], path=cache_path)
        except Exception as exc:
            logger.info("[Крипта] Fear & Greed кэш: %s", exc)
        return fresh
    if raw is not None:
        return {"value": int(raw["value"]), "label": str(raw["label"])}
    return None


def format_fear_greed(snap: dict[str, Any] | None) -> str:
    if not snap:
        return ""
    try:
        value = int(snap["value"])
        label = str(snap.get("label") or "").strip()
    except (KeyError, TypeError, ValueError):
        return ""
    if not label:
        return ""
    return f"Fear & Greed: {value} ({label})"


def fear_greed_line(**kwargs: Any) -> str:
    """Одна строка для _alloc_facts; пусто при сбое."""
    return format_fear_greed(fear_greed_snapshot(**kwargs))
