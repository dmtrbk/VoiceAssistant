# tests/test_crypto_lifetime.py
# Учёт lifetime_pnl_usd в журнале крипты: одна цифра, перезапись.

import json
import os
import tempfile
import unittest

from skills.crypto import journal as crypto_journal


class TestCryptoLifetime(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "jarvis_crypto_trades.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_compute_buy_hold_mark(self):
        trades = [
            {"ticker": "BTC", "side": "buy", "quote": 100.0, "price": 50.0},
        ]
        # qty=2, cost=100; mark 60 → value 120 → pnl +20
        pnl = crypto_journal.compute_lifetime_from_trades(trades, {"BTC": 60.0})
        self.assertEqual(pnl, 20.0)

    def test_compute_realized_sell(self):
        trades = [
            {"ticker": "ETH", "side": "buy", "quote": 100.0, "price": 10.0},
            {"ticker": "ETH", "side": "sell", "quote": 80.0, "price": 16.0},
        ]
        # bought 10 @10 cost 100; sold 5 @16 proceeds 80; cost_sold 50; realized +30; left 5 cost 50
        pnl = crypto_journal.compute_lifetime_from_trades(trades, {"ETH": 10.0})
        # unrealized 5*10-50=0 → total 30
        self.assertEqual(pnl, 30.0)

    def test_empty_trades_none(self):
        self.assertIsNone(crypto_journal.compute_lifetime_from_trades([], {"BTC": 1}))

    def test_recompute_overwrites_single_number(self):
        trades = [
            {"ticker": "SOL", "side": "buy", "quote": 50.0, "price": 25.0},
        ]
        crypto_journal._write_trades(trades, path=self.path, lifetime_pnl_usd=1.0)
        self.assertEqual(crypto_journal.read_lifetime_pnl(self.path), 1.0)
        value = crypto_journal.recompute_lifetime_pnl(
            mark_prices={"SOL": 30.0},
            path=self.path,
        )
        # qty=2 cost=50 mark=60 → +10
        self.assertEqual(value, 10.0)
        self.assertEqual(crypto_journal.read_lifetime_pnl(self.path), 10.0)
        with open(self.path, encoding="utf-8") as handle:
            raw = json.load(handle)
        self.assertEqual(raw["lifetime_pnl_usd"], 10.0)
        self.assertEqual(len(raw["trades"]), 1)
        # second overwrite
        value2 = crypto_journal.recompute_lifetime_pnl(
            mark_prices={"SOL": 20.0},
            path=self.path,
        )
        self.assertEqual(value2, -10.0)
        self.assertEqual(crypto_journal.read_lifetime_pnl(self.path), -10.0)

    def test_preserves_lifetime_when_appending_trades_until_recompute(self):
        crypto_journal._write_trades(
            [{"ticker": "BTC", "side": "buy", "quote": 10.0, "price": 1.0}],
            path=self.path,
            lifetime_pnl_usd=7.5,
        )
        trades = crypto_journal._read_trades(self.path)
        trades.append({"ticker": "BTC", "side": "buy", "quote": 10.0, "price": 1.0})
        crypto_journal._write_trades(trades, path=self.path)  # KEEP lifetime
        self.assertEqual(crypto_journal.read_lifetime_pnl(self.path), 7.5)

    def test_keeps_more_than_two_hundred_trades(self):
        from skills.crypto import common as crypto_common

        self.assertGreaterEqual(crypto_common._TRADE_HISTORY_KEEP, 5000)
        trades = [
            {"ticker": "BTC", "side": "buy", "quote": 1.0, "price": 1.0}
            for _ in range(250)
        ]
        crypto_journal._write_trades(trades, path=self.path)
        self.assertEqual(len(crypto_journal._read_trades(self.path)), 250)


if __name__ == "__main__":
    unittest.main()
