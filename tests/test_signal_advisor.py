import unittest
from unittest.mock import patch

import signal_advisor
from skills.stocks import StocksSkill


class TestSignalAdvisor(unittest.TestCase):
    def setUp(self):
        self.skill = StocksSkill()

    def test_collect_rows_marks_blocked_and_skips_bonds(self):
        signals = [
            {"instrumentUid": "u-sibn", "probability": 80, "strategyName": "tech"},
            {"instrumentUid": "u-nvtk", "probability": 40, "strategyName": "fund"},
            {"instrumentUid": "u-bond", "probability": 99, "strategyName": "bond"},
        ]
        insts = {
            "u-sibn": {
                "ticker": "SIBN",
                "classCode": "TQBR",
                "instrumentKind": "INSTRUMENT_TYPE_SHARE",
                "apiTradeAvailableFlag": True,
                "name": "Газпром нефть",
            },
            "u-nvtk": {
                "ticker": "NVTK",
                "classCode": "TQBR",
                "instrumentKind": "INSTRUMENT_TYPE_SHARE",
                "apiTradeAvailableFlag": True,
                "name": "Новатэк",
            },
            "u-bond": {
                "ticker": "SU26238",
                "classCode": "TQOB",
                "instrumentKind": "INSTRUMENT_TYPE_BOND",
                "apiTradeAvailableFlag": True,
            },
        }

        def fake_instrument(_figi, _ticker, uid=None):
            return insts.get(uid or "", {})

        with (
            patch.object(self.skill, "_fetch_buy_signals", return_value=signals),
            patch.object(self.skill, "_instrument", side_effect=fake_instrument),
        ):
            rows = signal_advisor.collect_signal_rows(self.skill)
        tickers = [row["ticker"] for row in rows]
        self.assertEqual(tickers, ["SIBN", "NVTK"])
        self.assertTrue(rows[0]["blocked"])
        self.assertFalse(rows[1]["blocked"])

    def test_build_facts_and_fallback(self):
        rows = [
            {
                "ticker": "NVTK",
                "name": "Новатэк",
                "probability": 40,
                "strategy": "fund",
                "info": "",
                "blocked": False,
                "reason": "",
            }
        ]
        facts = signal_advisor.build_facts(rows, "кэш 5000 ₽")
        self.assertIn("NVTK", facts)
        self.assertIn("5000", facts)
        self.assertIn("сырые сигналы", signal_advisor._fallback_advice(facts).lower())

    def test_run_does_not_place_orders(self):
        self.skill._token = "test-token"
        rows = [
            {
                "ticker": "NVTK",
                "name": "Новатэк",
                "probability": 40,
                "strategy": "fund",
                "info": "",
                "blocked": False,
                "reason": "",
            }
        ]
        with (
            patch.object(signal_advisor, "collect_signal_rows", return_value=rows),
            patch.object(signal_advisor, "collect_book", return_value="кэш 1000 ₽"),
            patch.object(signal_advisor, "advise", return_value="Бери Новатэк."),
            patch.object(self.skill, "_place_order") as place,
            patch.object(signal_advisor, "deliver") as deliver,
        ):
            text = signal_advisor.run(skill=self.skill, to_telegram=False)
        self.assertEqual(text, "Бери Новатэк.")
        place.assert_not_called()
        deliver.assert_called_once()


if __name__ == "__main__":
    unittest.main()
