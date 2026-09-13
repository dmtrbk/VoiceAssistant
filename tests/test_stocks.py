import unittest
from skills.stocks import (
    StocksSkill,
    _extract_lots,
    _wants_market_report,
    _has_max_hint,
    _is_drop_sell,
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


if __name__ == "__main__":
    unittest.main()
