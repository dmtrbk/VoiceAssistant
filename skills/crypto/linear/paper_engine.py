"""Paper-симулятор поверх живых данных Bybit (S1 fill, funding, fees)."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from .config import LinearConfig
from .journals import LinearJournals
from .portfolio import PortfolioGuard
from .strategy_core import Signal, limit_filled, size_leg_usd

logger = logging.getLogger(__name__)

FUNDING_HOURS_UTC = (0, 8, 16)


@dataclass
class PaperPending:
    signal: Signal
    limit_px: float
    qty: float
    leg_usd: float
    created_bar_ts: int
    expire_after_bars: int
    bars_lived: int = 0
    crossed_at_place: bool = False


@dataclass
class PaperPos:
    signal: Signal
    side: str
    qty: float
    entry: float
    sl: float
    tp: float
    leg_usd: float
    fees: float = 0.0
    funding: float = 0.0
    slip: float = 0.0
    open_ts: int = 0


@dataclass
class PaperBook:
    pending: dict[str, PaperPending] = field(default_factory=dict)
    positions: dict[str, PaperPos] = field(default_factory=dict)
    realized: float = 0.0
    last_funding_slot: str = ""


class PaperEngine:
    def __init__(
        self,
        cfg: LinearConfig,
        portfolio: PortfolioGuard,
        journals: LinearJournals,
        *,
        shadow: bool = False,
        min_usd_map: dict[str, float] | None = None,
    ) -> None:
        self.cfg = cfg
        self.portfolio = portfolio
        self.journals = journals
        self.shadow = shadow
        self.min_usd_map = min_usd_map or {}
        self.book = PaperBook()
        self.equity = cfg.start_equity_usd

    def on_signal(
        self,
        sig: Signal,
        *,
        bbo_bid: float,
        bbo_ask: float,
        last: float,
    ) -> None:
        ok, why = self.portfolio.can_add_entry(sig.symbol)
        if not ok:
            self.journals.event(
                {"type": "signal_blocked", "symbol": sig.symbol, "why": why, "shadow": self.shadow}
            )
            return
        # PostOnly honesty: if already crossed, reprice or skip
        limit_px = sig.limit_px
        crossed = (sig.side == "long" and bbo_ask > 0 and bbo_ask <= limit_px) or (
            sig.side == "short" and bbo_bid > 0 and bbo_bid >= limit_px
        )
        if crossed:
            if self.cfg.postonly_reject_policy == "skip":
                self.journals.event(
                    {"type": "postonly_skip", "symbol": sig.symbol, "limit": limit_px, "shadow": self.shadow}
                )
                return
            # 1 tick passive — без instruments используем 0.02% как прокси тика если нет
            tick = max(last * 1e-4, 1e-8)
            if sig.side == "long":
                limit_px = min(limit_px, bbo_bid - tick)
            else:
                limit_px = max(limit_px, bbo_ask + tick)

        min_usd = float(self.min_usd_map.get(sig.symbol, 0.0))
        leg = size_leg_usd(
            entry=limit_px,
            sl=sig.sl,
            fixed_leg=self.cfg.leg_usd,
            risk_clamp=self.cfg.risk_clamp,
            target_risk_usd=self.cfg.target_risk_usd,
            leg_min=self.cfg.leg_min_usd,
            leg_max=self.cfg.leg_max_usd,
            min_usd=min_usd,
        )
        qty = leg / limit_px if limit_px > 0 else 0.0
        if qty <= 0:
            return
        pend = PaperPending(
            signal=sig,
            limit_px=limit_px,
            qty=qty,
            leg_usd=leg,
            created_bar_ts=sig.bar_ts,
            expire_after_bars=self.cfg.entry_ttl_bars_15m,
            crossed_at_place=crossed,
        )
        self.book.pending[sig.symbol] = pend
        self.portfolio.set_pending(sig.symbol, {"limit": limit_px, "bar_ts": sig.bar_ts})
        self.journals.signal(
            {
                "symbol": sig.symbol,
                "side": sig.side,
                "bar_ts": sig.bar_ts,
                "limit_px": limit_px,
                "sl": sig.sl,
                "tp": sig.tp,
                "risk_px": sig.risk_px,
                "atr15": sig.atr15,
                "er_4h": sig.er_4h,
                "leg_usd": leg,
            },
            shadow=self.shadow,
        )

    def on_public_trade(self, symbol: str, price: float, ts_ms: int) -> None:
        """S1: fill только при пробитии limit на >= fill_through_pct."""
        pend = self.book.pending.get(symbol.upper())
        if not pend:
            return
        side = pend.signal.side
        through = float(self.cfg.fill_through_pct)
        if side == "long":
            hit = price < pend.limit_px * (1.0 - through)
        else:
            hit = price > pend.limit_px * (1.0 + through)
        if not hit:
            return
        self._fill_entry(pend, fill_px=pend.limit_px, ts_ms=ts_ms)

    def expire_pendings_at_bar(self, closed_bar_ts: int) -> list[str]:
        """Снять pending с TTL до оценки новых сигналов.

        bars_since_created = (closed_bar_ts - created_bar_ts) / 15m.
        При >= entry_ttl_bars_15m (по умолчанию 2) — cancel и освобождение слота.
        """
        cancelled: list[str] = []
        period = 15 * 60_000
        for sym, pend in list(self.book.pending.items()):
            if closed_bar_ts <= pend.created_bar_ts:
                continue
            bars_since = (int(closed_bar_ts) - int(pend.created_bar_ts)) // period
            pend.bars_lived = int(bars_since)
            if bars_since >= int(pend.expire_after_bars):
                self.book.pending.pop(sym, None)
                self.portfolio.clear_pending(sym)
                cancelled.append(sym)
                self.journals.event(
                    {
                        "type": "ttl_cancel",
                        "symbol": sym,
                        "shadow": self.shadow,
                        "created_bar_ts": pend.created_bar_ts,
                        "closed_bar_ts": closed_bar_ts,
                        "bars_since": bars_since,
                    }
                )
        return cancelled

    def on_bar_close_15m(self, symbol: str, bar: dict[str, float]) -> None:
        """OHLC S1 fill + SL/TP. TTL снимается отдельно через expire_pendings_at_bar."""
        sym = symbol.upper()
        pend = self.book.pending.get(sym)
        if pend:
            if limit_filled(
                pend.signal.side,
                pend.limit_px,
                low=float(bar["low"]),
                high=float(bar["high"]),
                through_pct=float(self.cfg.fill_through_pct),
            ):
                self._fill_entry(pend, fill_px=pend.limit_px, ts_ms=int(bar.get("ts") or 0))

        pos = self.book.positions.get(sym)
        if not pos:
            return
        lo, hi = float(bar["low"]), float(bar["high"])
        hit_sl = (pos.side == "long" and lo <= pos.sl) or (pos.side == "short" and hi >= pos.sl)
        hit_tp = (pos.side == "long" and hi >= pos.tp) or (pos.side == "short" and lo <= pos.tp)
        if hit_sl:
            exit_px = pos.sl * (1 - self.cfg.slip_exit) if pos.side == "long" else pos.sl * (1 + self.cfg.slip_exit)
            self._close(pos, exit_px, int(bar["ts"]), "sl", taker=True)
        elif hit_tp:
            self._close(pos, pos.tp, int(bar["ts"]), "tp", taker=False)

    def on_funding(self, symbol: str, rate: float, slot: str) -> None:
        if slot == self.book.last_funding_slot:
            return
        # применяем ко всем позициям символа; slot глобальный
        for sym, pos in list(self.book.positions.items()):
            if symbol and sym != symbol.upper():
                continue
            notion = pos.entry * pos.qty
            cash = -notion * rate if pos.side == "long" else notion * rate
            pos.funding += cash
        self.book.last_funding_slot = slot

    def maybe_funding_tick(self, now_ms: int | None = None) -> None:
        now = time.gmtime((now_ms or int(time.time() * 1000)) / 1000)
        if now.tm_hour in FUNDING_HOURS_UTC and now.tm_min == 0:
            slot = f"{now.tm_year}-{now.tm_yday}-{now.tm_hour}"
            # rate unknown here — desk should pass ticker fundingRate
            _ = slot

    def _fill_entry(self, pend: PaperPending, *, fill_px: float, ts_ms: int) -> None:
        sym = pend.signal.symbol
        fee = abs(fill_px * pend.qty) * self.cfg.maker_fee
        pos = PaperPos(
            signal=pend.signal,
            side=pend.signal.side,
            qty=pend.qty,
            entry=fill_px,
            sl=pend.signal.sl,
            tp=pend.signal.tp,
            leg_usd=pend.leg_usd,
            fees=fee,
            open_ts=ts_ms,
        )
        self.book.pending.pop(sym, None)
        self.book.positions[sym] = pos
        self.portfolio.set_open(sym, {"entry": fill_px, "qty": pend.qty})
        self.journals.trade(
            {
                "event": "open",
                "symbol": sym,
                "side": pos.side,
                "entry": fill_px,
                "qty": pend.qty,
                "sl": pos.sl,
                "tp": pos.tp,
                "leg_usd": pend.leg_usd,
                "fee": fee,
                "bar_ts": pend.signal.bar_ts,
            },
            shadow=self.shadow,
        )

    def _close(self, pos: PaperPos, exit_px: float, ts_ms: int, reason: str, *, taker: bool) -> None:
        fee_rate = self.cfg.taker_fee if taker else self.cfg.maker_fee
        fee = abs(exit_px * pos.qty) * fee_rate
        slip = abs(exit_px - pos.sl) * pos.qty if reason == "sl" else 0.0
        if pos.side == "long":
            pnl = (exit_px - pos.entry) * pos.qty
        else:
            pnl = (pos.entry - exit_px) * pos.qty
        pnl_net = pnl - fee - slip + pos.funding - pos.fees
        self.book.realized += pnl_net
        self.equity = self.cfg.start_equity_usd + self.book.realized
        self.portfolio.register_realized(pnl_net)
        self.portfolio.update_equity(self.equity)
        self.portfolio.clear_open(pos.signal.symbol)
        self.book.positions.pop(pos.signal.symbol, None)
        self.journals.trade(
            {
                "event": "close",
                "symbol": pos.signal.symbol,
                "side": pos.side,
                "entry": pos.entry,
                "exit": exit_px,
                "qty": pos.qty,
                "pnl": pnl_net,
                "fees": pos.fees + fee,
                "funding": pos.funding,
                "slip": slip,
                "reason": reason,
                "bar_ts": pos.signal.bar_ts,
                "exit_ts": ts_ms,
            },
            shadow=self.shadow,
        )
