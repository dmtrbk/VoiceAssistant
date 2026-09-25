# skills/model_catalog.py
# Каталог бесплатных моделей OpenRouter: кэш + опциональный ping.

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any

import requests

from skills.openai_client import (
    DEFAULT_API_BASE,
    FAST_MODEL,
    OPENAI_MODEL_CHOICES,
    STRONG_MODEL,
    _api_base,
    _api_key,
    get_client,
)

_LOG = logging.getLogger(__name__)
_CACHE_LOCK = threading.Lock()

CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "openrouter_models_cache.json")
CACHE_TTL_SEC = 6 * 3600
PROBE_TIMEOUT_SEC = 12.0

# id -> "ok" | "fail" | "skip"
_probe_status: dict[str, str] = {}
_probe_lock = threading.Lock()


def _is_free_row(row: dict[str, Any]) -> bool:
    mid = str(row.get("id") or "")
    if mid.endswith(":free") or ":free" in mid:
        return True
    pricing = row.get("pricing") or {}
    try:
        prompt = float(pricing.get("prompt") or 0)
        completion = float(pricing.get("completion") or 0)
    except (TypeError, ValueError):
        return False
    return prompt == 0.0 and completion == 0.0


def _title_for(row: dict[str, Any]) -> str:
    mid = str(row.get("id") or "")
    name = (row.get("name") or "").strip()
    if name and name.lower() != mid.lower():
        short = name
        if mid.endswith(":free") and "free" not in short.lower():
            short = f"{short} (free)"
        return short
    return mid


