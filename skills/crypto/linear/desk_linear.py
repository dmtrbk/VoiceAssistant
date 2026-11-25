"""Оркестратор paper/live: warm → features → signal on 15m close → exec/paper."""

from __future__ import annotations

import logging
import threading
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

_BAR_MS = {"15m": 15 * 60_000, "1h": 60 * 60_000, "4h": 4 * 60 * 60_000}
_15M = _BAR_MS["15m"]


class LinearDesk:
    def __init__(self, cfg: LinearConfig | None = None) -> None:
        self.cfg = cfg or default_config()
        self.rest = BybitRest(testnet=self.cfg.testnet)
        self.instruments = InstrumentCache(self.rest.public_get)
        self.account = AccountGuard(self.rest, self.cfg)
        # Core и Shadow — полностью изолированные portfolio/equity/kill
        self.portfolio = PortfolioGuard(self.cfg)
        self.shadow_portfolio = PortfolioGuard(self.cfg)
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
        self._last_daily_hour: str = ""
        self._bar_lock = threading.Lock()
        # дедуп: (symbol, timeframe, last_closed_bar_ts)
        self._seen_bars: dict[tuple[str, str], int] = {}
        self._last_confirm_slot: str = ""

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

        logger.info("[linear] warming REST history (once)…")
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
                self.shadow = PaperEngine(
                    self.cfg,
                    self.shadow_portfolio,
                    self.journals,
                    shadow=True,
                    min_usd_map=min_usd,
                )

        if self._stop:
            logger.info("[linear] stop during warm — выход")
            return

        start_eq = float(self.paper.equity) if self.paper else float(self.cfg.start_equity_usd)
        self.journals.session_start(equity=start_eq)
        try:
            self.journals.daily_snapshot(
                equity=start_eq,
                day_pnl=0.0,
                open_n=0,
                kill=False,
            )
            self._last_daily_hour = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H")
        except Exception:
            logger.exception("[linear] daily-snap on start failed")

        self.tape.set_symbols(want)
        # Только очередь: обработка баров — в main loop (дедуп WS vs REST)
        self.tape.set_handlers(on_kline_closed=None, on_trade=self._on_trade)
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
        if len(df) and int(df.iloc[-1]["ts"]) == row["ts"]:
            df.iloc[-1] = row
        else:
            self.ohlc[symbol][interval] = pd.concat([df, pd.DataFrame([row])], ignore_index=True)
            cap = {
                "15m": self.cfg.warm_bars_15m,
                "1h": self.cfg.warm_bars_1h,
                "4h": self.cfg.warm_bars_4h,
            }[interval]
            if len(self.ohlc[symbol][interval]) > cap + 50:
                self.ohlc[symbol][interval] = self.ohlc[symbol][interval].iloc[-cap:].reset_index(drop=True)

    def _mark_seen(self, symbol: str, timeframe: str, bar_ts: int) -> bool:
        """True если бар новый и помечен; False если уже обрабатывали."""
        key = (symbol.upper(), timeframe)
        prev = self._seen_bars.get(key)
        if prev is not None and int(bar_ts) <= int(prev):
            return False
        self._seen_bars[key] = int(bar_ts)
        return True

    def _on_trade(self, symbol: str, price: float, ts_ms: int) -> None:
        if self.paper:
            self.paper.on_public_trade(symbol, price, ts_ms)
        if self.shadow and symbol in self.cfg.shadow_symbols:
            self.shadow.on_public_trade(symbol, price, ts_ms)

    def _handle_closed_15m(self, symbol: str, bar: dict[str, Any]) -> None:
        """Единая обработка закрытой 15m (WS или REST) под lock + дедуп."""
        with self._bar_lock:
            if symbol not in self.ohlc:
                return
            ts = int(bar["ts"])
            if not self._mark_seen(symbol, "15m", ts):
                return

            # 1) TTL всех pending ДО сигналов на этом баре
            if self.paper:
                self.paper.expire_pendings_at_bar(ts)
            if self.shadow:
                self.shadow.expire_pendings_at_bar(ts)
            if not self.cfg.paper:
                self.executor.expire_pendings_at_bar(ts)

            self._append_bar(symbol, "15m", bar)

            # 2) HTF из локальных 15m — без REST (борьба с 10006 на :00/:15)
            if (ts + _15M) % _BAR_MS["1h"] == 0:
                self._rollup_htf_from_15m(symbol, "1h")
            if (ts + _15M) % _BAR_MS["4h"] == 0:
                self._rollup_htf_from_15m(symbol, "4h")

            self.features[symbol] = build_features(
                self.ohlc[symbol]["15m"],
                self.ohlc[symbol]["1h"],
                self.ohlc[symbol]["4h"],
            )

            # 3) fills / SL-TP на этом символе
            if self.paper:
                self.paper.on_bar_close_15m(symbol, bar)
            if self.shadow and symbol in set(self.cfg.shadow_symbols) | {self.cfg.btc_symbol}:
                self.shadow.on_bar_close_15m(symbol, bar)

            if symbol == self.cfg.btc_symbol:
                return
            self._emit_signal(symbol)

    def _rollup_htf_from_15m(self, symbol: str, key: str) -> None:
        """Собрать закрытый 1h/4h бар из уже известных 15m — без Bybit REST."""
        try:
            df15 = self.ohlc[symbol]["15m"]
            period = _BAR_MS[key]
            need = period // _15M
            if len(df15) < need:
                return
            last_ts = int(df15.iloc[-1]["ts"])
            # закрытый HTF bucket: конец последнего 15m = начало + period
            bucket_end = last_ts + _15M
            if bucket_end % period != 0:
                return
            bucket_open = bucket_end - period
            if not self._mark_seen(symbol, key, bucket_open):
                return
            chunk = df15[(df15["ts"] >= bucket_open) & (df15["ts"] < bucket_end)]
            if len(chunk) < need:
                logger.debug(
                    "[linear] HTF rollup skip %s %s: got %d/%d 15m",
                    symbol,
                    key,
                    len(chunk),
                    need,
                )
                return
            row = {
                "ts": int(bucket_open),
                "open": float(chunk.iloc[0]["open"]),
                "high": float(chunk["high"].max()),
                "low": float(chunk["low"].min()),
                "close": float(chunk.iloc[-1]["close"]),
                "volume": float(chunk["volume"].sum()),
                "turnover": float(chunk["turnover"].sum()) if "turnover" in chunk.columns else 0.0,
            }
            self._append_bar(symbol, key, row)
        except Exception as exc:
            logger.warning("[linear] HTF rollup %s %s: %s", symbol, key, exc)

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
        """Fallback один раз на 15m-слот: REST только по символам без WS-бара.

        Раньше цикл секунд 2–12 × 4 символа → десятки kline/слот и 10006.
        """
        now = datetime.now(timezone.utc)
        if now.minute % 15 != 0 or now.second < 8 or now.second > 14:
            return
        slot = now.strftime("%Y-%m-%d %H:%M")
        if slot == self._last_confirm_slot:
            return
        # ожидаемый open ts только что закрытого 15m
        expect_ts = (int(now.timestamp() * 1000) // _15M - 1) * _15M
        want = set(self.cfg.symbols) | set(self.cfg.shadow_symbols) | {self.cfg.btc_symbol}
        missing = []
        for sym in sorted(want):
            seen = self._seen_bars.get((sym.upper(), "15m"))
            if seen is None or int(seen) < expect_ts:
                missing.append(sym)
        if not missing:
            self._last_confirm_slot = slot
            return
        logger.info("[linear] REST confirm fallback %s missing=%s", slot, missing)
        for sym in missing:
            try:
                data = self.rest.public_get(
                    "/v5/market/kline",
                    {"category": "linear", "symbol": sym, "interval": "15", "limit": 2},
                )
                rows = data.get("result", {}).get("list") or []
                if len(rows) < 2:
                    continue
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
                self._handle_closed_15m(sym, bar)
            except Exception as exc:
                logger.warning("[linear] REST confirm %s: %s", sym, exc)
        self._last_confirm_slot = slot

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

    def _maybe_daily_snap(self, eq: float) -> None:
        utc = datetime.now(timezone.utc)
        hour_key = utc.strftime("%Y-%m-%d-%H")
        if hour_key == self._last_daily_hour:
            return
        # пишем в первую минуту часа или если час уже сменился (догон после блока)
        try:
            self.journals.daily_snapshot(
                equity=eq,
                day_pnl=self.portfolio.state.day_realized_pnl,
                open_n=self.portfolio.open_plus_pending(),
                kill=self.portfolio.state.kill,
            )
            if self.shadow:
                self.journals.event(
                    {
                        "type": "shadow_snap",
                        "equity": self.shadow.equity,
                        "day_realized_pnl": self.shadow_portfolio.state.day_realized_pnl,
                        "open_plus_pending": self.shadow_portfolio.open_plus_pending(),
                        "kill": self.shadow_portfolio.state.kill,
                    }
                )
            self._last_daily_hour = hour_key
        except Exception:
            logger.exception("[linear] daily-snap failed hour=%s", hour_key)

    def _loop(self) -> None:
        logger.info("[linear] desk loop running")
        while not self._stop:
            self.tape.wait_kline(timeout=1.0)
            for sym, bar in self.tape.pop_closed():
                try:
                    self._handle_closed_15m(sym, bar)
                except Exception:
                    logger.exception("[linear] handle bar %s failed", sym)
            try:
                self._rest_confirm_poll()
            except Exception:
                logger.exception("[linear] rest confirm poll failed")
            try:
                self._funding_tick()
            except Exception:
                logger.exception("[linear] funding tick failed")
            now = time.time()
            if now - self._last_reconcile >= 60:
                self._last_reconcile = now
                try:
                    if not self.cfg.paper:
                        self.executor.reconcile()
                        parkable = self.account.earn_respect_reserve()
                        self.journals.event({"type": "earn_reserve_free", "usd": parkable})
                    snap = self.account.wallet_snapshot()
                    eq = float(snap.get("totalEquity") or self.portfolio.state.equity)
                    if self.cfg.paper and self.paper:
                        eq = self.paper.equity
                    self.portfolio.update_equity(eq)
                    self._maybe_daily_snap(eq)
                except Exception:
                    logger.exception("[linear] reconcile/snap cycle failed")
            time.sleep(0.2)


def main_paper() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    desk = LinearDesk(default_config(paper=True))
    try:
        desk.start()
    except KeyboardInterrupt:
        desk.stop()
