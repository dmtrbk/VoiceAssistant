import os
import tempfile
import unittest
from unittest.mock import patch

from skill_settings import (
    CRYPTO_AUTO_TRADE_KEY,
    CRYPTO_VOICE_TRADE_KEY,
    OPTIONAL_IDS,
    skill_id_of,
)
from skills import ALL_SKILLS, crypto_skill, stocks_skill, wikipedia_skill
from skills.base import RequestContext
from skills.crypto import (
    CryptoSkill,
    _extract_quote,
    _format_usd,
    is_crypto_command,
    parse_ai_alloc,
)
from skills.stocks import StocksSkill
from skills.wikipedia import is_wiki_command


def _ticker_payload(symbol: str, last: str = "100000", chg: str = "0.012") -> dict:
    return {
        "retCode": 0,
        "result": {
            "list": [
                {
                    "symbol": symbol,
                    "lastPrice": last,
                    "price24hPcnt": chg,
                    "turnover24h": "1000000",
                }
            ]
        },
    }


class TestCryptoSkill(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        hold_path = os.path.join(self.tmp.name, "holds.json")
        bought_path = os.path.join(self.tmp.name, "bought.json")
        trade_path = os.path.join(self.tmp.name, "trades.json")
        patcher_hold = patch("skills.crypto.common._HOLD_PATH", hold_path)
        patcher_bought = patch("skills.crypto.common._BOUGHT_PATH", bought_path)
        patcher_trade = patch("skills.crypto.common._TRADE_PATH", trade_path)
        patcher_hold.start()
        patcher_bought.start()
        patcher_trade.start()
        self.addCleanup(patcher_hold.stop)
        self.addCleanup(patcher_bought.stop)
        self.addCleanup(patcher_trade.stop)
        self.skill = CryptoSkill()

    def _patch_ticker(self):
        def public_get(path, params, cache_key):
            symbol = str((params or {}).get("symbol") or "BTCUSDT")
            last = "4000" if "ETH" in symbol else "100000"
            if "instruments-info" in path:
                return {
                    "retCode": 0,
                    "result": {
                        "list": [
                            {
                                "lotSizeFilter": {
                                    "minOrderQty": "0.001",
                                    "minOrderAmt": "5",
                                    "qtyStep": "0.001",
                                }
                            }
                        ]
                    },
                }
            if "kline" in path:
                return {"retCode": 0, "result": {"list": [["1", "1", "1", "1", "110"], ["0", "1", "1", "1", "100"]]}}
            if path.endswith("/tickers") and not (params or {}).get("symbol"):
                return {
                    "retCode": 0,
                    "result": {
                        "list": [
                            {"symbol": "BTCUSDT", "lastPrice": "100000", "price24hPcnt": "0.01", "turnover24h": "9"},
                            {"symbol": "ETHUSDT", "lastPrice": "4000", "price24hPcnt": "0.02", "turnover24h": "8"},
                        ]
                    },
                }
            return _ticker_payload(symbol, last=last)

        return patch.object(self.skill, "_public_get", side_effect=public_get)

    def test_command_positive(self):
        self.assertTrue(is_crypto_command("сколько стоит биткоин"))
        self.assertTrue(is_crypto_command("курс эфира"))
        self.assertTrue(is_crypto_command("как там солана"))
        self.assertTrue(is_crypto_command("что с криптой"))
        self.assertTrue(is_crypto_command("покажи крипту"))
        self.assertTrue(is_crypto_command("биткоин"))
        self.assertTrue(is_crypto_command("купи биткоин"))
        self.assertTrue(is_crypto_command("продай эфир"))
        self.assertTrue(is_crypto_command("поторгуй криптой"))
        self.assertTrue(is_crypto_command("посоветуй по крипте"))
        self.assertTrue(self.skill.can_handle(RequestContext(raw_text="сколько стоит биткоин")))

    def test_command_negative(self):
        self.assertFalse(is_crypto_command("что такое биткоин"))
        self.assertFalse(is_crypto_command("что такое криптовалюта"))
        self.assertFalse(is_crypto_command("эфир"))
        self.assertFalse(is_crypto_command("в эфире рекорд"))
        self.assertFalse(is_crypto_command("включи эфир"))
        self.assertFalse(is_crypto_command("купи сбер"))
        self.assertFalse(is_crypto_command("как там портфель"))
        self.assertFalse(is_crypto_command("поторгуй"))
        self.assertFalse(is_crypto_command("зачем тебе крипта"))
        self.assertFalse(is_crypto_command("зачем тебе биткоин"))
        self.assertFalse(self.skill.can_handle(RequestContext(raw_text="какая погода")))

    def test_wiki_keeps_encyclopedia(self):
        self.assertTrue(is_wiki_command("что такое биткоин"))
        self.assertFalse(is_crypto_command("что такое биткоин"))

    def test_stocks_yields_crypto_phrases(self):
        stocks = StocksSkill()
        self.assertTrue(stocks.can_handle(RequestContext(raw_text="поторгуй")))
        self.assertTrue(stocks.can_handle(RequestContext(raw_text="купи сбер")))
        self.assertFalse(stocks.can_handle(RequestContext(raw_text="поторгуй криптой")))
        self.assertFalse(stocks.can_handle(RequestContext(raw_text="купи биткоин")))
        self.assertFalse(stocks.can_handle(RequestContext(raw_text="сколько стоит биткоин")))
        self.assertFalse(stocks.can_handle(RequestContext(raw_text="посоветуй по крипте")))

    def test_router_order(self):
        wiki_i = ALL_SKILLS.index(wikipedia_skill)
        crypto_i = ALL_SKILLS.index(crypto_skill)
        stocks_i = ALL_SKILLS.index(stocks_skill)
        self.assertLess(wiki_i, crypto_i)
        self.assertLess(crypto_i, stocks_i)

    def test_format_usd(self):
        self.assertIn("доллар", _format_usd(80))
        self.assertIn("млн", _format_usd(1_200_000))

    def test_extract_quote(self):
        self.assertEqual(_extract_quote("купи биткоин"), 0.0)
        self.assertEqual(_extract_quote("купи биткоин на 20 долларов"), 20.0)
        self.assertIsNone(_extract_quote("купи все биткоин"))

    def test_parse_ai_alloc(self):
        alloc, why = parse_ai_alloc(
            'конечно {"alloc": {"BTC": 40, "ETH": 30, "USDT": 30, "FAKE": 10}, "why": "Держу крупные."}',
            {"BTC", "ETH", "SOL"},
        )
        self.assertEqual(set(alloc), {"BTC", "ETH"})
        self.assertAlmostEqual(sum(alloc.values()), 100.0)
        self.assertIn("крупн", why.lower())

    def test_execute_quote(self):
        spoken: list[str] = []
        with self._patch_ticker():
            self.skill.execute(RequestContext(raw_text="сколько стоит биткоин", speak=spoken.append))
        self.assertEqual(len(spoken), 1)
        self.assertIn("Биткоин", spoken[0])
        self.assertIn("доллар", spoken[0])

    def test_voice_trade_off_blocks_orders(self):
        spoken: list[str] = []
        with (
            patch("skills.crypto.common._voice_trade_enabled", return_value=False),
            patch.object(self.skill, "_place_order") as place,
            self._patch_ticker(),
        ):
            self.skill.execute(RequestContext(raw_text="купи биткоин", speak=spoken.append))
            place.assert_not_called()
        self.assertEqual(spoken, ["Сделки крипты выключены."])

    def test_voice_trade_buy_min(self):
        spoken: list[str] = []
        with (
            patch.dict(os.environ, {"BYBIT_API_KEY": "k", "BYBIT_API_SECRET": "s"}, clear=False),
            patch("skills.crypto.common._voice_trade_enabled", return_value=True),
            patch.object(self.skill, "_cash_and_held", return_value=(100.0, {})),
            patch.object(self.skill, "_place_order", return_value="Купил Биткоин на 5 долларов.") as place,
            self._patch_ticker(),
        ):
            self.skill.execute(RequestContext(raw_text="купи биткоин", speak=spoken.append))
        place.assert_called_once()
        kwargs = place.call_args.kwargs
        self.assertEqual(place.call_args.args[0], "BTC")
        self.assertEqual(place.call_args.args[1], "Buy")
        self.assertGreaterEqual(kwargs.get("quote_usdt"), 5)
        self.assertEqual(spoken, ["Купил Биткоин на 5 долларов."])

    def test_voice_trade_buy_all_leaves_fee_buffer(self):
        spoken: list[str] = []
        with (
            patch.dict(os.environ, {"BYBIT_API_KEY": "k", "BYBIT_API_SECRET": "s"}, clear=False),
            patch("skills.crypto.common._voice_trade_enabled", return_value=True),
            patch.object(self.skill, "_cash_and_held", return_value=(1000.0, {})),
            patch.object(self.skill, "_place_order", return_value="Купил Биткоин на 998 долларов.") as place,
            self._patch_ticker(),
        ):
            self.skill.execute(RequestContext(raw_text="купи весь биткоин", speak=spoken.append))
        place.assert_called_once()
        quote = place.call_args.kwargs["quote_usdt"]
        self.assertAlmostEqual(quote, 1000.0 * 0.998)
        self.assertLess(quote, 1000.0)

    def test_desk_period_is_six_hours(self):
        from skills.crypto import common as crypto_common

        self.assertEqual(crypto_common._DESK_PERIOD_SEC, 6 * 60 * 60)
        self.assertEqual(crypto_common._WATCH_PERIOD_SEC, 12 * 60)

    def test_voice_trade_sell_min_not_dust(self):
        spoken: list[str] = []
        pos = {"qty": 0.00026352, "price": 76000.0, "value": 20.0}
        filters = {"min_qty": 1e-6, "min_amt": 5.0, "step": 1e-6}
        with (
            patch.dict(os.environ, {"BYBIT_API_KEY": "k", "BYBIT_API_SECRET": "s"}, clear=False),
            patch("skills.crypto.common._voice_trade_enabled", return_value=True),
            patch.object(self.skill, "_cash_and_held", return_value=(9973.0, {"BTC": pos})),
            patch.object(self.skill, "_filters", return_value=filters),
            patch.object(self.skill, "_place_order", return_value="Продал Биткоин на 5 долларов.") as place,
            self._patch_ticker(),
        ):
            self.skill.execute(RequestContext(raw_text="продай биткоин", speak=spoken.append))
        place.assert_called_once()
        self.assertEqual(place.call_args.args[0], "BTC")
        self.assertEqual(place.call_args.args[1], "Sell")
        qty = place.call_args.kwargs["base_qty"]
        self.assertGreaterEqual(qty * 76000, 5)
        self.assertLess(qty, 0.00026352)
        self.assertEqual(spoken, ["Продал Биткоин на 5 долларов."])

    def test_voice_trade_sell_all(self):
        spoken: list[str] = []
        pos = {"qty": 0.00026352, "price": 76000.0, "value": 20.0}
        filters = {"min_qty": 1e-6, "min_amt": 5.0, "step": 1e-6}
        with (
            patch.dict(os.environ, {"BYBIT_API_KEY": "k", "BYBIT_API_SECRET": "s"}, clear=False),
            patch("skills.crypto.common._voice_trade_enabled", return_value=True),
            patch.object(self.skill, "_cash_and_held", return_value=(9973.0, {"BTC": pos})),
            patch.object(self.skill, "_filters", return_value=filters),
            patch.object(self.skill, "_place_order", return_value="Продал Биткоин на 20 долларов.") as place,
            self._patch_ticker(),
        ):
            self.skill.execute(RequestContext(raw_text="продай весь биткоин", speak=spoken.append))
        place.assert_called_once()
        self.assertAlmostEqual(place.call_args.kwargs["base_qty"], 0.00026352)
        self.assertEqual(spoken, ["Продал Биткоин на 20 долларов."])

    def test_execute_watchlist(self):
        spoken: list[str] = []
        with (
            patch.dict(
                os.environ,
                {
                    "CRYPTO_WATCHLIST": "bitcoin,ethereum",
                    "BYBIT_API_KEY": "",
                    "BYBIT_API_SECRET": "",
                    "CRYPTO_API_KEY": "",
                    "CRYPTO_API_SECRET": "",
                },
                clear=False,
            ),
            self._patch_ticker(),
        ):
            self.skill._api_key = ""
            self.skill.execute(RequestContext(raw_text="что с криптой", speak=spoken.append))
        self.assertIn("Биткоин", spoken[0])
        self.assertIn("Эфир", spoken[0])

    def test_followup_after_quote(self):
        spoken: list[str] = []
        with (
            patch.dict(
                os.environ,
                {
                    "BYBIT_API_KEY": "",
                    "BYBIT_API_SECRET": "",
                    "CRYPTO_API_KEY": "",
                    "CRYPTO_API_SECRET": "",
                },
                clear=False,
            ),
            self._patch_ticker(),
        ):
            self.skill.execute(RequestContext(raw_text="биткоин", speak=spoken.append))
            self.assertTrue(self.skill.accepts_followup(RequestContext(raw_text="подробнее")))
            self.skill.execute(RequestContext(raw_text="подробнее", speak=spoken.append))
        self.assertIn("Биткоин", spoken[1])

    def test_timeout(self):
        spoken: list[str] = []
        with patch.object(self.skill, "_ticker", side_effect=__import__("requests").Timeout):
            self.skill.execute(RequestContext(raw_text="курс биткоина", speak=spoken.append))
        self.assertEqual(spoken, ["Крипта молчит."])

    def test_advisor_does_not_place_orders(self):
        spoken: list[str] = []
        with (
            patch("crypto_advisor.run", return_value="Держи биткоин и эфир.") as advise,
            patch.object(self.skill, "_place_order") as place,
        ):
            self.skill.execute(RequestContext(raw_text="посоветуй по крипте", speak=spoken.append))
        advise.assert_called_once()
        place.assert_not_called()

    def test_init_does_not_start_desk(self):
        self.assertFalse(getattr(self.skill, "_desk_thread", None) and self.skill._desk_thread.is_alive())

    def test_registered_as_optional(self):
        self.assertIn("crypto", OPTIONAL_IDS)
        self.assertEqual(skill_id_of(crypto_skill), "crypto")

    def test_separate_trade_toggles(self):
        import skill_settings

        self.assertTrue(hasattr(CryptoSkill, "start_background"))
        self.assertEqual(skill_settings.CRYPTO_VOICE_TRADE_KEY, "crypto_voice_trade")
        self.assertEqual(skill_settings.CRYPTO_AUTO_TRADE_KEY, "crypto_auto_trade")
        self.assertEqual(skill_settings.VOICE_TRADE_KEY, "stocks_voice_trade")
        self.assertNotEqual(CRYPTO_VOICE_TRADE_KEY, skill_settings.VOICE_TRADE_KEY)
        self.assertNotEqual(CRYPTO_AUTO_TRADE_KEY, skill_settings.AUTO_TRADE_KEY)
        class_src = __import__("inspect").getsource(CryptoSkill._place_order)
        self.assertIn("/v5/order/create", class_src)
        self.assertIn("baseCoin", class_src)
        self.assertNotIn("invest-public-api", class_src)

    def test_place_order_sell_uses_base_coin(self):
        with (
            patch.object(self.skill, "_filters", return_value={"min_qty": 1e-6, "min_amt": 5.0, "step": 1e-6}),
            patch.object(self.skill, "_ticker", return_value={"price": 76000.0, "chg": 0.0, "turnover": 1.0}),
            patch.object(self.skill, "_journal_trade"),
            patch.object(
                self.skill,
                "_signed",
                return_value={"retCode": 0, "result": {"orderId": "1"}},
            ) as signed,
        ):
            phrase = self.skill._place_order("BTC", "Sell", base_qty=0.00026352, price=76000.0)
        body = signed.call_args.args[2]
        self.assertEqual(body["marketUnit"], "baseCoin")
        self.assertEqual(body["side"], "Sell")
        self.assertEqual(body["isLeverage"], 0)
        self.assertEqual(body["qty"], "0.000263")
        self.assertIn("Продал", phrase)


if __name__ == "__main__":
    unittest.main()
