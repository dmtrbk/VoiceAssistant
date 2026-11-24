"""Конфигурация linear-контура (paper/live). Старый спот не трогаем."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from robotics_laws import CODE_GUIDANCE

_PROJECT = Path(__file__).resolve().parents[3]


@dataclass
class LinearConfig:
    """Рабочие параметры счёта и риска."""

    paper: bool = True
    testnet: bool = False

    # Core-2 по умолчанию; shadow_symbols — второй paper-портфель (Core-3b)
    symbols: tuple[str, ...] = ("ZECUSDT", "HYPEUSDT")
    shadow_symbols: tuple[str, ...] = ("ZECUSDT", "HYPEUSDT", "AAVEUSDT")
    shadow_enabled: bool = True
    btc_symbol: str = "BTCUSDT"

    # account (live)
    position_mode: str = "one_way"
    position_idx: int = 0
    margin_mode: str = "isolated"
    leverage: int = 2

    # sizing: paper default fixed $40; live may use risk_clamp
    sizing_mode: str = "fixed"  # fixed | risk_clamp
    leg_usd: float = 40.0
    target_risk_usd: float = 0.80
    leg_min_usd: float = 30.0
    leg_max_usd: float = 50.0
    max_open_plus_pending: int = 2
    max_per_symbol: int = 1

    uta_trade_reserve_usd: float = 120.0

    entry_ttl_bars_15m: int = 2
    fill_through_pct: float = 0.0002  # S1 paper fill
    postonly_reject_policy: str = "reprice_1tick"  # reprice_1tick | skip
    tp_r: float = 2.5
    sl_trigger_by: str = "MarkPrice"
    maker_fee: float = 0.0002
    taker_fee: float = 0.00055
    slip_exit: float = 0.0002

    kill_daily_loss_usd: float = 8.0
    kill_equity_floor_usd: float = 125.0
    start_equity_usd: float = 160.0

    # warm bars (not calendar days alone)
    warm_bars_15m: int = 4500
    warm_bars_1h: int = 1200
    warm_bars_4h: int = 300
    er4h_median_mode: str = "rolling_4320"

    runtime_dir: Path = field(
        default_factory=lambda: _PROJECT / "research_linear_data" / "linear_runtime"
    )
    laws_note: str = CODE_GUIDANCE

    @property
    def risk_clamp(self) -> bool:
        return self.sizing_mode == "risk_clamp"

    def path(self, name: str) -> Path:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        return self.runtime_dir / name


def default_config(*, paper: bool | None = None, sizing: str | None = None) -> LinearConfig:
    """Живые настройки из jarvis_linear_settings.json; env перекрывает."""
    from .settings_store import to_linear_config

    cfg = to_linear_config()
    env_paper = (os.getenv("LINEAR_PAPER") or "").strip().lower()
    if paper is not None:
        cfg.paper = bool(paper)
    elif env_paper in {"0", "false", "no", "off", "live"}:
        cfg.paper = False
    elif env_paper in {"1", "true", "yes", "on", "paper"}:
        cfg.paper = True
    if sizing:
        cfg.sizing_mode = sizing
    else:
        env_sizing = (os.getenv("LINEAR_SIZING") or "").strip()
        if env_sizing:
            cfg.sizing_mode = env_sizing
        elif not cfg.paper and cfg.sizing_mode == "fixed":
            # live без явного sizing — risk_clamp
            cfg.sizing_mode = "risk_clamp"
    raw = (os.getenv("LINEAR_SYMBOLS") or "").strip()
    if raw:
        cfg.symbols = tuple(s.strip().upper() for s in raw.split(",") if s.strip())
    shadow = (os.getenv("LINEAR_SHADOW") or "").strip().lower()
    if shadow in {"0", "false", "off", "no"}:
        cfg.shadow_enabled = False
    elif shadow in {"1", "true", "yes", "on"}:
        cfg.shadow_enabled = True
    cfg.runtime_dir.mkdir(parents=True, exist_ok=True)
    return cfg