def _read_cache() -> dict[str, Any] | None:
    if not os.path.exists(CACHE_PATH):
        return None
    try:
        with open(CACHE_PATH, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
        if isinstance(raw, dict) and isinstance(raw.get("models"), list):
            return raw
    except Exception as exc:
        _LOG.debug("[Модели] Кэш не прочитан: %s", exc)
    return None


def _write_cache(models: list[dict[str, str]], probed: dict[str, str] | None = None) -> None:
    payload = {
        "fetched_at": time.time(),
        "models": models,
        "probed": probed or {},
    }
    temp = CACHE_PATH + ".tmp"
    try:
        with open(temp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        os.replace(temp, CACHE_PATH)
    except Exception as exc:
        _LOG.warning("[Модели] Не удалось сохранить кэш: %s", exc)


def cached_free_models(*, allow_stale: bool = True) -> list[tuple[str, str]]:
    """[(id, title), ...] из кэша. Пусто, если кэша нет или он протух (и allow_stale=False)."""
    raw = _read_cache()
    if not raw:
        return []
    age = time.time() - float(raw.get("fetched_at") or 0)
    if age > CACHE_TTL_SEC and not allow_stale:
        return []
    rows: list[tuple[str, str]] = []
    for item in raw.get("models") or []:
        if not isinstance(item, dict):
            continue
        mid = str(item.get("id") or "").strip()
        title = str(item.get("title") or mid).strip()
        if mid:
            rows.append((mid, title))
    probed = raw.get("probed") if isinstance(raw.get("probed"), dict) else {}
    with _probe_lock:
        _probe_status.clear()
        for mid, status in probed.items():
            if isinstance(mid, str) and isinstance(status, str):
                _probe_status[mid] = status
    return rows


def fetch_free_models(*, force: bool = False) -> list[tuple[str, str]]:
    """Тянет каталог OpenRouter, оставляет бесплатные, пишет кэш."""
    with _CACHE_LOCK:
        if not force:
            fresh = cached_free_models(allow_stale=False)
            if fresh:
                return fresh

        key = _api_key()
        if not key:
            return cached_free_models(allow_stale=True) or list(OPENAI_MODEL_CHOICES)

        url = (_api_base() or DEFAULT_API_BASE).rstrip("/") + "/models"
        try:
            response = requests.get(
                url,
                headers={"Authorization": f"Bearer {key}"},
                timeout=20,
            )
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            _LOG.warning("[Модели] Каталог недоступен: %s", exc)
            return cached_free_models(allow_stale=True) or list(OPENAI_MODEL_CHOICES)

        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            return cached_free_models(allow_stale=True) or list(OPENAI_MODEL_CHOICES)

        models: list[dict[str, str]] = []
        seen: set[str] = set()
        for row in data:
            if not isinstance(row, dict) or not _is_free_row(row):
                continue
            mid = str(row.get("id") or "").strip()
            if not mid or mid in seen:
                continue
            # Только текстовый чат: отсекаем чистые image/audio если нет text output
            arch = row.get("architecture") or {}
            out_mods = arch.get("output_modalities") or []
            if out_mods and "text" not in out_mods:
                continue
            seen.add(mid)
            models.append({"id": mid, "title": _title_for(row)})

        models.sort(key=lambda item: item["title"].lower())
        _write_cache(models, probed=dict(_probe_status))
        return [(item["id"], item["title"]) for item in models]


def probe_model(model_id: str) -> str:
    """Короткий ping: 'ok' | 'fail'."""
    mid = (model_id or "").strip()
    if not mid:
        return "fail"
    client = get_client()
    if client is None:
        return "fail"
    try:
        client.chat.completions.create(
            model=mid,
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=1,
            temperature=0,
            timeout=PROBE_TIMEOUT_SEC,
        )
        status = "ok"
    except Exception as exc:
        _LOG.info("[Модели] Ping %s: %s", mid, exc)
        status = "fail"
    with _probe_lock:
        _probe_status[mid] = status
    return status


def probe_models(model_ids: list[str], progress: Any | None = None) -> dict[str, str]:
    """Последовательно пингует модели. progress(done, total, model_id, status) опционально."""
    total = len(model_ids)
    results: dict[str, str] = {}
    for index, mid in enumerate(model_ids, start=1):
        status = probe_model(mid)
        results[mid] = status
        if callable(progress):
            try:
                progress(index, total, mid, status)
            except Exception:
                pass
    cache = _read_cache() or {"models": [], "fetched_at": time.time()}
    models = cache.get("models") if isinstance(cache.get("models"), list) else []
    with _probe_lock:
        probed = dict(_probe_status)
    _write_cache(
        [{"id": str(m.get("id")), "title": str(m.get("title") or m.get("id"))} for m in models if isinstance(m, dict)],
        probed=probed,
    )
    return results


def probe_status(model_id: str) -> str | None:
    with _probe_lock:
        return _probe_status.get(model_id)


def settings_model_choices(current: str | None = None) -> list[tuple[str, str]]:
    """
    Список для комбобокса: избранные сверху, затем бесплатные из кэша/каталога.
    Сеть не трогает — только кэш; обновление через fetch_free_models.
    """
    rows: list[tuple[str, str]] = []
    seen: set[str] = set()

    def labeled(mid: str, title: str) -> str:
        status = probe_status(mid)
        if status == "ok":
            return f"✓ {title}"
        if status == "fail":
            return f"✗ {title}"
        return title

    for mid, title in OPENAI_MODEL_CHOICES:
        if mid not in seen:
            seen.add(mid)
            rows.append((mid, labeled(mid, title)))

    for mid, title in cached_free_models(allow_stale=True):
        if mid in seen:
            continue
        seen.add(mid)
        rows.append((mid, labeled(mid, title)))

    extra = (current or "").strip()
    if extra and extra not in seen:
        rows.append((extra, labeled(extra, extra)))
    return rows


def ensure_catalog_async(callback: Any | None = None) -> None:
    """Фоновое обновление каталога, если кэш протух или пуст."""

    def worker() -> None:
        try:
            models = fetch_free_models(force=False)
            if callable(callback):
                callback(models, None)
        except Exception as exc:
            if callable(callback):
                callback([], exc)

    if cached_free_models(allow_stale=False):
        if callable(callback):
            callback(cached_free_models(allow_stale=True), None)
        return
    threading.Thread(target=worker, name="model-catalog", daemon=True).start()
