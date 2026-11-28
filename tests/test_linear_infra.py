# tests/test_linear_infra.py
# TZ5: fill_through_pct, TTL=2 бара, изоляция Core/Shadow, инкрементальный HTF.

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from skills.crypto.linear.config import LinearConfig
from skills.crypto.linear.journals import LinearJournals
from skills.crypto.linear.paper_engine import PaperEngine
from skills.crypto.linear.portfolio import PortfolioGuard
from skills.crypto.linear.strategy_core import Signal
import pandas as pd


def _sig(symbol: str, bar_ts: int, *, side: str = "short", limit: float = 100.0) -> Signal:
    return Signal(
        symbol=symbol,
        side=side,
        bar_ts=bar_ts,
        limit_px=limit,
        sl=limit * 1.02 if side == "short" else limit * 0.98,
        tp=limit * 0.95 if side == "short" else limit * 1.05,
        risk_px=abs(limit * 0.02),
        atr15=1.0,
        er_4h=0.3,
        confirm_close=limit,
    )


class TestLinearInfra(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = LinearConfig(
            paper=True,
            start_equity_usd=120.0,
            kill_equity_floor_usd=100.0,
            entry_ttl_bars_15m=2,
            fill_through_pct=0.0002,
            runtime_dir=Path(self.tmp.name),
        )
        self.cfg.runtime_dir.mkdir(parents=True, exist_ok=True)

    def _engine(self, *, shadow: bool = False) -> PaperEngine:
        port = PortfolioGuard(self.cfg)
        journals = LinearJournals(self.cfg, prefix="paper")
        return PaperEngine(self.cfg, port, journals, shadow=shadow)

    def test_config_fill_through_attr_name(self):
        """Ловит опечатку fills_through_pct до старта сервиса."""
        self.assertTrue(hasattr(self.cfg, "fill_through_pct"))
        self.assertFalse(hasattr(self.cfg, "fills_through_pct"))
        # paper_engine обязан читать то же имя
        import skills.crypto.linear.paper_engine as pe

        src = Path(pe.__file__).read_text(encoding="utf-8")
        self.assertNotIn("fills_through_pct", src)
        self.assertIn("fill_through_pct", src)
        eng = self._engine()
        # getattr как в рантайме — не должен кидать AttributeError
        self.assertEqual(float(eng.cfg.fill_through_pct), 0.0002)  # fill_through_pct

    def test_ttl_cancels_after_exactly_2_bars_before_slot_free(self):
        eng = self._engine()
        t0 = 1_700_000_000_000
        period = 15 * 60_000
        sig = _sig("HYPEUSDT", t0)
        eng.on_signal(sig, bbo_bid=99.0, bbo_ask=99.5, last=99.2)
        self.assertIn("HYPEUSDT", eng.book.pending)
        self.assertFalse(eng.portfolio.can_add_entry("HYPEUSDT")[0])

        # +1 бар — ещё держим
        cancelled = eng.expire_pendings_at_bar(t0 + period)
        self.assertEqual(cancelled, [])
        self.assertIn("HYPEUSDT", eng.book.pending)

        # +2 бара — TTL, слот свободен
        cancelled = eng.expire_pendings_at_bar(t0 + 2 * period)
        self.assertEqual(cancelled, ["HYPEUSDT"])
        self.assertNotIn("HYPEUSDT", eng.book.pending)
        ok, why = eng.portfolio.can_add_entry("HYPEUSDT")
        self.assertTrue(ok, why)

    def test_core_shadow_portfolio_isolation(self):
        core = self._engine(shadow=False)
        shadow = self._engine(shadow=True)
        t0 = 1_700_000_000_000
        core.on_signal(_sig("ZECUSDT", t0), bbo_bid=700, bbo_ask=701, last=700.5)
        # shadow свой portfolio — ZEC не блокирует Core... wait, separate engines
        self.assertIn("ZECUSDT", core.book.pending)
        ok, _ = shadow.portfolio.can_add_entry("ZECUSDT")
        self.assertTrue(ok)
        shadow.on_signal(_sig("AAVEUSDT", t0, limit=90), bbo_bid=89, bbo_ask=90, last=89.5)
        self.assertIn("AAVEUSDT", shadow.book.pending)
        self.assertNotIn("AAVEUSDT", core.book.pending)
        # kill/equity независимы
        shadow.portfolio.update_equity(50.0)  # below floor → shadow kill
        self.assertTrue(shadow.portfolio.state.kill)
        self.assertFalse(core.portfolio.state.kill)

    def test_htf_rollup_from_15m_no_rest(self):
        from skills.crypto.linear.desk_linear import LinearDesk, _15M, _BAR_MS

        desk = LinearDesk(self.cfg)
        t0 = 1_700_000_000_000
        # выровнять к началу часа
        t0 = (t0 // _BAR_MS["1h"]) * _BAR_MS["1h"]
        rows = []
        for i in range(4):
            ts = t0 + i * _15M
            rows.append(
                {
                    "ts": ts,
                    "open": 10 + i,
                    "high": 11 + i,
                    "low": 9 + i,
                    "close": 10.5 + i,
                    "volume": 1.0,
                    "turnover": 1.0,
                }
            )
        desk.ohlc["ZECUSDT"] = {
            "15m": pd.DataFrame(rows),
            "1h": pd.DataFrame(columns=["ts", "open", "high", "low", "close", "volume", "turnover"]),
            "4h": pd.DataFrame(columns=["ts", "open", "high", "low", "close", "volume", "turnover"]),
        }
        desk._rollup_htf_from_15m("ZECUSDT", "1h")
        h1 = desk.ohlc["ZECUSDT"]["1h"]
        self.assertEqual(len(h1), 1)
        self.assertEqual(int(h1.iloc[0]["ts"]), t0)
        self.assertEqual(float(h1.iloc[0]["open"]), 10.0)
        self.assertEqual(float(h1.iloc[0]["close"]), 13.5)
        self.assertEqual(float(h1.iloc[0]["high"]), 14.0)
        # повтор — дедуп
        desk._rollup_htf_from_15m("ZECUSDT", "1h")
        self.assertEqual(len(desk.ohlc["ZECUSDT"]["1h"]), 1)

    def test_bar_dedupe_mark_seen(self):
        from skills.crypto.linear.desk_linear import LinearDesk

        desk = LinearDesk(self.cfg)
        self.assertTrue(desk._mark_seen("ZECUSDT", "15m", 100))
        self.assertFalse(desk._mark_seen("ZECUSDT", "15m", 100))
        self.assertFalse(desk._mark_seen("ZECUSDT", "15m", 99))
        self.assertTrue(desk._mark_seen("ZECUSDT", "15m", 101))
        self.assertTrue(desk._mark_seen("ZECUSDT", "1h", 100))  # другой TF

    def test_gap_heal_16_bars_restores_continuity_and_ema(self):
        """TZ6: дыра 4h (16×15m) → gap-heal → непрерывность + EMA ≡ эталону без сигналов на старых."""
        from skills.crypto.linear.desk_linear import LinearDesk, _15M, _BAR_MS

        def rollup_all(df15: pd.DataFrame, period: int) -> pd.DataFrame:
            need = period // _15M
            rows: list[dict] = []
            if df15.empty:
                return pd.DataFrame(
                    columns=["ts", "open", "high", "low", "close", "volume", "turnover"]
                )
            open_ts = (int(df15.iloc[0]["ts"]) // period) * period
            if open_ts < int(df15.iloc[0]["ts"]):
                open_ts += period
            last_end = int(df15.iloc[-1]["ts"]) + _15M
            while open_ts + period <= last_end:
                be = open_ts + period
                chunk = df15[(df15["ts"] >= open_ts) & (df15["ts"] < be)]
                if len(chunk) == need:
                    rows.append(
                        {
                            "ts": open_ts,
                            "open": float(chunk.iloc[0]["open"]),
                            "high": float(chunk["high"].max()),
                            "low": float(chunk["low"].min()),
                            "close": float(chunk.iloc[-1]["close"]),
                            "volume": float(chunk["volume"].sum()),
                            "turnover": float(chunk["turnover"].sum()),
                        }
                    )
                open_ts += period
            return pd.DataFrame(rows)

        t0 = (1_700_000_000_000 // _BAR_MS["4h"]) * _BAR_MS["4h"]

        def mk_bar(i: int) -> dict:
            c = 100.0 + i * 0.25 + (0.5 if i % 3 == 0 else -0.2)
            o = c - 0.1
            return {
                "ts": t0 + i * _15M,
                "open": o,
                "high": max(o, c) + 0.3,
                "low": min(o, c) - 0.3,
                "close": c,
                "volume": 1.0 + i * 0.01,
                "turnover": 100.0,
                "confirm": True,
            }

        warm_n, gap_n = 80, 16
        all_bars = [mk_bar(i) for i in range(warm_n + gap_n + 1)]
        warm = all_bars[:warm_n]
        missing = all_bars[warm_n : warm_n + gap_n]
        live = all_bars[warm_n + gap_n]
        self.assertEqual(len(missing), 16)

        warm_df = pd.DataFrame(warm)
        full_df = pd.DataFrame(all_bars)
        ref_1h = rollup_all(full_df, _BAR_MS["1h"])
        ref_4h = rollup_all(full_df, _BAR_MS["4h"])
        self.assertGreater(len(ref_1h), 0)

        desk = LinearDesk(self.cfg)
        desk._gap_heal_pause_sec = 0.0
        warm_1h = rollup_all(warm_df, _BAR_MS["1h"])
        warm_4h = rollup_all(warm_df, _BAR_MS["4h"])
        desk.ohlc["ZECUSDT"] = {"15m": warm_df.copy(), "1h": warm_1h.copy(), "4h": warm_4h.copy()}
        desk._seen_bars[("ZECUSDT", "15m")] = int(warm[-1]["ts"])
        if len(warm_1h):
            desk._seen_bars[("ZECUSDT", "1h")] = int(warm_1h.iloc[-1]["ts"])
        if len(warm_4h):
            desk._seen_bars[("ZECUSDT", "4h")] = int(warm_4h.iloc[-1]["ts"])

        emit_ts: list[int] = []

        def _spy_emit(sym: str) -> None:
            df = desk.features.get(sym)
            if df is not None and len(df):
                emit_ts.append(int(df.iloc[-1]["ts"]))

        desk._emit_signal = _spy_emit  # type: ignore[method-assign]
        desk._fetch_gap_klines_15m = (  # type: ignore[method-assign]
            lambda symbol, limit: missing
        )

        desk._handle_closed_15m("ZECUSDT", live)

        m15 = desk.ohlc["ZECUSDT"]["15m"]
        self.assertEqual(len(m15), warm_n + gap_n + 1)
        ts = m15["ts"].astype(int).tolist()
        for a, b in zip(ts, ts[1:]):
            self.assertEqual(b - a, _15M, f"gap {a}->{b}")
        self.assertEqual(int(m15.iloc[-1]["ts"]), int(live["ts"]))
        self.assertEqual(list(m15["close"].astype(float)), [float(b["close"]) for b in all_bars])

        gap_ts = {int(b["ts"]) for b in missing}
        self.assertTrue(gap_ts.isdisjoint(emit_ts))
        self.assertTrue(all(t == int(live["ts"]) for t in emit_ts) or emit_ts == [])

        got_1h = desk.ohlc["ZECUSDT"]["1h"]
        got_4h = desk.ohlc["ZECUSDT"]["4h"]
        self.assertEqual(len(got_1h), len(ref_1h))
        self.assertEqual(len(got_4h), len(ref_4h))
        self.assertGreater(len(got_4h), 0)
        self.assertEqual(list(got_1h["ts"].astype(int)), list(ref_1h["ts"].astype(int)))
        self.assertEqual(list(got_4h["ts"].astype(int)), list(ref_4h["ts"].astype(int)))
        self.assertEqual(
            list(got_1h["close"].astype(float)), list(ref_1h["close"].astype(float))
        )
        self.assertEqual(
            list(got_4h["close"].astype(float)), list(ref_4h["close"].astype(float))
        )
        for span in (20, 50):
            for label, got, ref in (("1h", got_1h, ref_1h), ("4h", got_4h, ref_4h)):
                e_got = got["close"].astype(float).ewm(span=span, adjust=False).mean().iloc[-1]
                e_ref = ref["close"].astype(float).ewm(span=span, adjust=False).mean().iloc[-1]
                self.assertAlmostEqual(
                    float(e_got), float(e_ref), places=9, msg=f"{label} EMA{span}"
                )
                self.assertLess(abs(float(e_got) - float(e_ref)), 1e-12)

    def test_paper_book_atomic_core_shadow_roundtrip(self):
        from skills.crypto.linear import paper_book_store as store

        core = self._engine(shadow=False)
        shadow = self._engine(shadow=True)
        t0 = 1_700_000_000_000
        core.on_signal(_sig("ZECUSDT", t0, side="short", limit=100.0), bbo_bid=99, bbo_ask=100, last=99.5)
        shadow.on_signal(
            _sig("AAVEUSDT", t0, side="short", limit=200.0), bbo_bid=199, bbo_ask=201, last=200
        )
        # fill shadow via bar (high пробивает limit, но не SL=204)
        shadow.on_bar_close_15m(
            "AAVEUSDT",
            {"ts": t0, "open": 200, "high": 201, "low": 199.5, "close": 200.5, "volume": 1},
        )
        self.assertIn("AAVEUSDT", shadow.book.positions)

        cp = Path(self.tmp.name) / "paper_book_core.json"
        sp = Path(self.tmp.name) / "paper_book_shadow.json"
        store.save_engine(core, cp)
        store.save_engine(shadow, sp)
        self.assertTrue(cp.is_file())
        self.assertTrue(sp.is_file())
        self.assertFalse(cp.with_suffix(".json.tmp").exists())

        core2 = self._engine(shadow=False)
        shadow2 = self._engine(shadow=True)
        self.assertTrue(store.load_into_engine(core2, store.try_load(cp)))
        self.assertTrue(store.load_into_engine(shadow2, store.try_load(sp)))
        self.assertIn("ZECUSDT", core2.book.pending)
        self.assertIn("AAVEUSDT", shadow2.book.positions)
        self.assertAlmostEqual(shadow2.book.positions["AAVEUSDT"].entry, 200.0)
        self.assertEqual(core2.portfolio.open_plus_pending(), 1)
        self.assertEqual(shadow2.portfolio.open_plus_pending(), 1)

    def test_gap_heal_applies_sl_on_healed_bars_no_backfill_signal(self):
        """Открытая paper-позиция + дыра → gap-heal закрывает по SL на доклеенном баре."""
        from skills.crypto.linear.desk_linear import LinearDesk, _15M, _BAR_MS
        from skills.crypto.linear.paper_engine import PaperPos
        from skills.crypto.linear.strategy_core import Signal

        desk = LinearDesk(self.cfg)
        desk._gap_heal_pause_sec = 0.0
        desk.paper = self._engine()
        t0 = (1_700_000_000_000 // _BAR_MS["4h"]) * _BAR_MS["4h"]

        def mk(i, *, high=None, low=None, close=None):
            c = 100.0 if close is None else close
            return {
                "ts": t0 + i * _15M,
                "open": c,
                "high": high if high is not None else c + 1,
                "low": low if low is not None else c - 1,
                "close": c,
                "volume": 1.0,
                "turnover": 1.0,
                "confirm": True,
            }

        warm = [mk(i) for i in range(20)]
        # gap bars: bar 25 spikes through SL for short (high >= sl)
        missing = [mk(i) for i in range(20, 36)]
        missing[5] = mk(25, high=120.0, low=99.0, close=110.0)  # SL=115 for short@100
        live = mk(36)

        cols = ["ts", "open", "high", "low", "close", "volume", "turnover"]
        empty = pd.DataFrame(columns=cols)
        desk.ohlc["ZECUSDT"] = {
            "15m": pd.DataFrame(warm),
            "1h": empty.copy(),
            "4h": empty.copy(),
        }
        desk._seen_bars[("ZECUSDT", "15m")] = int(warm[-1]["ts"])
        desk._rebuild_htf_from_15m("ZECUSDT")

        sig = Signal(
            symbol="ZECUSDT",
            side="short",
            bar_ts=int(warm[-1]["ts"]),
            limit_px=100.0,
            sl=115.0,
            tp=90.0,
            risk_px=15.0,
            atr15=1.0,
            er_4h=0.3,
            confirm_close=100.0,
        )
        desk.paper.book.positions["ZECUSDT"] = PaperPos(
            signal=sig,
            side="short",
            qty=0.4,
            entry=100.0,
            sl=115.0,
            tp=90.0,
            leg_usd=40.0,
            open_ts=int(warm[-1]["ts"]),
        )
        desk.paper.portfolio.set_open("ZECUSDT", {"entry": 100.0, "qty": 0.4})

        emit_n = {"n": 0}
        desk._emit_signal = lambda sym: emit_n.__setitem__("n", emit_n["n"] + 1)  # type: ignore
        desk._fetch_gap_klines_15m = lambda symbol, limit: missing  # type: ignore

        desk._handle_closed_15m("ZECUSDT", live)

        self.assertNotIn("ZECUSDT", desk.paper.book.positions)
        # сигнал только на live (spy считает вызовы emit) — gap без emit
        self.assertEqual(emit_n["n"], 1)


if __name__ == "__main__":
    unittest.main()
