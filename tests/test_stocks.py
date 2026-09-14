import unittest
from unittest.mock import patch

from skills.base import RequestContext
from skills.stocks import (
    StocksSkill,
    _extract_lots,
    _wants_market_report,
    _has_max_hint,
    _is_api_buy_forbidden_text,
    _is_drop_sell,
    _min_trade_rub,
    _signal_is_buy,
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
            self.skill._fallback_allocation([{"ticker": "TMOS"}, {"ticker": "VTBR"}]),
            {"VTBR": 100.0},
        )
        self.assertEqual(self.skill._fallback_allocation([{"ticker": "TMOS"}]), {})

    def test_filter_buy_alloc_drops_tmos_and_renormalizes(self):
        cleaned = self.skill._filter_buy_alloc(
            {"TMOS": 70.0, "VTBR": 30.0},
            [{"ticker": "VTBR"}, {"ticker": "TMOS"}],
        )
        self.assertEqual(cleaned, {"VTBR": 100.0})
        cleaned_sibn = self.skill._filter_buy_alloc(
            {"SIBN": 20.0, "NVTK": 80.0},
            [{"ticker": "NVTK"}, {"ticker": "SIBN"}],
        )
        self.assertEqual(cleaned_sibn, {"NVTK": 100.0})

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
        self.assertNotIn("TMOS", tickers)
        self.assertNotIn("EUTR", tickers)
        self.assertNotIn("SBER", tickers)

    def test_place_order_blocks_tmos_buy(self):
        with patch.object(self.skill, "_post") as post:
            with self.assertRaises(RuntimeError) as ctx:
                self.skill._place_order("TMOS", "ORDER_DIRECTION_BUY", 1)
            self.assertIn("api forbidden", str(ctx.exception))
            post.assert_not_called()

    def test_place_order_blocks_sibn_buy(self):
        with patch.object(self.skill, "_post") as post:
            with self.assertRaises(RuntimeError) as ctx:
                self.skill._place_order("SIBN", "ORDER_DIRECTION_BUY", 1)
            self.assertIn("api forbidden", str(ctx.exception))
            post.assert_not_called()

    def test_qualification_test_error_is_api_forbidden(self):
        snippet = (
            'http 400: {"code":3,"message":"Post order error: '
            "Dlya torgovli e`tim instrumentom neobxodimo projti sootvetstvuyushhij test v razd"
        )
        self.assertTrue(_is_api_buy_forbidden_text(snippet))
        with (
            patch.object(self.skill, "_instrument_ids", return_value=("uid", "figi", "Газпром нефть")),
            patch.object(self.skill, "_pick_account_id", return_value="acc"),
            patch.object(self.skill, "_post", side_effect=RuntimeError(snippet)),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                self.skill._place_order("LKOH", "ORDER_DIRECTION_BUY", 1)
        self.assertIn("api forbidden", str(ctx.exception))
        self.assertTrue(self.skill._is_buy_blocked("LKOH"))
        self.assertIn("LKOH", self.skill._manual_holds)

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

    def test_rebalance_drops_tmos_and_buys_the_rest(self):
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
            result = self.skill._rebalance_portfolio({"TMOS": 70.0, "VTBR": 30.0})
        place.assert_called_with("VTBR", "ORDER_DIRECTION_BUY", 3)
        self.assertNotIn("TMOS", str(place.call_args_list))
        self.assertIn("Купил", result)

    def test_signal_is_buy(self):
        self.assertTrue(_signal_is_buy("SIGNAL_DIRECTION_BUY"))
        self.assertTrue(_signal_is_buy(1))
        self.assertFalse(_signal_is_buy("SIGNAL_DIRECTION_SELL"))

    def test_desk_choose_weights_tinkoff_signals(self):
        candidates = [
            {"ticker": "SBER", "name": "Сбер", "price": 300.0, "pct": 0.0, "lot": 1},
            {"ticker": "CHMF", "name": "Северсталь", "price": 900.0, "pct": 0.0, "lot": 1},
        ]
        signals = [
            {"direction": "SIGNAL_DIRECTION_BUY", "instrumentUid": "u-chmf", "probability": 60},
            {"direction": "SIGNAL_DIRECTION_BUY", "instrumentUid": "u-chmf", "probability": 40},
            {"direction": "SIGNAL_DIRECTION_BUY", "instrumentUid": "u-sber", "probability": 20},
        ]
        insts = {
            "u-chmf": {
                "ticker": "CHMF",
                "classCode": "TQBR",
                "instrumentKind": "INSTRUMENT_TYPE_SHARE",
                "apiTradeAvailableFlag": True,
                "lot": 1,
            },
            "u-sber": {
                "ticker": "SBER",
                "classCode": "TQBR",
                "instrumentKind": "INSTRUMENT_TYPE_SHARE",
                "apiTradeAvailableFlag": True,
                "lot": 1,
            },
        }

        def fake_instrument(_figi, _ticker, uid=None):
            return insts.get(uid or "", {})

        with (
            patch.object(self.skill, "_fetch_buy_signals", return_value=signals),
            patch.object(self.skill, "_instrument", side_effect=fake_instrument),
        ):
            alloc = self.skill._desk_choose(candidates, equity=20_000)
        self.assertGreater(alloc["CHMF"], alloc["SBER"])
        self.assertAlmostEqual(sum(alloc.values()), 100.0, delta=0.2)

    def test_desk_choose_skips_blocked_and_expensive(self):
        candidates = [{"ticker": "VTBR", "name": "ВТБ", "price": 50.0, "pct": 0.0, "lot": 1}]
        signals = [
            {"direction": "SIGNAL_DIRECTION_BUY", "instrumentUid": "u-tmos", "probability": 90},
            {"direction": "SIGNAL_DIRECTION_BUY", "instrumentUid": "u-lkoh", "probability": 80},
            {"direction": "SIGNAL_DIRECTION_BUY", "instrumentUid": "u-vtbr", "probability": 10},
        ]
        insts = {
            "u-tmos": {
                "ticker": "TMOS",
                "classCode": "TQTF",
                "instrumentKind": "INSTRUMENT_TYPE_ETF",
                "apiTradeAvailableFlag": True,
                "lot": 1,
            },
            "u-lkoh": {
                "ticker": "LKOH",
                "classCode": "TQBR",
                "instrumentKind": "INSTRUMENT_TYPE_SHARE",
                "apiTradeAvailableFlag": True,
                "lot": 1,
            },
            "u-vtbr": {
                "ticker": "VTBR",
                "classCode": "TQBR",
                "instrumentKind": "INSTRUMENT_TYPE_SHARE",
                "apiTradeAvailableFlag": True,
                "lot": 1,
            },
        }

        def fake_instrument(_figi, _ticker, uid=None):
            return insts.get(uid or "", {})

        def fake_quote(ticker):
            prices = {"LKOH": 7000.0, "TMOS": 6.5, "VTBR": 50.0}
            return ticker, prices[ticker], 0.0

        with (
            patch.object(self.skill, "_fetch_buy_signals", return_value=signals),
            patch.object(self.skill, "_instrument", side_effect=fake_instrument),
            patch.object(self.skill, "_quote", side_effect=fake_quote),
        ):
            alloc = self.skill._desk_choose(candidates, equity=5000)
        self.assertEqual(alloc, {"VTBR": 100.0})

    def test_desk_choose_falls_back_without_signals(self):
        candidates = [{"ticker": "VTBR", "name": "ВТБ", "price": 50.0, "pct": 0.0, "lot": 1}]
        with patch.object(self.skill, "_fetch_buy_signals", return_value=[]):
            alloc = self.skill._desk_choose(candidates, equity=5000)
        self.assertEqual(alloc, {"VTBR": 100.0})

    def test_desk_choose_telegrams_blocked_signal(self):
        candidates = [
            {"ticker": "NVTK", "name": "Новатэк", "price": 1000.0, "pct": 0.0, "lot": 1},
            {"ticker": "SIBN", "name": "Газпром нефть", "price": 500.0, "pct": 0.0, "lot": 1},
        ]
        signals = [
            {"direction": "SIGNAL_DIRECTION_BUY", "instrumentUid": "u-sibn", "probability": 80},
            {"direction": "SIGNAL_DIRECTION_BUY", "instrumentUid": "u-nvtk", "probability": 20},
        ]
        insts = {
            "u-sibn": {
                "ticker": "SIBN",
                "classCode": "TQBR",
                "instrumentKind": "INSTRUMENT_TYPE_SHARE",
                "apiTradeAvailableFlag": True,
                "lot": 1,
            },
            "u-nvtk": {
                "ticker": "NVTK",
                "classCode": "TQBR",
                "instrumentKind": "INSTRUMENT_TYPE_SHARE",
                "apiTradeAvailableFlag": True,
                "lot": 1,
            },
        }

        def fake_instrument(_figi, _ticker, uid=None):
            return insts.get(uid or "", {})

        with (
            patch.object(self.skill, "_fetch_buy_signals", return_value=signals),
            patch.object(self.skill, "_instrument", side_effect=fake_instrument),
            patch("skills.stocks.telegram_configured", return_value=True),
            patch("skills.stocks.send_telegram_notification") as send,
        ):
            alloc = self.skill._desk_choose(candidates, equity=20_000)
            alloc_again = self.skill._desk_choose(candidates, equity=20_000)
        self.assertEqual(alloc, {"NVTK": 100.0})
        self.assertEqual(alloc_again, {"NVTK": 100.0})
        send.assert_called_once()
        text = send.call_args[0][0]
        self.assertIn("SIBN", text)
        self.assertIn("приложении", text)
        self.assertIn("SIBN", self.skill._manual_holds)

    def test_rebalance_keeps_manual_hold(self):
        self.skill._token = "test-token"
        self.skill._manual_holds.add("SIBN")
        positions = ([{"ticker": "SIBN", "qty": 1.0, "price": 500.0}], 0.0, 0.0)
        with (
            patch.object(self.skill, "_safe_positions", return_value=positions),
            patch.object(self.skill, "_broker_cash", return_value=0.0),
            patch.object(self.skill, "_quote", return_value=("ВТБ", 80.0, 0.0)),
            patch.object(self.skill, "_lot_size", return_value=1),
            patch.object(self.skill, "_max_lots", return_value=(0, 1)),
            patch.object(self.skill, "_place_order") as place,
            patch("skills.stocks.time.sleep"),
        ):
            self.skill._rebalance_portfolio({"VTBR": 100.0})
        place.assert_not_called()


if __name__ == "__main__":
    unittest.main()
