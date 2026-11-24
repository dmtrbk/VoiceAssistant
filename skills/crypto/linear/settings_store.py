"""Живые настройки linear-стола → jarvis_linear_settings.json."""

from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass, fields, replace
from typing import Any

_PROJECT_DIR = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)
_PATH = os.path.join(_PROJECT_DIR, "jarvis_linear_settings.json")
_lock = threading.Lock()
_cache: "LinearSettings | None" = None
_mtime: float | None = None


@dataclass(frozen=True)
class LinearSettings:
    paper: bool = True
    shadow_on: bool = True
    symbols: str = "ZECUSDT,HYPEUSDT"
    shadow_symbols: str = "ZECUSDT,HYPEUSDT,AAVEUSDT"
    sizing_mode: str = "fixed"  # fixed | risk_clamp
    leg_usd: float = 40.0
    target_risk_usd: float = 0.80
    leg_min_usd: float = 30.0
    leg_max_usd: float = 50.0
    uta_reserve_usd: float = 120.0
    kill_daily_loss_usd: float = 8.0
    kill_equity_floor_usd: float = 100.0
    start_equity_usd: float = 120.0  # paper: виртуальный баланс
    postonly_policy: str = "reprice_1tick"  # reprice_1tick | skip
    desk_enabled: bool = True  # paper/live loop intent (runner / future tray)


DEFAULTS = LinearSettings()
_KEYS = {f.name for f in fields(LinearSettings)}
_BOOL = frozenset({"paper", "shadow_on", "desk_enabled"})
_CHOICES = {
    "sizing_mode": ("fixed", "risk_clamp"),
    "postonly_policy": ("reprice_1tick", "skip"),
}
_LIMITS = {
    "leg_usd": (10.0, 100.0),
    "target_risk_usd": (0.2, 3.0),
    "leg_min_usd": (10.0, 80.0),
    "leg_max_usd": (20.0, 150.0),
    "uta_reserve_usd": (50.0, 500.0),
    "kill_daily_loss_usd": (2.0, 40.0),
    "kill_equity_floor_usd": (50.0, 500.0),
    "start_equity_usd": (50.0, 500.0),
}


def settings_path() -> str:
    return _PATH


def reset_cache() -> None:
    global _cache, _mtime
    with _lock:
        _cache = None
        _mtime = None


def _clamp(key: str, value: object) -> object:
    if key in _BOOL:
        return bool(value)
    if key in _CHOICES:
        raw = str(value or "")
        return raw if raw in _CHOICES[key] else _CHOICES[key][0]
    if key in {"symbols", "shadow_symbols"}:
        parts = [p.strip().upper() for p in str(value or "").split(",") if p.strip()]
        return ",".join(parts) if parts else getattr(DEFAULTS, key)
    number = float(value)
    lo, hi = _LIMITS[key]
    number = max(lo, min(hi, number))
    return number


def clamp_settings(data: LinearSettings) -> LinearSettings:
    values = {k: _clamp(k, getattr(data, k)) for k in _KEYS}
    lo = float(values["leg_min_usd"])
    hi = float(values["leg_max_usd"])
    if lo > hi:
        values["leg_max_usd"] = lo
    return LinearSettings(**values)


def current() -> LinearSettings:
    global _cache, _mtime
    try:
        mtime = os.path.getmtime(_PATH)
    except OSError:
        mtime = None
    with _lock:
        if _cache is not None and _mtime == mtime:
            return _cache
    data = DEFAULTS if mtime is None else _read(_PATH)
    with _lock:
        _cache = data
        _mtime = mtime
        return data


def update(**changes: object) -> LinearSettings:
    data = clamp_settings(replace(current(), **{k: v for k, v in changes.items() if k in _KEYS}))
    _write(data)
    return data


def reset() -> LinearSettings:
    _write(DEFAULTS)
    return DEFAULTS


def status_line() -> str:
    data = current()
    mode = "paper" if data.paper else "LIVE"
    sizing = "фикс $" + f"{data.leg_usd:.0f}" if data.sizing_mode == "fixed" else "risk-clamp"
    parts = [
        f"Linear {mode}",
        f"старт ${data.start_equity_usd:.0f}",
        f"Core {data.symbols}",
        sizing,
    ]
    if data.shadow_on:
        parts.append("shadow AAVE")
    if not data.desk_enabled:
        parts.append("контур выкл")
    return " · ".join(parts)


def to_linear_config():
    """Собрать LinearConfig из живых настроек."""
    from .config import LinearConfig

    data = current()
    cfg = LinearConfig(
        paper=bool(data.paper),
        symbols=tuple(s for s in data.symbols.split(",") if s),
        shadow_symbols=tuple(s for s in data.shadow_symbols.split(",") if s),
        shadow_enabled=bool(data.shadow_on),
        sizing_mode=str(data.sizing_mode),
        leg_usd=float(data.leg_usd),
        target_risk_usd=float(data.target_risk_usd),
        leg_min_usd=float(data.leg_min_usd),
        leg_max_usd=float(data.leg_max_usd),
        uta_trade_reserve_usd=float(data.uta_reserve_usd),
        kill_daily_loss_usd=float(data.kill_daily_loss_usd),
        kill_equity_floor_usd=float(data.kill_equity_floor_usd),
        start_equity_usd=float(data.start_equity_usd),
        postonly_reject_policy=str(data.postonly_policy),
    )
    cfg.runtime_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def _read(path: str) -> LinearSettings:
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except Exception:
        return DEFAULTS
    if not isinstance(raw, dict):
        return DEFAULTS
    changes = {k: raw[k] for k in _KEYS if k in raw}
    return clamp_settings(replace(DEFAULTS, **changes))


def _write(data: LinearSettings) -> None:
    global _cache, _mtime
    tmp = _PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(asdict(data), fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp, _PATH)
    with _lock:
        _cache = data
        try:
            _mtime = os.path.getmtime(_PATH)
        except OSError:
            _mtime = None
