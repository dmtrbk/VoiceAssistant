"""Live-исполнение: PostOnly вход, TTL, trading-stop SL, Maker reduceOnly TP, reconcile."""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any

from .account import AccountGuard
from .bybit_rest import BybitRest
from .config import LinearConfig
from .instruments import InstrumentCache
from .portfolio import PortfolioGuard
from .strategy_core import Signal, size_leg_usd

logger = logging.getLogger(__name__)

_15M = 15 * 60_000


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
    expire_bar_ts: int = 0  # created + ttl×15m; expire when closed_bar_ts >= this
    tp_order_id: str = ""
    sl_set: bool = False
    status: str = "pending"  # pending|open|closed|cancelled
    restored: bool = False  # поднято с биржи без локального signal-контекста


def closed_15m_bar_ts(now_ms: int | None = None) -> int:
    """Open-ts последнего полностью закрытого 15m бара (не текущего формирующегося)."""
    ms = int(now_ms if now_ms is not None else time.time() * 1000)
    return (ms // _15M - 1) * _15M


class LinearExecutor:
    def __init__(
        self,
        rest: BybitRest,
        cfg: LinearConfig,
        instruments: InstrumentCache,
        account: AccountGuard,
        portfolio: PortfolioGuard | None = None,
    ) -> None:
        self.rest = rest
        self.cfg = cfg
        self.instruments = instruments
        self.account = account
        self.portfolio = portfolio
        self.orders: dict[str, LiveOrderState] = {}
        self._lock = threading.RLock()

    def bind_portfolio(self, portfolio: PortfolioGuard) -> None:
        self.portfolio = portfolio

    def _link(self, prefix: str) -> str:
        return f"{prefix}-{uuid.uuid4().hex[:16]}"

    def _pf_ok(self, symbol: str) -> tuple[bool, str]:
        if self.portfolio is None:
            return True, "ok"
        return self.portfolio.can_add_entry(symbol)

    def place_entry_postonly(
        self, sig: Signal, *, bar_ts: int, bbo_bid: float, bbo_ask: float
    ) -> LiveOrderState | None:
        if self.cfg.paper:
            raise RuntimeError("use paper engine for paper mode")
        with self._lock:
            ok_pf, why_pf = self._pf_ok(sig.symbol)
            if not ok_pf:
                logger.info("[linear] live skip entry %s: portfolio %s", sig.symbol, why_pf)
                return None
            if sig.symbol.upper() in self.orders:
                st0 = self.orders[sig.symbol.upper()]
                if st0.status in ("pending", "open"):
                    logger.info("[linear] live skip entry %s: local %s", sig.symbol, st0.status)
                    return None

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
            crossed = (sig.side == "long" and bbo_ask > 0 and bbo_ask <= limit_px) or (
                sig.side == "short" and bbo_bid > 0 and bbo_bid >= limit_px
            )
            if crossed:
                if self.cfg.postonly_reject_policy == "skip":
                    logger.info("[linear] PostOnly skip (crossed) %s px=%.6g", sig.symbol, limit_px)
                    return None
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
                logger.warning("[linear] PostOnly create failed %s: %s", sig.symbol, exc)
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
                expire_bar_ts=bar_ts + self.cfg.entry_ttl_bars_15m * _15M,
                status="pending",
            )
            self.orders[sig.symbol.upper()] = st
            if self.portfolio is not None:
                self.portfolio.set_pending(sig.symbol, {"limit": limit_px, "bar_ts": bar_ts, "orderId": oid})
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
        """SL via trading-stop + Maker TP reduceOnly. Переводит pending→open."""
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
                    "tpslMode": "Partial",
                },
            )
            st.sl_set = True
            logger.info("[linear] SL set %s %s triggerBy=%s", st.symbol, sl, self.cfg.sl_trigger_by)
        except Exception as exc:
            logger.error("[linear] trading-stop SL failed %s: %s", st.symbol, exc)

        if not st.tp_order_id:
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
        if self.portfolio is not None:
            self.portfolio.set_open(st.symbol, {"entry": st.limit_px or sig.limit_px, "qty": qty})

    def on_ttl_expire(self, symbol: str) -> None:
        with self._lock:
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
                if self.portfolio is not None:
                    self.portfolio.clear_pending(symbol)
                logger.info("[linear] live TTL cancel %s", symbol)

    def expire_pendings_at_bar(self, closed_bar_ts: int) -> list[str]:
        """Снять live pending с TTL после OHLC-слота (зеркало paper)."""
        to_expire: list[str] = []
        with self._lock:
            for sym, st in list(self.orders.items()):
                if st.status != "pending":
                    continue
                if int(closed_bar_ts) >= int(st.expire_bar_ts):
                    to_expire.append(sym)
        for sym in to_expire:
            self.on_ttl_expire(sym)
        return to_expire

    def _query_order(self, st: LiveOrderState) -> dict[str, Any] | None:
        if not st.entry_order_id and not st.entry_link_id:
            return None
        params: dict[str, Any] = {"category": "linear", "symbol": st.symbol}
        if st.entry_order_id:
            params["orderId"] = st.entry_order_id
        else:
            params["orderLinkId"] = st.entry_link_id
        try:
            data = self.rest.signed("GET", "/v5/order/realtime", params)
            lst = (data.get("result") or {}).get("list") or []
            return lst[0] if lst else None
        except Exception as exc:
            logger.debug("[linear] order realtime %s: %s", st.symbol, exc)
            return None

    def _query_filled(self, st: LiveOrderState) -> float:
        row = self._query_order(st)
        if not row:
            return float(st.filled_qty or 0)
        return float(row.get("cumExecQty") or 0)

    def _fetch_positions(self) -> dict[str, dict[str, Any]]:
        data = self.rest.signed(
            "GET",
            "/v5/position/list",
            {"category": "linear", "settleCoin": "USDT"},
        )
        out: dict[str, dict[str, Any]] = {}
        for row in (data.get("result") or {}).get("list") or []:
            sym = str(row.get("symbol") or "").upper()
            size = abs(float(row.get("size") or 0))
            if sym and size > 0:
                out[sym] = row
        return out

    def _fetch_open_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"category": "linear", "settleCoin": "USDT"}
        if symbol:
            params = {"category": "linear", "symbol": symbol}
        try:
            data = self.rest.signed("GET", "/v5/order/realtime", params)
            return list((data.get("result") or {}).get("list") or [])
        except Exception as exc:
            logger.warning("[linear] open orders: %s", exc)
            return []

    @staticmethod
    def _signal_from_position(row: dict[str, Any]) -> Signal:
        sym = str(row.get("symbol") or "").upper()
        side_raw = str(row.get("side") or "")
        side = "long" if side_raw.lower() == "buy" else "short"
        entry = float(row.get("avgPrice") or row.get("entryPrice") or 0) or 1.0
        sl = float(row.get("stopLoss") or 0) or (entry * 0.98 if side == "long" else entry * 1.02)
        tp = float(row.get("takeProfit") or 0) or (entry * 1.05 if side == "long" else entry * 0.95)
        risk = abs(entry - sl) or entry * 0.02
        return Signal(
            symbol=sym,
            side=side,
            bar_ts=0,
            limit_px=entry,
            sl=sl,
            tp=tp,
            risk_px=risk,
            atr15=risk,
            er_4h=0.0,
            confirm_close=entry,
        )

    def _promote_pending_if_filled(self, st: LiveOrderState, pos_size: float) -> bool:
        """pending → open при fill или size>0. True если стал open."""
        if st.status != "pending":
            return st.status == "open"
        row = self._query_order(st)
        filled = float((row or {}).get("cumExecQty") or 0) if row else 0.0
        status = str((row or {}).get("orderStatus") or "")
        if pos_size > 0 or filled > 0 or status in {"Filled", "PartiallyFilled"}:
            qty = pos_size if pos_size > 0 else filled
            logger.info(
                "[linear] pending→open %s filled=%.6g pos=%.6g status=%s",
                st.symbol,
                filled,
                pos_size,
                status,
            )
            self.protect_position(st, qty)
            return True
        if status in {"Cancelled", "Rejected", "Deactivated"}:
            st.status = "cancelled"
            self.orders.pop(st.symbol.upper(), None)
            if self.portfolio is not None:
                self.portfolio.clear_pending(st.symbol)
            return False
        return False

    def on_private_order(self, row: dict[str, Any]) -> None:
        """Колбэк приватного WS topic=order — мгновенный protect после филла."""
        if self.cfg.paper:
            return
        sym = str(row.get("symbol") or "").upper()
        if not sym:
            return
        with self._lock:
            st = self.orders.get(sym)
            if st is None or st.status != "pending":
                return
            oid = str(row.get("orderId") or "")
            link = str(row.get("orderLinkId") or "")
            if st.entry_order_id and oid and oid != st.entry_order_id:
                if not (st.entry_link_id and link == st.entry_link_id):
                    return
            status = str(row.get("orderStatus") or "")
            filled = float(row.get("cumExecQty") or 0)
            if status in {"Filled", "PartiallyFilled"} and filled > 0:
                self.protect_position(st, filled)
            elif status in {"Cancelled", "Rejected", "Deactivated"} and filled <= 0:
                st.status = "cancelled"
                self.orders.pop(sym, None)
                if self.portfolio is not None:
                    self.portfolio.clear_pending(sym)

    def on_private_execution(self, row: dict[str, Any]) -> None:
        """topic=execution — страховка: любой fill по entry → protect."""
        if self.cfg.paper:
            return
        sym = str(row.get("symbol") or "").upper()
        link = str(row.get("orderLinkId") or "")
        oid = str(row.get("orderId") or "")
        with self._lock:
            st = self.orders.get(sym)
            if st is None or st.status != "pending":
                return
            if st.entry_order_id and oid and oid != st.entry_order_id:
                if not (st.entry_link_id and link == st.entry_link_id):
                    return
            filled = float(row.get("execQty") or 0) + float(st.filled_qty or 0)
            # cum из execution может быть куском — лучше перечитать
            qty = self._query_filled(st) or filled
            if qty > 0:
                self.protect_position(st, qty)

    def bootstrap_from_exchange(self, symbols: list[str] | tuple[str, ...]) -> None:
        """При старте live: восстановить позиции/ордера с биржи + protect если дырки."""
        if self.cfg.paper:
            return
        want = {s.upper() for s in symbols}
        try:
            pos_map = self._fetch_positions()
        except Exception as exc:
            logger.warning("[linear] bootstrap positions: %s", exc)
            return
        open_orders = self._fetch_open_orders()
        entry_by_sym: dict[str, dict[str, Any]] = {}
        tp_by_sym: dict[str, dict[str, Any]] = {}
        for o in open_orders:
            sym = str(o.get("symbol") or "").upper()
            if sym not in want:
                continue
            if bool(o.get("reduceOnly")):
                tp_by_sym[sym] = o
            else:
                # активный вход
                st_name = str(o.get("orderStatus") or "")
                if st_name in {"New", "PartiallyFilled", "Untriggered", "Created"}:
                    entry_by_sym[sym] = o

        with self._lock:
            for sym, row in pos_map.items():
                if sym not in want:
                    continue
                size = abs(float(row.get("size") or 0))
                if size <= 0:
                    continue
                sig = self._signal_from_position(row)
                # если в open orders есть reduceOnly — подхватить tp id
                tp_o = tp_by_sym.get(sym) or {}
                sl_raw = str(row.get("stopLoss") or "")
                st = LiveOrderState(
                    symbol=sym,
                    side="Buy" if sig.side == "long" else "Sell",
                    signal=sig,
                    limit_px=float(row.get("avgPrice") or sig.limit_px),
                    qty=size,
                    filled_qty=size,
                    created_bar_ts=0,
                    expire_bar_ts=0,
                    tp_order_id=str(tp_o.get("orderId") or ""),
                    sl_set=bool(sl_raw and sl_raw not in {"0", "0.0"}),
                    status="open",
                    restored=True,
                )
                self.orders[sym] = st
                if self.portfolio is not None:
                    self.portfolio.set_open(sym, {"entry": st.limit_px, "qty": size, "restored": True})
                if not st.sl_set or not st.tp_order_id:
                    logger.warning(
                        "[linear] bootstrap protect %s sl_set=%s tp=%s",
                        sym,
                        st.sl_set,
                        bool(st.tp_order_id),
                    )
                    self.protect_position(st, size)

            for sym, o in entry_by_sym.items():
                if sym in self.orders:
                    continue
                # pending вход без позиции
                side = str(o.get("side") or "Buy")
                px = float(o.get("price") or 0)
                qty = float(o.get("qty") or 0)
                sig_side = "long" if side == "Buy" else "short"
                sl = px * 0.98 if sig_side == "long" else px * 1.02
                tp = px * 1.05 if sig_side == "long" else px * 0.95
                sig = Signal(
                    symbol=sym,
                    side=sig_side,
                    bar_ts=0,
                    limit_px=px,
                    sl=sl,
                    tp=tp,
                    risk_px=abs(px - sl),
                    atr15=abs(px - sl),
                    er_4h=0.0,
                    confirm_close=px,
                )
                now_closed = closed_15m_bar_ts()
                st = LiveOrderState(
                    symbol=sym,
                    side=side,
                    signal=sig,
                    entry_order_id=str(o.get("orderId") or ""),
                    entry_link_id=str(o.get("orderLinkId") or ""),
                    limit_px=px,
                    qty=qty,
                    created_bar_ts=now_closed,
                    expire_bar_ts=now_closed + self.cfg.entry_ttl_bars_15m * _15M,
                    status="pending",
                    restored=True,
                )
                self.orders[sym] = st
                if self.portfolio is not None:
                    self.portfolio.set_pending(sym, {"limit": px, "restored": True})
                logger.info("[linear] bootstrap pending %s oid=%s", sym, st.entry_order_id)

        logger.info("[linear] bootstrap done: local orders=%d", len(self.orders))

    def reconcile(self) -> None:
        """Раз в минуту: TTL по closed_bar, pending→open, restore SL/TP, sync portfolio/equity."""
        if self.cfg.paper:
            return
        try:
            pos_map = self._fetch_positions()
        except Exception as exc:
            logger.warning("[linear] position list: %s", exc)
            return

        closed_ts = closed_15m_bar_ts()
        self.expire_pendings_at_bar(closed_ts)

        with self._lock:
            for sym, st in list(self.orders.items()):
                row = pos_map.get(sym)
                size = abs(float((row or {}).get("size") or 0)) if row else 0.0

                if st.status == "pending":
                    self._promote_pending_if_filled(st, size)
                    continue

                if st.status == "open":
                    if size <= 0:
                        if st.tp_order_id:
                            self.cancel_order(st.tp_order_id, sym)
                        st.status = "closed"
                        self.orders.pop(sym, None)
                        if self.portfolio is not None:
                            # realized неизвестен точно без closedPnl — слот освобождаем
                            self.portfolio.clear_open(sym)
                        logger.info("[linear] reconcile closed %s", sym)
                        continue
                    sl_raw = str((row or {}).get("stopLoss") or "")
                    exchange_sl = bool(sl_raw and sl_raw not in {"0", "0.0"})
                    st.sl_set = st.sl_set or exchange_sl
                    # проверить reduceOnly TP среди открытых ордеров
                    if not st.tp_order_id:
                        for o in self._fetch_open_orders(sym):
                            if bool(o.get("reduceOnly")):
                                st.tp_order_id = str(o.get("orderId") or "")
                                break
                    if not st.sl_set or not st.tp_order_id:
                        logger.warning(
                            "[linear] reconcile restore protect %s size=%s sl=%s tp=%s",
                            sym,
                            size,
                            st.sl_set,
                            bool(st.tp_order_id),
                        )
                        self.protect_position(st, size)

            # позиции на бирже, которых нет локально
            want = {s.upper() for s in self.cfg.symbols}
            for sym, row in pos_map.items():
                if sym not in want or sym in self.orders:
                    continue
                size = abs(float(row.get("size") or 0))
                if size <= 0:
                    continue
                logger.warning("[linear] reconcile orphan position %s size=%s — restore", sym, size)
                sig = self._signal_from_position(row)
                st = LiveOrderState(
                    symbol=sym,
                    side="Buy" if sig.side == "long" else "Sell",
                    signal=sig,
                    limit_px=float(row.get("avgPrice") or sig.limit_px),
                    qty=size,
                    filled_qty=size,
                    status="open",
                    restored=True,
                    sl_set=bool(str(row.get("stopLoss") or "") not in {"", "0", "0.0"}),
                )
                self.orders[sym] = st
                if self.portfolio is not None:
                    self.portfolio.set_open(sym, {"entry": st.limit_px, "qty": size, "orphan": True})
                self.protect_position(st, size)

        # equity / kill из кошелька
        if self.portfolio is not None:
            try:
                snap = self.account.wallet_snapshot()
                eq = float(snap.get("totalEquity") or self.portfolio.state.equity)
                self.portfolio.update_equity(eq)
            except Exception as exc:
                logger.debug("[linear] reconcile equity: %s", exc)
