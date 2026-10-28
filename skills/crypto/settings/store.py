# skills/crypto/settings/store.py
# Живые пороги стола. Файл свой, не skills_enabled.json.

from __future__ import annotations

import json
import os
import threading
from typing import Any

from .schema import DEFAULTS, DeskSettings, from_payload, to_payload

_PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
_PATH = os.path.join(_PROJECT_DIR, "jarvis_crypto_settings.json")
_lock = threading.Lock()
_cache: DeskSettings | None = None
_mtime: float | None = None


def settings_path() -> str:
    return _PATH


def reset_cache() -> None:
    global _cache, _mtime
    with _lock:
        _cache = None
        _mtime = None


def current() -> DeskSettings:
    global _cache, _mtime
    path = _PATH
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = None
    with _lock:
        if _cache is not None and _mtime == mtime:
            return _cache
    data = DEFAULTS if mtime is None else _read(path)
    with _lock:
        _cache = data
        _mtime = mtime
        return data


def update(**changes: object) -> DeskSettings:
    from .schema import merge_settings

    data = merge_settings(current(), **changes)
    _write(data)
    return data


def reset() -> DeskSettings:
    _write(DEFAULTS)
    return DEFAULTS


def status_line() -> str:
    from skills.crypto.common import probe_arms, probe_legs, read_alloc_state, read_btc_dip, read_probe, read_sui_dip

    data = current()
    pocket = read_btc_dip()
    sui = read_sui_dip()
    probe = read_probe()
    state = read_alloc_state() or {}
    alloc = state.get("alloc") if isinstance(state.get("alloc"), dict) else {}
    sleeve = len(alloc)
    parts = ["Карман открыт" if float(pocket.get("qty") or 0) > 0 else "Карман закрыт"]
    if float(sui.get("qty") or 0) > 0:
        parts.append(f"{str(sui.get('ticker') or 'AVA')} открыт")
    elif not data.second_on:
        parts.append("Второй выключен")
    if not data.sleeve_on:
        parts.append("Рукав выключен")
    else:
        parts.append(f"Рукав {sleeve} из {int(data.sleeve_max_names)}")
    if not data.probe_on:
        parts.append("Щуп выключен")
    else:
        held = probe_legs(probe)
        arms = probe_arms(probe)
        if held:
            names = ", ".join(str(leg["ticker"]) for leg in held)
            parts.append(f"Щуп держит {names}")
        if arms:
            names = ", ".join(str(arm["ticker"]) for arm in arms)
            parts.append(f"Щуп взведён {names}")
        if not held and not arms:
            parts.append("Щуп молчит")
    return " · ".join(parts)


def _read(path: str) -> DeskSettings:
    try:
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
    except Exception:
        return DEFAULTS
    return from_payload(raw)


def _write(data: DeskSettings) -> None:
    global _cache, _mtime
    path = _PATH
    temp_path = path + ".tmp"
    payload = to_payload(data)
    with open(temp_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(temp_path, path)
    with _lock:
        _cache = data
        try:
            _mtime = os.path.getmtime(path)
        except OSError:
            _mtime = None


def _dump_for_tests() -> dict[str, Any]:
    return to_payload(current())
