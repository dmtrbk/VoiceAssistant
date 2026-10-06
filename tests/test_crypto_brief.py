import unittest
from unittest import mock

from skills import ALL_SKILLS, crypto_brief_skill, crypto_skill
from skills.base import RequestContext
from skills.crypto_brief import build_crypto_brief, wants_crypto_brief
from skill_settings import OPTIONAL_IDS, skill_id_of


class TestCryptoBrief(unittest.TestCase):
    def test_phrase(self):
        self.assertTrue(wants_crypto_brief("разбери крипту"))
        self.assertTrue(wants_crypto_brief("подробный отчёт по крипте"))
        self.assertTrue(wants_crypto_brief("подробный отчет"))
        self.assertFalse(wants_crypto_brief("что с криптой"))

    def test_registered_before_crypto(self):
        self.assertLess(ALL_SKILLS.index(crypto_brief_skill), ALL_SKILLS.index(crypto_skill))
        self.assertIn("crypto_brief", OPTIONAL_IDS)
        self.assertEqual(skill_id_of(crypto_brief_skill), "crypto_brief")

    def test_brief_mentions_entry_and_exit(self):
        skill = mock.Mock()
        skill._manual_holds = {"BTC"}
        skill._wallet.side_effect = lambda with_earn=True: (
            (120.0, [{
                "ticker": "DOGE",
                "name": "Дож",
                "value": 40.0,
                "price": 0.10,
                "qty": 400.0,
            }])
            if with_earn
            else (20.0, [])
        )
        skill._alt_bands.return_value = {"ma": 0.11, "z": -1.0}
        with (
            mock.patch("skills.crypto_brief.open_avg_costs", return_value={"DOGE": 0.09}),
            mock.patch("skills.crypto_brief.read_btc_dip", return_value={"qty": 0, "entry": 0}),
            mock.patch("skills.crypto_brief._read_ticker_set", return_value={"ADA"}),
            mock.patch("skills.crypto_brief.read_lifetime_pnl", return_value=10.0),
        ):
            text = build_crypto_brief(skill)
        self.assertIn("На счёте", text)
        self.assertIn("Под проценты", text)
        self.assertIn("Дож", text)
        self.assertIn("от входа", text)
        self.assertIn("До средней осталось", text)
        self.assertIn("Карман пуст", text)
        self.assertIn("Новые покупки закрыты", text)
        self.assertIn("Кардано", text)
        self.assertIn("Закрыто по журналу", text)

    def test_execute_speaks(self):
        spoken: list[str] = []
        with (
            mock.patch("skills.crypto.skill.CryptoSkill", return_value=mock.Mock()),
            mock.patch("skills.crypto_brief.build_portfolio_report", return_value="Готово."),
        ):
            crypto_brief_skill.execute(RequestContext(raw_text="разбери крипту", speak=spoken.append))
        self.assertEqual(spoken, ["Готово."])
