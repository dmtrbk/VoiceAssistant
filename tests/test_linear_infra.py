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
from skills.crypto.linear.warm import merge_ohlc_tail
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
        self.assertEqual(float(eng.cfg.fill_through_pct), 0.0002)

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

    def test_merge_ohlc_tail_incremental_no_full_rewarm(self):
        base = pd.DataFrame(
            {
                "ts": [1000, 2000, 3000],
                "open": [1, 2, 3],
                "high": [1, 2, 3],
                "low": [1, 2, 3],
                "close": [1, 2, 3],
                "volume": [1, 1, 1],
                "turnover": [1, 1, 1],
            }
        )
        tail = pd.DataFrame(
            {
                "ts": [3000, 4000],
                "open": [3, 4],
                "high": [3, 4],
                "low": [3, 4],
                "close": [3.5, 4],
                "volume": [1, 1],
                "turnover": [1, 1],
            }
        )
        out = merge_ohlc_tail(base, tail, cap=10)
        self.assertEqual(list(out["ts"]), [1000, 2000, 3000, 4000])
        self.assertEqual(float(out.iloc[2]["close"]), 3.5)

    def test_bar_dedupe_mark_seen(self):
        from skills.crypto.linear.desk_linear import LinearDesk

        desk = LinearDesk(self.cfg)
        self.assertTrue(desk._mark_seen("ZECUSDT", "15m", 100))
        self.assertFalse(desk._mark_seen("ZECUSDT", "15m", 100))
        self.assertFalse(desk._mark_seen("ZECUSDT", "15m", 99))
        self.assertTrue(desk._mark_seen("ZECUSDT", "15m", 101))
        self.assertTrue(desk._mark_seen("ZECUSDT", "1h", 100))  # другой TF


if __name__ == "__main__":
    unittest.main()
