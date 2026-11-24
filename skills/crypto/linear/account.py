"""One-Way + Isolated 2x, доступный баланс, резерв под Earn."""

from __future__ import annotations

import logging
from typing import Any

from .bybit_rest import BybitRest
from .config import LinearConfig

logger = logging.getLogger(__name__)


class AccountGuard:
    def __init__(self, rest: BybitRest, cfg: LinearConfig) -> None:
        self.rest = rest
        self.cfg = cfg

    def ensure_modes(self, symbols: list[str] | tuple[str, ...]) -> None:
        """Проверить/выставить Isolated + leverage; positionIdx=0 (One-Way)."""
        if self.cfg.paper:
            logger.info("[linear] paper: skip set-leverage / margin mode")
            return
        for sym in symbols:
            try:
                # Isolated margin mode (UTA): switch-isolated
                self.rest.signed(
                    "POST",
                    "/v5/position/switch-isolated",
                    {
                        "category": "linear",
                        "symbol": sym,
                        "tradeMode": 1,  # 1 isolated, 0 cross
                        "buyLeverage": str(self.cfg.leverage),
                        "sellLeverage": str(self.cfg.leverage),
                    },
                )
            except Exception as exc:
                logger.warning("[linear] switch-isolated %s: %s", sym, exc)
            try:
                self.rest.signed(
                    "POST",
                    "/v5/position/set-leverage",
                    {
                        "category": "linear",
                        "symbol": sym,
                        "buyLeverage": str(self.cfg.leverage),
                        "sellLeverage": str(self.cfg.leverage),
                    },
                )
            except Exception as exc:
                logger.warning("[linear] set-leverage %s: %s", sym, exc)
        logger.info(
            "[linear] account mode: One-Way positionIdx=%s Isolated %sx; SL triggerBy=%s",
            self.cfg.position_idx,
            self.cfg.leverage,
            self.cfg.sl_trigger_by,
        )

    def wallet_snapshot(self) -> dict[str, Any]:
        if self.cfg.paper:
            return {
                "totalAvailableBalance": self.cfg.start_equity_usd,
                "totalEquity": self.cfg.start_equity_usd,
                "totalWalletBalance": self.cfg.start_equity_usd,
            }
        data = self.rest.signed(
            "GET",
            "/v5/account/wallet-balance",
            {"accountType": "UNIFIED"},
        )
        lst = (data.get("result") or {}).get("list") or []
        if not lst:
            return {}
        acc = lst[0]
        return {
            "totalAvailableBalance": float(acc.get("totalAvailableBalance") or 0),
            "totalEquity": float(acc.get("totalEquity") or 0),
            "totalWalletBalance": float(acc.get("totalWalletBalance") or 0),
        }

    def can_place_order(self, notional_usd: float) -> tuple[bool, str]:
        snap = self.wallet_snapshot()
        avail = float(snap.get("totalAvailableBalance") or 0)
        # маржа ≈ notional / leverage; плюс резерв UTA
        margin_need = notional_usd / max(1, self.cfg.leverage)
        if avail < margin_need + 1.0:
            return False, f"available {avail:.2f} < margin {margin_need:.2f}"
        # торговый резерв: не уводить UTA ниже reserve после маржи
        if avail - margin_need < self.cfg.uta_trade_reserve_usd * 0.15:
            # мягкая проверка: если available уже ниже reserve — блок
            pass
        if avail < self.cfg.uta_trade_reserve_usd * 0.5 and not self.cfg.paper:
            return False, f"UTA available {avail:.2f} below half reserve {self.cfg.uta_trade_reserve_usd}"
        return True, "ok"

    def earn_respect_reserve(self) -> float:
        """Сколько USDT можно парковать в Earn, не трогая торговый резерв."""
        snap = self.wallet_snapshot()
        avail = float(snap.get("totalAvailableBalance") or 0)
        return max(0.0, avail - self.cfg.uta_trade_reserve_usd)
