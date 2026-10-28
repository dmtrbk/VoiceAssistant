# tests/test_crypto_reconcile.py
# Сверка кармана/рукава с балансом биржи. Щуп в skip.

import unittest

from skills.crypto import reconcile as R


class TestCryptoReconcile(unittest.TestCase):
    def test_pocket_clears_when_wallet_empty(self):
        self.assertEqual(
            R.reconcile_pocket_qty(local_qty=10.0, wallet_qty=0.0, price=100.0),
            0.0,
        )

    def test_pocket_clamps_to_wallet(self):
        self.assertAlmostEqual(
            R.reconcile_pocket_qty(local_qty=10.0, wallet_qty=4.0, price=100.0),
            4.0,
        )

    def test_pocket_keeps_when_aligned(self):
        self.assertAlmostEqual(
            R.reconcile_pocket_qty(local_qty=2.0, wallet_qty=2.0, price=100.0),
            2.0,
        )

    def test_pocket_dust_clears(self):
        self.assertEqual(
            R.reconcile_pocket_qty(local_qty=1.0, wallet_qty=0.01, price=100.0, dust_usd=3.0),
            0.0,
        )

    def test_sleeve_alloc_drops_missing_desk_leg(self):
        cleaned = R.reconcile_sleeve_alloc(
            {"ENA": 15.0, "CAP": 15.0},
            held_values={"ENA": 40.0},
            probe_tickers=set(),
            desk_bought={"ENA", "CAP"},
            min_held_usd=5.0,
        )
        self.assertEqual(cleaned, {"ENA": 15.0})

    def test_sleeve_alloc_keeps_probe_ticker(self):
        cleaned = R.reconcile_sleeve_alloc(
            {"PEPE": 10.0},
            held_values={},
            probe_tickers={"PEPE"},
            desk_bought={"PEPE"},
            min_held_usd=5.0,
        )
        self.assertEqual(cleaned, {"PEPE": 10.0})

    def test_sleeve_maps_skip_probe(self):
        arms, lows = R.reconcile_sleeve_maps(
            {"PEPE": {"high": 1.0, "ts": 1.0}, "CAP": {"high": 2.0, "ts": 1.0}},
            {"PEPE": 0.9, "CAP": 1.5},
            held_values={},
            probe_tickers={"PEPE"},
            min_held_usd=5.0,
        )
        self.assertIn("PEPE", arms)
        self.assertIn("PEPE", lows)
        self.assertNotIn("CAP", arms)
        self.assertNotIn("CAP", lows)

    def test_second_pocket_legs_clamp_and_drop(self):
        cleaned, notes = R.reconcile_second_pocket_legs(
            [
                {"ticker": "PONS", "qty": 100.0, "entry": 0.29},
                {"ticker": "CAP", "qty": 10.0, "entry": 1.0},
            ],
            wallet_qtys={"PONS": 40.0},
            prices={"PONS": 0.28, "CAP": 1.0},
        )
        self.assertEqual(len(cleaned), 1)
        self.assertEqual(cleaned[0]["ticker"], "PONS")
        self.assertAlmostEqual(cleaned[0]["qty"], 40.0)
        self.assertTrue(any("CAP" in n for n in notes))

    def test_orphan_orders_skip_probe_and_fresh(self):
        now = 1_700_000_000.0  # реалистичный epoch, Bybit createdTime в ms
        rows = [
            {
                "symbol": "PEPEUSDT",
                "orderId": "1",
                "orderStatus": "New",
                "orderType": "Limit",
                "createdTime": str(int((now - 200) * 1000)),
            },
            {
                "symbol": "CAPUSDT",
                "orderId": "2",
                "orderStatus": "New",
                "orderType": "Limit",
                "createdTime": str(int((now - 200) * 1000)),
            },
            {
                "symbol": "ENAUSDT",
                "orderId": "3",
                "orderStatus": "New",
                "orderType": "Limit",
                "createdTime": str(int((now - 10) * 1000)),
            },
        ]
        orphans = R.orphan_open_orders(rows, now=now, skip_tickers={"PEPE"}, max_age_sec=90)
        self.assertEqual(orphans, [{"ticker": "CAP", "order_id": "2"}])


if __name__ == "__main__":
    unittest.main()
