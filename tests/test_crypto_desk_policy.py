# tests/test_crypto_desk_policy.py
# Правила автостола крипты: режим BTC, скор, коридор, анти-чёрн.

import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

from skills.crypto import desk_policy as policy
from skills.crypto import journal as crypto_journal
from skills.crypto.skill import CryptoSkill


_MSK = ZoneInfo("Europe/Moscow")


class TestDeskPolicy(unittest.TestCase):
    def test_risk_off_on_weak_btc_week(self):
        self.assertTrue(policy.is_risk_off(btc_chg_7=-6.0, btc_chg_day=1.0))
        self.assertTrue(policy.is_risk_off(btc_chg_7=-1.0, btc_chg_day=-4.0))
        self.assertFalse(policy.is_risk_off(btc_chg_7=2.0, btc_chg_day=-1.0))

    def test_filter_drops_pump_and_cooldown(self):
        rows = [
            {"ticker": "BTC", "turnover": 1e9, "chg": 1.0, "chg_7": 2.0},
            {"ticker": "DOGE", "turnover": 1e9, "chg": 40.0, "chg_7": 5.0},
            {"ticker": "SOL", "turnover": 1e9, "chg": 1.0, "chg_7": 3.0},
        ]
        filtered = policy.filter_auto_candidates(
            rows,
            held=set(),
            watchlist=["BTC", "ETH", "SOL", "DOGE"],
            cooldown={"SOL"},
        )
        tickers = {r["ticker"] for r in filtered}
        self.assertIn("BTC", tickers)
        self.assertNotIn("DOGE", tickers)
        self.assertNotIn("SOL", tickers)

    def test_score_alloc_keeps_cash_floor(self):
        rows = [
            {"ticker": "BTC", "chg": 1.0, "chg_7": 4.0, "turnover": 1e9},
            {"ticker": "ETH", "chg": 0.5, "chg_7": 3.0, "turnover": 1e9},
        ]
        alloc, why = policy.score_alloc(rows)
        self.assertTrue(alloc)
        self.assertLessEqual(sum(alloc.values()), 100.0 - policy.DESK_CASH_FLOOR_PCT + 0.5)
        self.assertIn("Скор", why)

    def test_score_empty_when_all_negative(self):
        rows = [
            {"ticker": "BTC", "chg": -2.0, "chg_7": -3.0, "turnover": 1e9},
            {"ticker": "ETH", "chg": -1.0, "chg_7": -2.0, "turnover": 1e9},
        ]
        alloc, why = policy.score_alloc(rows)
        self.assertEqual(alloc, {})
        self.assertIn("кэш", why.lower())

    def test_rebalance_band(self):
        self.assertFalse(
            policy.should_rebalance_leg(
                current_value=100.0,
                target_value=103.0,
                equity=1000.0,
                band_pct=6.0,
                min_trade_usd=5.0,
            )
        )
        self.assertTrue(
            policy.should_rebalance_leg(
                current_value=100.0,
                target_value=200.0,
                equity=1000.0,
                band_pct=6.0,
                min_trade_usd=5.0,
            )
        )


class TestJournalChurnAndDaily(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "trades.json")
        self.daily = os.path.join(self.tmp.name, "daily.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_cooldown_after_losing_sell(self):
        now = time.time()
        trades = [
            {"ticker": "ETH", "side": "buy", "quote": 100.0, "price": 10.0, "ts_epoch": now - 3600},
            {"ticker": "ETH", "side": "sell", "quote": 40.0, "price": 8.0, "ts_epoch": now - 100},
        ]
        cool = crypto_journal.cooldown_tickers(12.0, trades, now=now)
        self.assertIn("ETH", cool)

    def test_no_cooldown_on_winning_sell(self):
        now = time.time()
        trades = [
            {"ticker": "BTC", "side": "buy", "quote": 100.0, "price": 50.0, "ts_epoch": now - 3600},
            {"ticker": "BTC", "side": "sell", "quote": 120.0, "price": 60.0, "ts_epoch": now - 100},
        ]
        cool = crypto_journal.cooldown_tickers(12.0, trades, now=now)
        self.assertNotIn("BTC", cool)

    def test_daily_report_gate(self):
        morning = datetime(2026, 9, 18, 10, 0, tzinfo=_MSK)
        evening = datetime(2026, 9, 18, 21, 30, tzinfo=_MSK)
        self.assertFalse(crypto_journal.should_send_daily_report(now=morning, path=self.daily))
        self.assertTrue(crypto_journal.should_send_daily_report(now=evening, path=self.daily))
        crypto_journal.mark_daily_report_sent(now=evening, path=self.daily)
        self.assertFalse(crypto_journal.should_send_daily_report(now=evening, path=self.daily))

    def test_format_daily_report(self):
        now = datetime.now(_MSK)
        trades = [
            {
                "ticker": "BTC",
                "side": "buy",
                "quote": 20.0,
                "price": 100.0,
                "name": "Биткоин",
                "ts_epoch": now.timestamp(),
                "ts": now.strftime("%d.%m %H:%M"),
            }
        ]
        text = crypto_journal.format_daily_pnl_report(trades, lifetime=1.5, day=now)
        self.assertIn("сделок 1", text)
        self.assertIn("Lifetime", text)


class TestDeskAutoUsesScore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name, attr in (
            ("holds.json", "_HOLD_PATH"),
            ("bought.json", "_BOUGHT_PATH"),
            ("trades.json", "_TRADE_PATH"),
            ("daily.json", "_DAILY_PATH"),
        ):
            path = os.path.join(self.tmp.name, name)
            patcher = patch(f"skills.crypto.common.{attr}", path)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.skill = CryptoSkill()

    def test_trade_auto_uses_score_not_groq(self):
        with (
            patch.object(self.skill, "desk_score_alloc", return_value=({}, "кэш")) as score,
            patch.object(self.skill, "ai_pick_alloc") as ai,
            patch.object(self.skill, "desk_auto_candidates", return_value=[]),
        ):
            result = self.skill._trade_auto_locked(silent=True)
        score.assert_called_once()
        ai.assert_not_called()
        self.assertIn("кэш", result.lower())

    def test_risk_off_blocks_auto(self):
        rows = [{"ticker": "BTC", "chg": -4.0, "chg_7": -8.0, "turnover": 1e9, "held": 0}]
        with (
            patch.object(self.skill, "_btc_regime", return_value=(-8.0, -4.0)),
            patch.object(self.skill, "desk_auto_candidates", return_value=rows),
        ):
            alloc, why = self.skill.desk_score_alloc(rows)
        self.assertEqual(alloc, {})
        self.assertIn("риск", why.lower())


if __name__ == "__main__":
    unittest.main()
