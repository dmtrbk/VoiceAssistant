# skills/crypto/settings/schema.py
# Пороги стола: поля, пределы, рабочие числа. Без диска и Qt.

from __future__ import annotations

from dataclasses import asdict, dataclass, fields, replace


@dataclass(frozen=True)
class DeskSettings:
    pocket_pct: float = 25.0
    pocket_day_pct: float = -3.0
    pocket_bounce_pct: float = 1.0
    pocket_stop_pct: float = 15.0
    pocket_hold_sec: float = 10 * 60
    pocket_exit_pullback_pct: float = 0.5
    pocket_exit_hold_sec: float = 5 * 60
    sui_usd: float = 50.0
    second_on: bool = False
    second_ticker: str = "AVA"
    sleeve_max_pct: float = 50.0
    sleeve_max_names: int = 2
    sleeve_entry_z: float = -2.5
    sleeve_bounce_pct: float = 1.5
    sleeve_hold_sec: float = 20 * 60
    sleeve_still_z: float = -1.2
    sleeve_cancel_z: float = -0.5
    sleeve_exit_arm_pct: float = 3.0
    sleeve_exit_pullback_pct: float = 0.5
    sleeve_exit_hold_sec: float = 5 * 60
    sleeve_stop_pct: float = 7.0
    probe_on: bool = True
    probe_usd: float = 30.0
    probe_max_loss_pct: float = 8.0
    probe_shelf_range_pct: float = 12.0
    probe_break_min_pct: float = 4.0
    probe_break_max_pct: float = 12.0
    rebalance_pct: float = 8.0
    cooldown_hours: float = 12.0


DEFAULTS = DeskSettings()

_LIMITS: dict[str, tuple[float, float]] = {
    "pocket_pct": (8.0, 50.0),
    "pocket_day_pct": (-15.0, -1.0),
    "pocket_bounce_pct": (0.3, 5.0),
    "pocket_stop_pct": (5.0, 30.0),
    "pocket_hold_sec": (2 * 60, 40 * 60),
    "pocket_exit_pullback_pct": (0.2, 3.0),
    "pocket_exit_hold_sec": (60, 30 * 60),
    "sui_usd": (10.0, 200.0),
    "sleeve_max_pct": (20.0, 92.0),
    "sleeve_max_names": (1, 6),
    "sleeve_entry_z": (-4.0, -1.0),
    "sleeve_bounce_pct": (0.3, 5.0),
    "sleeve_hold_sec": (5 * 60, 60 * 60),
    "sleeve_still_z": (-2.5, 0.0),
    "sleeve_cancel_z": (-2.0, 0.0),
    "sleeve_exit_arm_pct": (1.0, 10.0),
    "sleeve_exit_pullback_pct": (0.2, 3.0),
    "sleeve_exit_hold_sec": (60, 30 * 60),
    "sleeve_stop_pct": (3.0, 20.0),
    "probe_usd": (10.0, 100.0),
    "probe_max_loss_pct": (3.0, 20.0),
    "probe_shelf_range_pct": (4.0, 25.0),
    "probe_break_min_pct": (1.0, 10.0),
    "probe_break_max_pct": (6.0, 25.0),
    "rebalance_pct": (2.0, 15.0),
    "cooldown_hours": (1.0, 48.0),
}

_INT_FIELDS = frozenset({"sleeve_max_names"})
_BOOL_FIELDS = frozenset({"probe_on", "second_on"})
_CHOICES: dict[str, tuple[str, ...]] = {"second_ticker": ("SUI", "AVA")}
_KEYS = {item.name for item in fields(DeskSettings)}


def clamp_field(key: str, value: object) -> object:
    if key not in _KEYS:
        raise KeyError(key)
    if key in _BOOL_FIELDS:
        return bool(value)
    if key in _CHOICES:
        raw = str(value or "").upper()
        allowed = _CHOICES[key]
        return raw if raw in allowed else allowed[-1]
    number = float(value)
    low, high = _LIMITS[key]
    number = max(low, min(high, number))
    if key in _INT_FIELDS:
        return int(round(number))
    return number


def clamp_settings(data: DeskSettings) -> DeskSettings:
    values = {key: clamp_field(key, getattr(data, key)) for key in _KEYS}
    pocket = float(values["pocket_pct"])
    sleeve = float(values["sleeve_max_pct"])
    if pocket + sleeve > 100.0 + 1e-6:
        values["sleeve_max_pct"] = clamp_field("sleeve_max_pct", 100.0 - pocket)
    low = float(values["probe_break_min_pct"])
    high = float(values["probe_break_max_pct"])
    if low >= high:
        values["probe_break_max_pct"] = clamp_field("probe_break_max_pct", low + 0.5)
    return DeskSettings(**values)


