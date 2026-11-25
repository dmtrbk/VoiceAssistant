"""Live-исполнение: PostOnly вход, TTL, trading-stop SL, Maker reduceOnly TP, reconcile."""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from .account import AccountGuard
from .bybit_rest import BybitRest
from .config import LinearConfig
from .instruments import InstrumentCache
from .strategy_core import Signal, size_leg_usd

logger = logging.getLogger(__name__)


@dataclass
class LiveOrderState:
    symbol: str
    side: str  # Buy/Sell
    signal: Signal
    entry_order_id: str = ""
    entry_link_id: str = ""
    limit_px: float = 0.0
    qty: float = 0.0
    filled_qty: float = 0.0
    created_bar_ts: int = 0
    expire_bar_ts: int = 0  # open ts of last valid bar (created + ttl)
    tp_order_id: str = ""
    sl_set: bool = False
    status: str = "pending"  # pending|open|closed|cancelled


class LinearExecutor:
    def __init__(
        self,
        rest: BybitRest,
        cfg: LinearConfig,
        instruments: InstrumentCache,
        account: AccountGuard,
    ) -> None:
        self.rest = rest
        self.cfg = cfg
        self.instruments = instruments
        self.account = account
        self.orders: dict[str, LiveOrderState] = {}

    def _link(self, prefix: str) -> str:
        return f"{prefix}-{uuid.uuid4().hex[:16]}"

    def place_entry_postonly(self, sig: Signal, *, bar_ts: int, bbo_bid: float, bbo_ask: float) -> LiveOrderState | None:
        if self.cfg.paper:
            raise RuntimeError("use paper engine for paper mode")
        side = "Buy" if sig.side == "long" else "Sell"
        last = (bbo_bid + bbo_ask) / 2.0 if bbo_bid and bbo_ask else sig.limit_px
        min_usd = self.instruments.min_usd(sig.symbol, last)
        leg = size_leg_usd(
            entry=sig.limit_px,
            sl=sig.sl,
            fixed_leg=self.cfg.leg_usd,
            risk_clamp=self.cfg.risk_clamp,
            target_risk_usd=self.cfg.target_risk_usd,
            leg_min=self.cfg.leg_min_usd,
            leg_max=self.cfg.leg_max_usd,
            min_usd=min_usd,
        )
        ok, why = self.account.can_place_order(leg)
        if not ok:
            logger.info("[linear] skip entry %s: %s", sig.symbol, why)
            return None

        limit_px = self.instruments.round_price(sig.symbol, sig.limit_px, side=side, passive=True)
        # если рынок уже за лимитом — PostOnly отклонят
        crossed = (sig.side == "long" and bbo_ask > 0 and bbo_ask <= limit_px) or (
            sig.side == "short" and bbo_bid > 0 and bbo_bid >= limit_px
        )
        if crossed:
            if self.cfg.postonly_reject_policy == "skip":
                logger.info("[linear] PostOnly skip (crossed) %s px=%.6g", sig.symbol, limit_px)
                return None
            # reprice 1 tick passive from BBO
            inst = self.instruments.get(sig.symbol)
            if sig.side == "long":
                limit_px = self.instruments.round_price(
                    sig.symbol, bbo_bid - inst.tick_size, side="Buy", passive=True
                )
            else:
                limit_px = self.instruments.round_price(
                    sig.symbol, bbo_ask + inst.tick_size, side="Sell", passive=True
                )
            logger.info("[linear] PostOnly reprice 1tick %s → %.6g", sig.symbol, limit_px)

        qty = self.instruments.round_qty(sig.symbol, leg / limit_px)
        if qty <= 0:
            logger.info("[linear] qty too small %s", sig.symbol)
            return None

        link = self._link("le")
        body = {
            "category": "linear",
            "symbol": sig.symbol,
            "side": side,
            "orderType": "Limit",
            "qty": str(qty),
            "price": str(limit_px),
            "timeInForce": "PostOnly",
            "positionIdx": self.cfg.position_idx,
            "orderLinkId": link,
        }
        try:
            data = self.rest.signed("POST", "/v5/order/create", body)
        except Exception as exc:
            msg = str(exc)
            logger.warning("[linear] PostOnly create failed %s: %s", sig.symbol, msg)
            return None
        oid = str((data.get("result") or {}).get("orderId") or "")
        st = LiveOrderState(
            symbol=sig.symbol,
            side=side,
            signal=sig,
            entry_order_id=oid,
            entry_link_id=link,
            limit_px=limit_px,
            qty=qty,
            created_bar_ts=bar_ts,
            expire_bar_ts=bar_ts + self.cfg.entry_ttl_bars_15m * 15 * 60_000,
            status="pending",
        )
        self.orders[sig.symbol] = st
        return st

    def cancel_order(self, order_id: str, symbol: str) -> None:
        if not order_id:
            return
        try:
            self.rest.signed(
                "POST",
                "/v5/order/cancel",
                {"category": "linear", "symbol": symbol, "orderId": order_id},
            )
        except Exception as exc:
            logger.warning("[linear] cancel %s: %s", order_id, exc)

    def protect_position(self, st: LiveOrderState, filled_qty: float) -> None:
        """SL via trading-stop + Maker TP reduceOnly."""
        qty = self.instruments.round_qty(st.symbol, filled_qty)
        if qty <= 0:
            return
        sig = st.signal
        sl = self.instruments.round_price(
            st.symbol, sig.sl, side="Sell" if sig.side == "long" else "Buy", passive=True
        )
        tp = self.instruments.round_price(
            st.symbol, sig.tp, side="Sell" if sig.side == "long" else "Buy", passive=True
        )
        # native SL
        try:
            self.rest.signed(
                "POST",
                "/v5/position/trading-stop",
                {
                    "category": "linear",
                    "symbol": st.symbol,
                    "positionIdx": self.cfg.position_idx,
                    "stopLoss": str(sl),
                    "slTriggerBy": self.cfg.sl_trigger_by,
                    "tpslMode": "Partial",  # TP отдельным ордером
                },
            )
            st.sl_set = True
            logger.info("[linear] SL set %s %s triggerBy=%s", st.symbol, sl, self.cfg.sl_trigger_by)
        except Exception as exc:
            logger.error("[linear] trading-stop SL failed %s: %s", st.symbol, exc)

        # Maker TP reduceOnly
        tp_side = "Sell" if sig.side == "long" else "Buy"
        link = self._link("tp")
        try:
            data = self.rest.signed(
                "POST",
                "/v5/order/create",
                {
                    "category": "linear",
                    "symbol": st.symbol,
                    "side": tp_side,
                    "orderType": "Limit",
                    "qty": str(qty),
                    "price": str(tp),
                    "timeInForce": "GTC",
                    "reduceOnly": True,
                    "positionIdx": self.cfg.position_idx,
                    "orderLinkId": link,
                },
            )
            st.tp_order_id = str((data.get("result") or {}).get("orderId") or "")
            logger.info("[linear] Maker TP %s %s qty=%s", st.symbol, tp, qty)
        except Exception as exc:
            logger.error("[linear] Maker TP failed %s: %s", st.symbol, exc)

        st.filled_qty = qty
        st.status = "open"

    def on_ttl_expire(self, symbol: str) -> None:
        st = self.orders.get(symbol.upper())
        if not st or st.status != "pending":
            return
        filled = self._query_filled(st)
        self.cancel_order(st.entry_order_id, st.symbol)
        if filled > 0:
            self.protect_position(st, filled)
        else:
            st.status = "cancelled"
            self.orders.pop(symbol.upper(), None)
            logger.info("[linear] live TTL cancel %s", symbol)

    def expire_pendings_at_bar(self, closed_bar_ts: int) -> list[str]:
        """Снять live pending с TTL до новых сигналов (зеркало paper)."""
        cancelled: list[str] = []
        for sym, st in list(self.orders.items()):
            if st.status != "pending":
                continue
            if int(closed_bar_ts) >= int(st.expire_bar_ts):
                self.on_ttl_expire(sym)
                cancelled.append(sym)
        return cancelled

    def _query_filled(self, st: LiveOrderState) -> float:
        try:
            data = self.rest.signed(
                "GET",
                "/v5/order/realtime",
                {"category": "linear", "symbol": st.symbol, "orderId": st.entry_order_id},
            )
            lst = (data.get("result") or {}).get("list") or []
            if not lst:
                return float(st.filled_qty or 0)
            return float(lst[0].get("cumExecQty") or 0)
        except Exception:
            return float(st.filled_qty or 0)

    def reconcile(self) -> None:
        """Раз в минуту: снять TP если позиции нет; восстановить SL/TP если дырка."""
        if self.cfg.paper:
            return
        try:
            data = self.rest.signed(
                "GET",
                "/v5/position/list",
                {"category": "linear", "settleCoin": "USDT"},
            )
        except Exception as exc:
            logger.warning("[linear] position list: %s", exc)
            return
        pos_map: dict[str, float] = {}
        for row in (data.get("result") or {}).get("list") or []:
            sym = str(row.get("symbol") or "").upper()
            pos_map[sym] = abs(float(row.get("size") or 0))

        now_bar = (int(time.time() * 1000) // (15 * 60_000)) * (15 * 60_000)
        self.expire_pendings_at_bar(now_bar)

        for sym, st in list(self.orders.items()):
            size = pos_map.get(sym, 0.0)
            if st.status == "open" and size <= 0:
                # позиция закрыта → снять TP
                if st.tp_order_id:
                    self.cancel_order(st.tp_order_id, sym)
                st.status = "closed"
                self.orders.pop(sym, None)
                continue
            if st.status == "open" and size > 0:
                if not st.sl_set or not st.tp_order_id:
                    logger.warning("[linear] reconcile restore protect %s size=%s", sym, size)
                    self.protect_position(st, size)
