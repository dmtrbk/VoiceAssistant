"""Оркестратор paper/live: warm → features → signal on 15m close → exec/paper."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from .account import AccountGuard
from .bybit_rest import BybitRest
from .config import LinearConfig, default_config
from .execution import LinearExecutor
from .instruments import InstrumentCache
from .journals import LinearJournals
from . import paper_book_store as book_store
from .paper_engine import PaperEngine
from .portfolio import PortfolioGuard
from .strategy_core import StrategyParams, build_features, signal_at_bar
from .stream_linear import LinearPublicTape
from .stream_private import LinearPrivateTape
from .warm import warm_universe

logger = logging.getLogger(__name__)

_BAR_MS = {"15m": 15 * 60_000, "1h": 60 * 60_000, "4h": 4 * 60 * 60_000}
_15M = _BAR_MS["15m"]
_MSK = ZoneInfo("Europe/Moscow")
_NET_ERR_MARKERS = (
    "nameresolution",
    "failed to resolve",
    "name resolution",
    "nodename nor servname",
    "temporary failure in name resolution",
    "max retries exceeded",
    "connection aborted",
    "connection reset",
    "connect timeout",
    "timed out",
    "network is unreachable",
    "connection refused",
)
_NET_BACKOFF_SEC = 300.0  # тихий cooldown 5 мин
_NET_ALERT_SEC = 20 * 60  # алерт если сеть/WS молчит дольше


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
        self.executor = LinearExecutor(
            self.rest,
            self.cfg,
            self.instruments,
            self.account,
            portfolio=self.portfolio,
        )
        self.tape = LinearPublicTape(testnet=self.cfg.testnet)
        self.private_tape: LinearPrivateTape | None = None
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
        # после DNS/connect ошибок — не долбить REST confirm каждый :15
        self._confirm_backoff_until: float = 0.0
        self._gap_heal_pause_sec: float = 0.15
        self._net_down_since: float = 0.0
        self._net_alert_sent: bool = False
        self._last_bar_wall: float = time.time()
        self._tg_daily_day: str = ""
        self._persist_lock = threading.Lock()

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
            self._wire_paper_persist()
            restored = self._restore_paper_books()
            if restored:
                self._replay_paper_catchup()
        else:
            # live: поднять позиции/ордера с биржи + private WS для мгновенного protect
            try:
                self.executor.bootstrap_from_exchange(self.cfg.symbols)
            except Exception:
                logger.exception("[linear] bootstrap_from_exchange failed")
            self.private_tape = LinearPrivateTape(
                api_key=self.rest.api_key,
                api_secret=self.rest.api_secret,
                testnet=self.cfg.testnet,
            )
            self.private_tape.set_handlers(
                on_order=self.executor.on_private_order,
                on_execution=self.executor.on_private_execution,
            )
            self.private_tape.start()

        if self._stop:
            logger.info("[linear] stop during warm — выход")
            return

        if self.cfg.paper and self.paper:
            start_eq = float(self.paper.equity)
        elif not self.cfg.paper:
            try:
                start_eq = float(self.account.wallet_snapshot().get("totalEquity") or self.cfg.start_equity_usd)
            except Exception:
                start_eq = float(self.cfg.start_equity_usd)
            self.portfolio.update_equity(start_eq)
        else:
            start_eq = float(self.cfg.start_equity_usd)
        self.journals.session_start(
            equity=start_eq,
            extra={
                "open_plus_pending": self.portfolio.open_plus_pending(),
                "shadow_open_plus_pending": (
                    self.shadow_portfolio.open_plus_pending() if self.shadow else 0
                ),
            },
        )
        try:
            self.journals.daily_snapshot(
                equity=start_eq,
                day_pnl=float(self.portfolio.state.day_realized_pnl),
                open_n=self.portfolio.open_plus_pending(),
                kill=bool(self.portfolio.state.kill),
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
        try:
            self._persist_paper_books()
        except Exception:
            logger.exception("[linear] persist on stop failed")
        self.tape.stop()
        if self.private_tape is not None:
            self.private_tape.stop()

    def _wire_paper_persist(self) -> None:
        if self.paper:
            self.paper.set_persist_hook(lambda: self._persist_one(shadow=False))
        if self.shadow:
            self.shadow.set_persist_hook(lambda: self._persist_one(shadow=True))

    def _persist_one(self, *, shadow: bool) -> None:
        with self._persist_lock:
            eng = self.shadow if shadow else self.paper
            if eng is None:
                return
            path = book_store.shadow_path(self.cfg.runtime_dir) if shadow else book_store.core_path(
                self.cfg.runtime_dir
            )
            book_store.save_engine(eng, path)

    def _persist_paper_books(self) -> None:
        if self.paper:
            self._persist_one(shadow=False)
        if self.shadow:
            self._persist_one(shadow=True)

    def _restore_paper_books(self) -> bool:
        ok = False
        if self.paper:
            raw = book_store.try_load(book_store.core_path(self.cfg.runtime_dir))
            if book_store.load_into_engine(self.paper, raw):
                logger.info(
                    "[linear] restored core book: pos=%d pending=%d equity=%.2f",
                    len(self.paper.book.positions),
                    len(self.paper.book.pending),
                    self.paper.equity,
                )
                ok = True
        if self.shadow:
            raw = book_store.try_load(book_store.shadow_path(self.cfg.runtime_dir))
            if book_store.load_into_engine(self.shadow, raw):
                logger.info(
                    "[linear] restored shadow book: pos=%d pending=%d equity=%.2f",
                    len(self.shadow.book.positions),
                    len(self.shadow.book.pending),
                    self.shadow.equity,
                )
                ok = True
        return ok

    def _replay_paper_catchup(self) -> None:
        """После рестарта: прогнать warm 15m по открытым/pending (SL первым, без сигналов)."""
        for eng in (self.paper, self.shadow):
            if eng is None:
                continue
            syms = set(eng.book.positions) | set(eng.book.pending)
            for sym in sorted(syms):
                df = self.ohlc.get(sym, {}).get("15m")
                if df is None or df.empty:
                    continue
                pos = eng.book.positions.get(sym)
                pend = eng.book.pending.get(sym)
                since = 0
                if pos is not None:
                    since = max(since, int(pos.open_ts or pos.signal.bar_ts))
                if pend is not None:
                    since = max(since, int(pend.created_bar_ts))
                rows = df[df["ts"].astype(int) > since]
                for _, row in rows.iterrows():
                    bar = {
                        "ts": int(row["ts"]),
                        "open": float(row["open"]),
                        "high": float(row["high"]),
                        "low": float(row["low"]),
                        "close": float(row["close"]),
                        "volume": float(row.get("volume") or 0),
                        "confirm": True,
                    }
                    # fill/SL на баре → затем TTL (не наоборот)
                    eng.on_bar_close_15m(sym, bar)
                    eng.expire_pendings_at_bar(int(bar["ts"]))
                    if sym not in eng.book.positions and sym not in eng.book.pending:
                        break
            self._persist_one(shadow=bool(eng.shadow))

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

    def _last_15m_ts(self, symbol: str) -> int | None:
        key = (symbol.upper(), "15m")
        seen = self._seen_bars.get(key)
        if seen is not None:
            return int(seen)
        df = self.ohlc.get(symbol, {}).get("15m")
        if df is not None and len(df):
            return int(df.iloc[-1]["ts"])
        return None

    def _ingest_closed_15m(
        self,
        symbol: str,
        bar: dict[str, Any],
        *,
        emit_signal: bool,
    ) -> bool:
        """Append + HTF + fills → TTL expire → signal.

        emit_signal=False для gap-heal (без торговли задним числом).
        Порядок: on_bar_close (OHLC fill/SL) → expire_pendings → emit.
        """
        if symbol not in self.ohlc:
            return False
        ts = int(bar["ts"])
        if not self._mark_seen(symbol, "15m", ts):
            return False

        self._append_bar(symbol, "15m", bar)

        if (ts + _15M) % _BAR_MS["1h"] == 0:
            self._rollup_htf_from_15m(symbol, "1h")
        if (ts + _15M) % _BAR_MS["4h"] == 0:
            self._rollup_htf_from_15m(symbol, "4h")

        self.features[symbol] = build_features(
            self.ohlc[symbol]["15m"],
            self.ohlc[symbol]["1h"],
            self.ohlc[symbol]["4h"],
        )

        # 1) OHLC fill / SL-TP на этом баре
        if self.paper:
            self.paper.on_bar_close_15m(symbol, bar)
        if self.shadow and symbol in set(self.cfg.shadow_symbols) | {self.cfg.btc_symbol}:
            self.shadow.on_bar_close_15m(symbol, bar)

        # 2) TTL после проверки fill на 2-м баре жизни
        if self.paper:
            self.paper.expire_pendings_at_bar(ts)
        if self.shadow:
            self.shadow.expire_pendings_at_bar(ts)
        if not self.cfg.paper:
            self.executor.expire_pendings_at_bar(ts)

        # 3) новые сигналы только после освобождения слотов
        if emit_signal and symbol != self.cfg.btc_symbol:
            self._emit_signal(symbol)
        return True

    @staticmethod
    def _is_net_error(exc: BaseException) -> bool:
        low = str(exc).lower()
        if any(m in low for m in _NET_ERR_MARKERS):
            return True
        return isinstance(exc, (ConnectionError, TimeoutError, OSError))

    def _enter_net_backoff(self, reason: str) -> None:
        now = time.time()
        if self._net_down_since <= 0:
            self._net_down_since = now
        self._confirm_backoff_until = now + _NET_BACKOFF_SEC
        logger.warning(
            "[linear] net backoff %.0fs (%s) down_since=%.0f",
            _NET_BACKOFF_SEC,
            reason,
            self._net_down_since,
        )

    def _probe_network(self) -> bool:
        """Лёгкий /v5/market/time — один запрос вместо шторма kline."""
        try:
            self.rest.public_get("/v5/market/time", {})
            return True
        except Exception as exc:
            logger.debug("[linear] market/time probe fail: %s", exc)
            return False

    def _fetch_gap_klines_15m(self, symbol: str, *, limit: int) -> list[dict[str, Any]]:
        """Один REST kline.15 → список закрытых баров (asc)."""
        data = self.rest.public_get(
            "/v5/market/kline",
            {
                "category": "linear",
                "symbol": symbol,
                "interval": "15",
                "limit": max(2, min(1000, int(limit))),
            },
        )
        rows = data.get("result", {}).get("list") or []
        # API newest-first
        out: list[dict[str, Any]] = []
        now_ms = int(time.time() * 1000)
        for row in reversed(rows):
            ts = int(row[0])
            if ts + _15M > now_ms:
                continue
            out.append(
                {
                    "ts": ts,
                    "open": float(row[1]),
                    "high": float(row[2]),
                    "low": float(row[3]),
                    "close": float(row[4]),
                    "volume": float(row[5]),
                    "turnover": float(row[6]) if len(row) > 6 else 0.0,
                    "confirm": True,
                }
            )
        return out

    def _rebuild_htf_from_15m(self, symbol: str) -> None:
        """Полная пересборка 1h/4h с нуля на непрерывном 15m (после gap-heal)."""
        if symbol not in self.ohlc:
            return
        df15 = self.ohlc[symbol]["15m"]
        if df15 is None or df15.empty:
            return
        for key in ("1h", "4h"):
            period = _BAR_MS[key]
            need = period // _15M
            rows: list[dict[str, Any]] = []
            open_ts = (int(df15.iloc[0]["ts"]) // period) * period
            if open_ts < int(df15.iloc[0]["ts"]):
                open_ts += period
            last_end = int(df15.iloc[-1]["ts"]) + _15M
            while open_ts + period <= last_end:
                be = open_ts + period
                chunk = df15[(df15["ts"] >= open_ts) & (df15["ts"] < be)]
                if (
                    len(chunk) == need
                    and int(chunk.iloc[0]["ts"]) == open_ts
                    and int(chunk.iloc[-1]["ts"]) == be - _15M
                ):
                    rows.append(
                        {
                            "ts": open_ts,
                            "open": float(chunk.iloc[0]["open"]),
                            "high": float(chunk["high"].max()),
                            "low": float(chunk["low"].min()),
                            "close": float(chunk.iloc[-1]["close"]),
                            "volume": float(chunk["volume"].sum()),
                            "turnover": float(chunk["turnover"].sum())
                            if "turnover" in chunk.columns
                            else 0.0,
                        }
                    )
                open_ts += period
            self.ohlc[symbol][key] = pd.DataFrame(rows)
            seen_key = (symbol.upper(), key)
            if rows:
                self._seen_bars[seen_key] = int(rows[-1]["ts"])
            else:
                self._seen_bars.pop(seen_key, None)
        try:
            self.features[symbol] = build_features(
                self.ohlc[symbol]["15m"],
                self.ohlc[symbol]["1h"],
                self.ohlc[symbol]["4h"],
            )
        except ValueError:
            pass

    def gap_heal_15m(self, symbol: str, new_ts: int) -> int:
        """Доклеить пропуск 15m перед new_ts. Без сигналов на старых барах. Возвращает n healed.

        На каждом доклеенном баре: expire TTL + SL/TP (SL первым). Затем HTF с нуля.
        """
        with self._bar_lock:
            last = self._last_15m_ts(symbol)
        if last is None:
            return 0
        delta = int(new_ts) - int(last)
        if delta <= _15M:
            return 0
        n_missing = int(delta // _15M) - 1
        if n_missing < 1:
            return 0
        limit = min(n_missing + 5, 1000)
        logger.warning(
            "[linear] gap-heal %s: ≈%d missing 15m (last=%s → new=%s), REST limit=%d",
            symbol,
            n_missing,
            last,
            new_ts,
            limit,
        )
        try:
            fetched = self._fetch_gap_klines_15m(symbol, limit=limit)
        except Exception as exc:
            logger.warning("[linear] gap-heal %s REST failed: %s", symbol, exc)
            if self._is_net_error(exc):
                self._enter_net_backoff("gap-heal")
            return 0

        # пауза после REST, чтобы не штормить при heal нескольких символов подряд
        time.sleep(self._gap_heal_pause_sec)
        healed = [b for b in fetched if int(last) < int(b["ts"]) < int(new_ts)]
        healed.sort(key=lambda b: int(b["ts"]))
        n = 0
        with self._bar_lock:
            for bar in healed:
                if self._ingest_closed_15m(symbol, bar, emit_signal=False):
                    n += 1
            if n:
                self._rebuild_htf_from_15m(symbol)
        if n:
            logger.info("[linear] gap-heal %s: dokleeno %d bars + HTF rebuild", symbol, n)
            self._persist_paper_books()
        return n

    def _handle_closed_15m(self, symbol: str, bar: dict[str, Any]) -> None:
        """Единая обработка закрытой 15m (WS или REST) под lock + дедуп + gap-heal."""
        ts = int(bar["ts"])
        self._last_bar_wall = time.time()
        if self._net_down_since > 0:
            # бар пришёл — сеть/WS ожили
            down_for = time.time() - self._net_down_since
            logger.info("[linear] net recovered after %.0fs (bar %s)", down_for, symbol)
            self._net_down_since = 0.0
            self._net_alert_sent = False
            self._confirm_backoff_until = 0.0
        with self._bar_lock:
            if symbol not in self.ohlc:
                return
            last = self._last_15m_ts(symbol)
            need_heal = last is not None and ts - int(last) > _15M
        if need_heal:
            self.gap_heal_15m(symbol, ts)
        with self._bar_lock:
            self._ingest_closed_15m(symbol, bar, emit_signal=True)

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
                # PortfolioGuard: can_add / pending — внутри LinearExecutor
                self.executor.place_entry_postonly(sig, bar_ts=sig.bar_ts, bbo_bid=bid, bbo_ask=ask)

        if (
            self.cfg.shadow_enabled
            and self.shadow
            and symbol in self.cfg.shadow_symbols
        ):
            self.shadow.on_signal(sig, bbo_bid=bid, bbo_ask=ask, last=last)

    def _rest_confirm_poll(self) -> None:
        """Fallback один раз на 15m-слот: REST только по символам без WS-бара.

        При DNS/ConnectionError — тихий cooldown + probe /v5/market/time,
        без шторма 4×kline каждый слот. После восстановления — gap-heal через handle.
        """
        now = datetime.now(timezone.utc)
        # нижняя граница :08; верхнюю убрали — от повторов защищает _last_confirm_slot
        if now.minute % 15 != 0 or now.second < 8:
            return
        wall = time.time()
        if wall < self._confirm_backoff_until:
            return
        slot = now.strftime("%Y-%m-%d %H:%M")
        if slot == self._last_confirm_slot:
            return

        # если уже были в net-down — сначала лёгкий probe
        if self._net_down_since > 0:
            if not self._probe_network():
                self._enter_net_backoff("probe still down")
                self._last_confirm_slot = slot
                self._maybe_net_alert()
                return
            logger.info("[linear] net probe OK — REST confirm + gap-heal")
            self._net_down_since = 0.0
            self._net_alert_sent = False

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
        net_fail = False
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
                time.sleep(self._gap_heal_pause_sec)
            except Exception as exc:
                logger.warning("[linear] REST confirm %s: %s", sym, exc)
                if self._is_net_error(exc):
                    net_fail = True
                    break  # не долбить остальные символы
        if net_fail:
            self._enter_net_backoff("REST confirm")
            self._maybe_net_alert()
        self._last_confirm_slot = slot

    def _maybe_net_alert(self) -> None:
        if self._net_alert_sent or self._net_down_since <= 0:
            return
        if time.time() - self._net_down_since < _NET_ALERT_SEC:
            return
        self._net_alert_sent = True
        mins = int((time.time() - self._net_down_since) / 60)
        text = f"Linear: сеть/WS молчит >{mins} мин (DNS/connect). Тихий backoff, gap-heal после восстановления."
        self._tg_send(text)

    def _tg_send(self, text: str) -> None:
        try:
            from skills.utils import send_telegram_notification, telegram_configured

            if not telegram_configured():
                return
            send_telegram_notification(text, background=True)
        except Exception:
            logger.debug("[linear] telegram send failed", exc_info=True)

    def format_daily_digest(self) -> str:
        """Короткая суточная сводка Core/Shadow для Telegram."""
        day = datetime.now(_MSK).strftime("%d.%m")
        lines = [f"Linear paper за {day}:"]

        def _side(name: str, eng: PaperEngine | None, port: PortfolioGuard) -> None:
            if eng is None:
                return
            opens = [
                f"{s} {p.side}@{p.entry:.4g}" for s, p in eng.book.positions.items()
            ]
            pend = list(eng.book.pending.keys())
            kill = "KILL" if port.state.kill else "ok"
            lines.append(
                f"· {name}: eq ${eng.equity:.2f} (day {port.state.day_realized_pnl:+.2f}), "
                f"open {len(eng.book.positions)}/{self.cfg.max_open_plus_pending}, "
                f"pending {len(pend)}, kill={kill}"
            )
            if opens:
                lines.append(f"  pos: {', '.join(opens)}")
            if pend:
                lines.append(f"  pend: {', '.join(pend)}")
            if port.state.kill and port.state.kill_reason:
                lines.append(f"  reason: {port.state.kill_reason}")

        _side("Core", self.paper, self.portfolio)
        if self.shadow:
            _side("Shadow", self.shadow, self.shadow_portfolio)

        try:
            since = (datetime.now(_MSK) - timedelta(hours=30)).astimezone(timezone.utc)
            since_s = since.isoformat()
            ttl = sl = tp = 0
            ev = self.cfg.path("paper_events.jsonl")
            if ev.is_file():
                for line in ev.read_text(encoding="utf-8").splitlines()[-500:]:
                    if not line.strip():
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if str(obj.get("ts_utc") or "") < since_s:
                        continue
                    if obj.get("type") == "ttl_cancel":
                        ttl += 1
            for name in ("paper_trades.jsonl", "shadow_aave_trades.jsonl"):
                tp_path = self.cfg.path(name)
                if not tp_path.is_file():
                    continue
                for line in tp_path.read_text(encoding="utf-8").splitlines()[-300:]:
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if obj.get("event") != "close":
                        continue
                    if str(obj.get("ts_utc") or "") < since_s:
                        continue
                    if obj.get("reason") == "sl":
                        sl += 1
                    elif obj.get("reason") == "tp":
                        tp += 1
            lines.append(f"· counters ~24h: TTL={ttl} SL={sl} TP={tp}")
        except Exception:
            logger.debug("[linear] digest counters failed", exc_info=True)
        return "\n".join(lines)

    def _maybe_telegram_daily(self) -> None:
        """1 сводка в сутки после 21:00 МСК."""
        now = datetime.now(_MSK)
        if now.hour < 21:
            return
        day = now.strftime("%Y-%m-%d")
        if day == self._tg_daily_day:
            return
        meta_path = self.cfg.path("linear_daily_meta.json")
        try:
            if meta_path.is_file():
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                if isinstance(meta, dict) and meta.get("last_report_day") == day:
                    self._tg_daily_day = day
                    return
        except Exception:
            pass
        text = self.format_daily_digest()
        self._tg_send(text)
        self._tg_daily_day = day
        try:
            tmp = str(meta_path) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"last_report_day": day}, fh, ensure_ascii=False, indent=2)
                fh.write("\n")
            os.replace(tmp, meta_path)
        except Exception:
            logger.debug("[linear] daily meta write failed", exc_info=True)
        # kill — отдельный редкий алерт
        if self.portfolio.state.kill:
            self._tg_send(f"Linear Core KILL: {self.portfolio.state.kill_reason}")
        if self.shadow and self.shadow_portfolio.state.kill:
            self._tg_send(f"Linear Shadow KILL: {self.shadow_portfolio.state.kill_reason}")

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
                    self._maybe_telegram_daily()
                    self._maybe_net_alert()
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
