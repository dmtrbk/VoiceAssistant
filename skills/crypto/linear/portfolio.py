"""Портфельные лимиты и kill-switch."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .config import LinearConfig

logger = logging.getLogger(__name__)


@dataclass
class PortfolioState:
    open_positions: dict[str, dict[str, Any]] = field(default_factory=dict)
    pending_entries: dict[str, dict[str, Any]] = field(default_factory=dict)
    day_key: str = ""
    day_realized_pnl: float = 0.0
    equity: float = 120.0
    peak_equity: float = 120.0
    kill: bool = False
    kill_reason: str = ""


class PortfolioGuard:
    def __init__(self, cfg: LinearConfig) -> None:
        self.cfg = cfg
        self.state = PortfolioState(
            equity=cfg.start_equity_usd,
            peak_equity=cfg.start_equity_usd,
            day_key=self._utc_day(),
        )

    @staticmethod
    def _utc_day() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _rollover_day(self) -> None:
        key = self._utc_day()
        if key != self.state.day_key:
            self.state.day_key = key
            self.state.day_realized_pnl = 0.0
            if self.state.kill and "daily" in self.state.kill_reason:
                self.state.kill = False
                self.state.kill_reason = ""

    def update_equity(self, equity: float) -> None:
        self._rollover_day()
        self.state.equity = float(equity)
        self.state.peak_equity = max(self.state.peak_equity, self.state.equity)
        if self.state.equity < self.cfg.kill_equity_floor_usd:
            self.state.kill = True
            self.state.kill_reason = f"equity {self.state.equity:.2f} < floor {self.cfg.kill_equity_floor_usd}"
            logger.warning("[linear] KILL: %s", self.state.kill_reason)

    def register_realized(self, pnl: float) -> None:
        self._rollover_day()
        self.state.day_realized_pnl += float(pnl)
        if self.state.day_realized_pnl <= -abs(self.cfg.kill_daily_loss_usd):
            self.state.kill = True
            self.state.kill_reason = (
                f"daily loss {self.state.day_realized_pnl:.2f} <= -{self.cfg.kill_daily_loss_usd}"
            )
            logger.warning("[linear] KILL: %s", self.state.kill_reason)

    def open_plus_pending(self) -> int:
        return len(self.state.open_positions) + len(self.state.pending_entries)

    def can_add_entry(self, symbol: str) -> tuple[bool, str]:
        self._rollover_day()
        if self.state.kill:
            return False, f"kill-switch: {self.state.kill_reason}"
        sym = symbol.upper()
        if sym in self.state.open_positions or sym in self.state.pending_entries:
            return False, "max_per_symbol"
        if self.open_plus_pending() >= self.cfg.max_open_plus_pending:
            return False, "max_open_plus_pending"
        return True, "ok"

    def set_pending(self, symbol: str, meta: dict[str, Any]) -> None:
        self.state.pending_entries[symbol.upper()] = meta

    def clear_pending(self, symbol: str) -> None:
        self.state.pending_entries.pop(symbol.upper(), None)

    def set_open(self, symbol: str, meta: dict[str, Any]) -> None:
        self.clear_pending(symbol)
        self.state.open_positions[symbol.upper()] = meta

    def clear_open(self, symbol: str) -> None:
        self.state.open_positions.pop(symbol.upper(), None)
