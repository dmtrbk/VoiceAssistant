import unittest
from unittest.mock import patch

from skills.base import RequestContext
from skills.stocks import (
    StocksSkill,
    _extract_lots,
    _wants_market_report,
    _has_max_hint,
    _is_drop_sell,
    _min_trade_rub,
    _trade_kind,
)


class TestStocks(unittest.TestCase):
    def setUp(self):
        self.skill = StocksSkill()

    def test_extract_lots(self):
        # Default without count is 1 lot
        self.assertEqual(_extract_lots("купи сбер"), 1)
        self.assertEqual(_extract_lots("продай газпром"), 1)

        # Explicit digits
        self.assertEqual(_extract_lots("купи 5 лотов сбер"), 5)
        self.assertEqual(_extract_lots("продай 10 акций втб"), 10)

        # Word numbers
        self.assertEqual(_extract_lots("купи три лота лукойла"), 3)
        self.assertEqual(_extract_lots("купи две штуки сбер"), 2)

        # Max hints return None (all-in)
        self.assertIsNone(_extract_lots("купи все акции сбер"))
        self.assertIsNone(_extract_lots("продай целиком газпром"))
        self.assertIsNone(_extract_lots("купи максимум тмос"))

        # Substrings in normal words should not trigger max hint
        self.assertFalse(_has_max_hint("всегда покупай сбер"))
        self.assertFalse(_has_max_hint("вовсе не дорого"))
        self.assertEqual(_extract_lots("купи всегда один сбер"), 1)

    def test_wants_market_report(self):
        self.assertTrue(_wants_market_report("как там акции"))
        self.assertTrue(_wants_market_report("что с портфелем"))
        self.assertTrue(_wants_market_report("покажи котировки"))

        # Encyclopedia queries must be False
        self.assertFalse(_wants_market_report("что такое акции"))
        self.assertFalse(_wants_market_report("что значит биржа"))
        self.assertFalse(_wants_market_report("кто такой брокер"))

    def test_order_filled(self):
        # NEW is not filled yet
        self.assertFalse(self.skill._order_filled({"executionReportStatus": "EXECUTION_REPORT_STATUS_NEW"}))
        self.assertFalse(self.skill._order_filled({"executionReportStatus": "EXECUTION_REPORT_STATUS_REJECTED"}))
        self.assertFalse(self.skill._order_filled({"executionReportStatus": "EXECUTION_REPORT_STATUS_CANCELLED"}))

        # FILL or PARTIAL is filled
        self.assertTrue(self.skill._order_filled({"executionReportStatus": "EXECUTION_REPORT_STATUS_FILL"}))
        self.assertTrue(self.skill._order_filled({"executionReportStatus": "EXECUTION_REPORT_STATUS_PARTIALLYFILL"}))

    def test_drop_sell_needs_market_context(self):
        self.assertFalse(_is_drop_sell("сбрось таймер"))
        self.assertFalse(_trade_kind("сбрось таймер"))
        self.assertTrue(_is_drop_sell("сбрось акции сбер"))
        self.assertEqual(_trade_kind("сбрось акции сбер"), "sell")
        self.assertEqual(_trade_kind("продай сбер"), "sell")

    def test_init_does_not_start_desk(self):
        self.assertFalse(getattr(self.skill, "_desk_thread", None) and self.skill._desk_thread.is_alive())

    def test_voice_trade_off_blocks_orders(self):
        spoken: list[str] = []
        with (
            patch("skills.stocks._voice_trade_enabled", return_value=False),
            patch.object(self.skill, "_place_order") as place,
        ):
            self.skill.execute(RequestContext(raw_text="купи сбер", speak=spoken.append))
            place.assert_not_called()
        self.assertEqual(
            spoken,
            ["Сделки выключены."],
        )

    def test_quote_works_when_voice_trade_off(self):
        spoken: list[str] = []
        with (
            patch("skills.stocks._voice_trade_enabled", return_value=False),
            patch.object(self.skill, "_speak_one", return_value="Сбер 300 рублей."),
        ):
            self.skill.execute(RequestContext(raw_text="сколько стоит сбер", speak=spoken.append))
        self.assertEqual(spoken, ["Сбер 300 рублей."])

    def test_min_trade_rub_scales_with_small_equity(self):
        small = _min_trade_rub(300)
        self.assertLess(small, 300 * 0.4)
        self.assertGreater(small, 0)
        self.assertLessEqual(small, 300 * 0.05 + 1e-9)
        self.assertEqual(_min_trade_rub(10_000), 1000)
        self.assertEqual(_min_trade_rub(100_000), 2000)

    def test_fallback_uses_available_ticker(self):
        self.assertEqual(self.skill._fallback_allocation([{"ticker": "VTBR"}]), {"VTBR": 100.0})
        self.assertEqual(
            self.skill._fallback_allocation([{"ticker": "VTBR"}, {"ticker": "TMOS"}]),
            {"TMOS": 100.0},
        )

    def test_small_desk_skips_tape_movers(self):
        self.skill._watchlist = ["SBER", "VTBR"]
        tape = [
            {"ticker": "EUTR", "name": "EUTR", "price": 10.0, "pct": 9.0, "lot": 1, "value": 1_000_000},
            {"ticker": "VTBR", "name": "ВТБ", "price": 80.0, "pct": 1.0, "lot": 1, "value": 1_000_000},
        ]

        def fake_moex(ticker: str):
            quotes = {
                "TMOS": ("Крупнейшие компании", 6.5, 0.0),
                "SBER": ("Сбер", 320.0, 0.0),
            }
            if ticker not in quotes:
                raise RuntimeError(f"no moex quote {ticker}")
            return quotes[ticker]

        with (
            patch.object(self.skill, "_tqbr_tape", return_value=tape),
            patch.object(self.skill, "_held_map", return_value={"VTBR": 2}),
            patch.object(self.skill, "_equity_estimate", return_value=300.0),
            patch.object(self.skill, "_moex_quote", side_effect=fake_moex),
        ):
            chosen = self.skill._desk_candidates()
        tickers = {row["ticker"] for row in chosen}
        self.assertIn("VTBR", tickers)
        self.assertIn("TMOS", tickers)
        self.assertNotIn("EUTR", tickers)
        self.assertNotIn("SBER", tickers)

    def test_rebalance_small_account_buys_target(self):
        self.skill._token = "test-token"
        with (
            patch.object(self.skill, "_safe_positions", return_value=([], 0.0, 0.0)),
            patch.object(self.skill, "_broker_cash", return_value=300.0),
            patch.object(self.skill, "_quote", return_value=("ВТБ", 80.0, 0.0)),
            patch.object(self.skill, "_lot_size", return_value=1),
            patch.object(self.skill, "_max_lots", return_value=(3, 0)),
            patch.object(self.skill, "_place_order", return_value="Купил 3 лота: ВТБ.") as place,
            patch("skills.stocks.time.sleep"),
        ):
            result = self.skill._rebalance_portfolio({"VTBR": 100.0})
        place.assert_called_with("VTBR", "ORDER_DIRECTION_BUY", 3)
        self.assertIn("Купил", result)


if __name__ == "__main__":
    unittest.main()