def merge_settings(base: DeskSettings, **changes: object) -> DeskSettings:
    merged = replace(base, **changes)
    pocket = float(clamp_field("pocket_pct", merged.pocket_pct))
    sleeve = float(clamp_field("sleeve_max_pct", merged.sleeve_max_pct))
    if pocket + sleeve > 100.0 + 1e-6:
        if "sleeve_max_pct" in changes and "pocket_pct" not in changes:
            changes = {**changes, "pocket_pct": 100.0 - sleeve}
        else:
            changes = {**changes, "sleeve_max_pct": 100.0 - pocket}
    return clamp_settings(replace(base, **changes))


def to_payload(data: DeskSettings) -> dict[str, object]:
    return {
        "pocket": {
            "pct": data.pocket_pct,
            "day_pct": data.pocket_day_pct,
            "bounce_pct": data.pocket_bounce_pct,
            "stop_pct": data.pocket_stop_pct,
            "hold_sec": data.pocket_hold_sec,
            "exit_pullback_pct": data.pocket_exit_pullback_pct,
            "exit_hold_sec": data.pocket_exit_hold_sec,
        },
        "sui": {"usd": data.sui_usd, "on": data.second_on, "ticker": data.second_ticker},
        "sleeve": {
            "max_pct": data.sleeve_max_pct,
            "max_names": data.sleeve_max_names,
            "entry_z": data.sleeve_entry_z,
            "bounce_pct": data.sleeve_bounce_pct,
            "hold_sec": data.sleeve_hold_sec,
            "still_z": data.sleeve_still_z,
            "cancel_z": data.sleeve_cancel_z,
            "exit_arm_pct": data.sleeve_exit_arm_pct,
            "exit_pullback_pct": data.sleeve_exit_pullback_pct,
            "exit_hold_sec": data.sleeve_exit_hold_sec,
            "stop_pct": data.sleeve_stop_pct,
        },
        "probe": {
            "on": data.probe_on,
            "usd": data.probe_usd,
            "max_loss_pct": data.probe_max_loss_pct,
            "shelf_range_pct": data.probe_shelf_range_pct,
            "break_min_pct": data.probe_break_min_pct,
            "break_max_pct": data.probe_break_max_pct,
        },
        "risk": {
            "rebalance_pct": data.rebalance_pct,
            "cooldown_hours": data.cooldown_hours,
        },
    }


def from_payload(raw: object) -> DeskSettings:
    if not isinstance(raw, dict):
        return DEFAULTS
    if any(key in raw for key in _KEYS):
        flat = {key: raw[key] for key in _KEYS if key in raw}
        return merge_settings(DEFAULTS, **flat)
    pocket = raw.get("pocket") if isinstance(raw.get("pocket"), dict) else {}
    sui = raw.get("sui") if isinstance(raw.get("sui"), dict) else {}
    sleeve = raw.get("sleeve") if isinstance(raw.get("sleeve"), dict) else {}
    probe = raw.get("probe") if isinstance(raw.get("probe"), dict) else {}
    risk = raw.get("risk") if isinstance(raw.get("risk"), dict) else {}
    mapped = {
        "pocket_pct": pocket.get("pct"),
        "pocket_day_pct": pocket.get("day_pct"),
        "pocket_bounce_pct": pocket.get("bounce_pct"),
        "pocket_stop_pct": pocket.get("stop_pct"),
        "pocket_hold_sec": pocket.get("hold_sec"),
        "pocket_exit_pullback_pct": pocket.get("exit_pullback_pct"),
        "pocket_exit_hold_sec": pocket.get("exit_hold_sec"),
        "sui_usd": sui.get("usd"),
        "second_on": sui.get("on"),
        "second_ticker": sui.get("ticker"),
        "sleeve_max_pct": sleeve.get("max_pct"),
        "sleeve_max_names": sleeve.get("max_names"),
        "sleeve_entry_z": sleeve.get("entry_z"),
        "sleeve_bounce_pct": sleeve.get("bounce_pct"),
        "sleeve_hold_sec": sleeve.get("hold_sec"),
        "sleeve_still_z": sleeve.get("still_z"),
        "sleeve_cancel_z": sleeve.get("cancel_z"),
        "sleeve_exit_arm_pct": sleeve.get("exit_arm_pct"),
        "sleeve_exit_pullback_pct": sleeve.get("exit_pullback_pct"),
        "sleeve_exit_hold_sec": sleeve.get("exit_hold_sec"),
        "sleeve_stop_pct": sleeve.get("stop_pct"),
        "probe_on": probe.get("on"),
        "probe_usd": probe.get("usd"),
        "probe_max_loss_pct": probe.get("max_loss_pct"),
        "probe_shelf_range_pct": probe.get("shelf_range_pct"),
        "probe_break_min_pct": probe.get("break_min_pct"),
        "probe_break_max_pct": probe.get("break_max_pct"),
        "rebalance_pct": risk.get("rebalance_pct"),
        "cooldown_hours": risk.get("cooldown_hours"),
    }
    changes = {key: value for key, value in mapped.items() if value is not None}
    return merge_settings(DEFAULTS, **changes)


def as_plain(data: DeskSettings) -> dict[str, object]:
    return asdict(data)
