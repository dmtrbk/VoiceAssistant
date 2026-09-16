import unittest
from unittest.mock import patch

import crypto_advisor
from skills.crypto import CryptoSkill, parse_ai_alloc


class TestCryptoAdvisor(unittest.TestCase):
    def test_compose_includes_alloc_and_momentum(self):
        rows = [
            {
                "ticker": "BTC",
                "name": "Биткоин",
                "chg_7": 5.5,
                "trend": "выше",
            },
            {
                "ticker": "ETH",
                "name": "Эфир",
                "chg_7": -2.0,
                "trend": "ниже",
            },
        ]
        text = crypto_advisor.compose_message("Держи крупные.", {"BTC": 70, "ETH": 30}, rows)
        self.assertIn("Держи крупные.", text)
        self.assertIn("Биткоин 70%", text)
        self.assertIn("Моментум 7 дней", text)
        self.assertIn("осторожно", text)

    def test_parse_ignores_unknown_and_stables(self):
        alloc, why = parse_ai_alloc(
            '{"alloc": {"btc": 50, "USDT": 20, "XYZ": 30}, "why": "Кэш."}',
            {"BTC", "ETH"},
        )
        self.assertEqual(list(alloc), ["BTC"])
        self.assertEqual(alloc["BTC"], 100.0)
        self.assertEqual(why, "Кэш.")

    def test_run_does_not_place_orders(self):
        skill = CryptoSkill()
        with (
            patch.object(skill, "desk_candidates", return_value=[{"ticker": "BTC", "name": "Биткоин", "chg_7": 1.0, "trend": "выше"}]),
            patch.object(skill, "ai_pick_alloc", return_value=({"BTC": 100.0}, "Только биткоин.")),
            patch.object(skill, "_place_order") as place,
            patch("crypto_advisor.telegram_configured", return_value=False),
        ):
            text = crypto_advisor.run(skill=skill, to_telegram=False, to_stdout=False)
        place.assert_not_called()
        self.assertIn("Только биткоин.", text)
