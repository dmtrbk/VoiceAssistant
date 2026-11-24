"""Оркестратор paper/live: warm → features → signal on 15m close → exec/paper."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from .account import AccountGuard
from .bybit_rest import BybitRest
from .config import LinearConfig, default_config
from .execution import LinearExecutor
from .instruments import InstrumentCache
from .journals import LinearJournals
from .paper_engine import PaperEngine
from .portfolio import PortfolioGuard
from .strategy_core import StrategyParams, build_features, signal_at_bar
from .stream_linear import LinearPublicTape
from .warm import warm_universe

logger = logging.getLogger(__name__)


class LinearDesk:
    def __init__(self, cfg: LinearConfig | None = None) -> None:
        self.cfg = cfg or default_config()
        self.rest = BybitRest(testnet=self.cfg.testnet)
        self.instruments = InstrumentCache(self.rest.public_get)
        self.account = AccountGuard(self.rest, self.cfg)
        self.portfolio = PortfolioGuard(self.cfg)
        self.journals = LinearJournals(self.cfg, prefix="paper" if self.cfg.paper else "live")
        self.executor = LinearExecutor(self.rest, self.cfg, self.instruments, self.account)
        self.tape = LinearPublicTape(testnet=self.cfg.testnet)
        self.params = StrategyParams(
            er4h_median_mode=self.cfg.er4h_median_mode,
            tp_r=self.cfg.tp_r,
        )
        self.ohlc: dict[str, dict[str, pd.DataFrame]] = {}
        self.features: dict[str, pd.DataFrame] = {}
        self.paper: PaperEngine | None = None
        self.shadow: PaperEngine | None = None
        self._stop = False
        self._last_reconcile = 0.0

    def start(self) -> None:
        logger.info(
            "[linear] start paper=%s symbols=%s shadow=%s sizing=%s | %s",
            self.cfg.paper,
            self.cfg.symbols,
            self.cfg.shadow_enabled,
            self.cfg.sizing_mode,
            self.cfg.laws_note[:80],
        )
        want = set(self.cfg.symbols) | {self.cfg.btc_symbol}
        if self.cfg.shadow_enabled:
            want |= set(self.cfg.shadow_symbols)
        self.instruments.refresh(sorted(want))
        if not self.cfg.paper:
            self.account.ensure_modes(self.cfg.symbols)

        logger.info("[linear] warming REST history…")
        self.ohlc = warm_universe(self.rest, self.cfg)
        self._rebuild_features()

        min_usd = {
            s: self.instruments.min_usd(s, float(self.ohlc[s]["15m"].iloc[-1]["close"]))
            for s in self.ohlc
            if s != self.cfg.btc_symbol and len(self.ohlc[s]["15m"])
        }
        if self.cfg.paper:
            self.paper = PaperEngine(self.cfg, self.portfolio, self.journals, min_usd_map=min_usd)
            if self.cfg.shadow_enabled:
                # отдельный portfolio для shadow, чтобы не блокировать Core-2
                shadow_port = PortfolioGuard(self.cfg)
                self.shadow = PaperEngine(
                    self.cfg,
                    shadow_port,
                    self.journals,
                    shadow=True,
                    min_usd_map=min_usd,
                )

        if self._stop:
            logger.info("[linear] stop during warm — выход")
            return

        start_eq = float(self.paper.equity) if self.paper else float(self.cfg.start_equity_usd)
        self.journals.session_start(equity=start_eq)
        self.journals.daily_snapshot(
            equity=start_eq,
            day_pnl=0.0,
            open_n=0,
            kill=False,
        )

        self.tape.set_symbols(want)
        self.tape.set_handlers(on_kline_closed=self._on_kline, on_trade=self._on_trade)
        self.tape.start()
        self._loop()

    def stop(self) -> None:
        self._stop = True
        self.tape.stop()

    def _rebuild_features(self) -> None:
        for sym, frames in self.ohlc.items():
            self.features[sym] = build_features(frames["15m"], frames["1h"], frames["4h"])

    def _append_bar(self, symbol: str, interval: str, bar: dict[str, Any]) -> None:
        df = self.ohlc[symbol][interval]
        row = {
            "ts": int(bar["ts"]),
            "open": float(bar["open"]),
            "high": float(bar["high"]),
            "low": float(bar["low"]),
            "close": float(bar["close"]),
            "volume": float(bar.get("volume") or 0),
            "turnover": float(bar.get("turnover") or 0),
        }
        # replace if same ts else append
        if len(df) and int(df.iloc[-1]["ts"]) == row["ts"]:
            df.iloc[-1] = row
        else:
            self.ohlc[symbol][interval] = pd.concat([df, pd.DataFrame([row])], ignore_index=True)
            # trim
            cap = {"15m": self.cfg.warm_bars_15m, "1h": self.cfg.warm_bars_1h, "4h": self.cfg.warm_bars_4h}[
                interval
            ]
            if len(self.ohlc[symbol][interval]) > cap + 50:
                self.ohlc[symbol][interval] = self.ohlc[symbol][interval].iloc[-cap:].reset_index(drop=True)

    def _on_trade(self, symbol: str, price: float, ts_ms: int) -> None:
        if self.paper:
            self.paper.on_public_trade(symbol, price, ts_ms)
        if self.shadow and symbol in self.cfg.shadow_symbols:
            self.shadow.on_public_trade(symbol, price, ts_ms)

    def _on_kline(self, symbol: str, bar: dict[str, Any]) -> None:
        if symbol not in self.ohlc:
            return
        self._append_bar(symbol, "15m", bar)
        # roll HTF if boundary
        ts = int(bar["ts"])
        if ts % 3_600_000 == 0:
            self._refresh_htf(symbol, "1h", "60")
        if ts % 14_400_000 == 0:
            self._refresh_htf(symbol, "4h", "240")
        self.features[symbol] = build_features(
            self.ohlc[symbol]["15m"],
            self.ohlc[symbol]["1h"],
            self.ohlc[symbol]["4h"],
        )
        if self.paper:
            self.paper.on_bar_close_15m(symbol, bar)
        if self.shadow and symbol in set(self.cfg.shadow_symbols) | {self.cfg.btc_symbol}:
            self.shadow.on_bar_close_15m(symbol, bar)

        # signal only for working / shadow symbols (not BTC alone)
        if symbol == self.cfg.btc_symbol:
            return
        self._emit_signal(symbol)

    def _refresh_htf(self, symbol: str, key: str, interval: str) -> None:
        try:
            from .warm import fetch_klines_paginated

            want = self.cfg.warm_bars_1h if key == "1h" else self.cfg.warm_bars_4h
            self.ohlc[symbol][key] = fetch_klines_paginated(self.rest, symbol, interval, want)
        except Exception as exc:
            logger.warning("[linear] refresh %s %s: %s", symbol, key, exc)

    def _emit_signal(self, symbol: str) -> None:
        df = self.features.get(symbol)
        btc = self.features.get(self.cfg.btc_symbol)
        if df is None or len(df) < 100:
            return
        i = len(df) - 1
        sig = signal_at_bar(symbol, df, i, btc, self.params)
        if sig is None:
            return
        bbo = self.tape.bbo.get(symbol) or {}
        bid = float(bbo.get("bid") or 0)
        ask = float(bbo.get("ask") or 0)
        last = float(self.tape.last.get(symbol) or sig.confirm_close)

        if symbol in self.cfg.symbols:
            if self.cfg.paper and self.paper:
                self.paper.on_signal(sig, bbo_bid=bid, bbo_ask=ask, last=last)
            elif not self.cfg.paper:
                self.executor.place_entry_postonly(sig, bar_ts=sig.bar_ts, bbo_bid=bid, bbo_ask=ask)

        if (
            self.cfg.shadow_enabled
            and self.shadow
            and symbol in self.cfg.shadow_symbols
        ):
            self.shadow.on_signal(sig, bbo_bid=bid, bbo_ask=ask, last=last)

    def _rest_confirm_poll(self) -> None:
        """Fallback: на HH:MM:02 UTC после :00/:15/:30/:45 проверить закрытие."""
        now = datetime.now(timezone.utc)
        if now.minute % 15 != 0 or now.second < 2 or now.second > 8:
            return
        # debounce via last bar ts
        for sym in set(self.cfg.symbols) | set(self.cfg.shadow_symbols) | {self.cfg.btc_symbol}:
            try:
                data = self.rest.public_get(
                    "/v5/market/kline",
                    {"category": "linear", "symbol": sym, "interval": "15", "limit": 2},
                )
                rows = data.get("result", {}).get("list") or []
                if len(rows) < 2:
                    continue
                # rows[0] newest possibly unclosed; rows[1] closed
                closed = rows[1]
                bar = {
                    "ts": int(closed[0]),
                    "open": float(closed[1]),
                    "high": float(closed[2]),
                    "low": float(closed[3]),
                    "close": float(closed[4]),
                    "volume": float(closed[5]),
                    "confirm": True,
                }
                cur = self.ohlc.get(sym, {}).get("15m")
                if cur is not None and len(cur) and int(cur.iloc[-1]["ts"]) == bar["ts"]:
                    continue
                self._on_kline(sym, bar)
            except Exception as exc:
                logger.warning("[linear] REST confirm %s: %s", sym, exc)

    def _funding_tick(self) -> None:
        now = datetime.now(timezone.utc)
        if now.hour not in (0, 8, 16) or now.minute != 0 or now.second > 15:
            return
        slot = f"{now.date()}-{now.hour}"
        for sym, rate in self.tape.funding.items():
            if self.paper:
                self.paper.on_funding(sym, rate, slot)
            if self.shadow:
                self.shadow.on_funding(sym, rate, slot)

    def _loop(self) -> None:
        logger.info("[linear] desk loop running")
        while not self._stop:
            self.tape.wait_kline(timeout=1.0)
            for sym, bar in self.tape.pop_closed():
                # already handled via callback if set; avoid double if callback used
                pass
            self._rest_confirm_poll()
            self._funding_tick()
            now = time.time()
            if now - self._last_reconcile >= 60:
                self._last_reconcile = now
                if not self.cfg.paper:
                    self.executor.reconcile()
                    # Earn: не уводить резерв
                    parkable = self.account.earn_respect_reserve()
                    self.journals.event({"type": "earn_reserve_free", "usd": parkable})
                snap = self.account.wallet_snapshot()
                eq = float(snap.get("totalEquity") or self.portfolio.state.equity)
                if self.cfg.paper and self.paper:
                    eq = self.paper.equity
                self.portfolio.update_equity(eq)
                # раз в час — снимок equity в daily jsonl
                utc = datetime.now(timezone.utc)
                if utc.minute == 0:
                    self.journals.daily_snapshot(
                        equity=eq,
                        day_pnl=self.portfolio.state.day_realized_pnl,
                        open_n=self.portfolio.open_plus_pending(),
                        kill=self.portfolio.state.kill,
                    )
            time.sleep(0.2)


def main_paper() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    desk = LinearDesk(default_config(paper=True))
    try:
        desk.start()
    except KeyboardInterrupt:
        desk.stop()
