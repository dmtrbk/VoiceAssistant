"""Кэш /v5/market/instruments-info и округление price/qty."""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from typing import Any, Callable

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Instrument:
    symbol: str
    tick_size: float
    qty_step: float
    min_qty: float
    min_notional: float
    max_leverage: float = 0.0


def _floor_to_step(value: float, step: float) -> float:
    if step <= 0:
        return value
    n = math.floor(value / step + 1e-12)
    return n * step


def _decimals(step: float) -> int:
    s = f"{step:.16f}".rstrip("0")
    if "." not in s:
        return 0
    return len(s.split(".")[1])


class InstrumentCache:
    def __init__(self, public_get: Callable[[str, dict[str, Any] | None], dict[str, Any]]) -> None:
        self._get = public_get
        self._cache: dict[str, Instrument] = {}
        self._ts = 0.0

    def refresh(self, symbols: list[str] | tuple[str, ...] | None = None) -> None:
        data = self._get("/v5/market/instruments-info", {"category": "linear", "limit": 1000})
        rows = (data.get("result") or {}).get("list") or []
        want = {s.upper() for s in (symbols or [])} if symbols else None
        for row in rows:
            sym = str(row.get("symbol") or "").upper()
            if want is not None and sym not in want:
                continue
            if str(row.get("status") or "") != "Trading":
                continue
            pf = row.get("priceFilter") or {}
            lf = row.get("lotSizeFilter") or {}
            lev = row.get("leverageFilter") or {}
            self._cache[sym] = Instrument(
                symbol=sym,
                tick_size=float(pf.get("tickSize") or 0.01),
                qty_step=float(lf.get("qtyStep") or 0.001),
                min_qty=float(lf.get("minOrderQty") or 0.0),
                min_notional=float(lf.get("minNotionalValue") or 0.0),
                max_leverage=float(lev.get("maxLeverage") or 0.0),
            )
        self._ts = time.time()
        logger.info("[linear] instruments cached: %d", len(self._cache))

    def get(self, symbol: str) -> Instrument:
        sym = symbol.upper()
        if sym not in self._cache:
            self.refresh([sym])
        inst = self._cache.get(sym)
        if inst is None:
            raise RuntimeError(f"instrument missing: {sym}")
        return inst

    def round_price(self, symbol: str, price: float, *, side: str, passive: bool) -> float:
        """Округление цены. passive long → вниз (ниже рынка), short → вверх."""
        inst = self.get(symbol)
        tick = inst.tick_size
        if side.lower() in {"buy", "long"}:
            # buy limit: floor
            px = _floor_to_step(price, tick)
            if not passive:
                # aggressive would ceil — we never cross for PostOnly
                px = _floor_to_step(price, tick)
        else:
            # sell limit: ceil for staying above? PostOnly short = ask side → ceil
            n = math.ceil(price / tick - 1e-12)
            px = n * tick
            if passive and px < price:
                px = (math.floor(price / tick) + 1) * tick
        return float(f"{px:.{_decimals(tick)}f}")

    def round_qty(self, symbol: str, qty: float) -> float:
        inst = self.get(symbol)
        q = _floor_to_step(qty, inst.qty_step)
        if q < inst.min_qty:
            return 0.0
        return float(f"{q:.{_decimals(inst.qty_step)}f}")

    def min_usd(self, symbol: str, last: float) -> float:
        inst = self.get(symbol)
        return max(inst.min_notional, inst.min_qty * last if last > 0 else 0.0)
