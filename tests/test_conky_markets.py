import unittest
from unittest import mock

import conky_markets


class TestConkyMarkets(unittest.TestCase):
    def test_plus_is_system_gray(self):
        text = conky_markets.render_lines(1234.4, 20.2)
        self.assertIn("${color c0c0c0}+1 234 ₽", text)
        self.assertIn("${color c0c0c0}+20 $$", text)
        self.assertNotIn("white", text)
        self.assertNotIn("eaeaea", text)
        self.assertNotIn("биржа", text)
        self.assertNotIn("крипта", text)

    def test_lifetime_adds_earn_interest(self):
        with (
            mock.patch("skills.crypto.journal.read_lifetime_pnl", return_value=1.63),
            mock.patch.object(conky_markets, "crypto_earn_interest", return_value=0.42),
        ):
            self.assertAlmostEqual(conky_markets.crypto_lifetime_pnl(refresh=False), 2.05)

    def test_crypto_line_only_numbers(self):
        text = conky_markets.render_lines(10, 12.4, crypto_life=48.6)
        crypto = conky_markets.select_line(text, "crypto")
        self.assertEqual(crypto, "${color c0c0c0}+12 $$ / ${color c0c0c0}+49 $$")

    def test_stocks_line_adds_lifetime_like_crypto(self):
        text = conky_markets.render_lines(
            -32.05,
            5.0,
            stocks_life=1500.4,
            crypto_life=816.0,
        )
        stocks = conky_markets.select_line(text, "stocks")
        self.assertEqual(
            stocks,
            "${color 888888}-32 ₽ / ${color c0c0c0}+1 500 ₽",
        )
        crypto = conky_markets.select_line(text, "crypto")
        self.assertIn("+5 $$ / ${color c0c0c0}+816 $$", crypto)

    def test_minus_is_command_gray(self):
        text = conky_markets.render_lines(-50.2, -1.4)
        self.assertIn("${color 888888}-50 ₽", text)
        self.assertIn("${color 888888}-1 $$", text)

    def test_mixed_signs(self):
        text = conky_markets.render_lines(10, -3)
        self.assertIn("${color c0c0c0}+10 ₽", text)
        self.assertIn("${color 888888}-3 $$", text)

    def test_missing_is_dash_and_plus_color(self):
        text = conky_markets.render_lines(None, None)
        self.assertIn("—", text)
        self.assertEqual(conky_markets.color_for(None), "c0c0c0")
        self.assertEqual(conky_markets.color_for(0), "c0c0c0")

    def test_select_line(self):
        text = conky_markets.render_lines(1, -2)
        self.assertEqual(conky_markets.select_line(text, "stocks"), "${color c0c0c0}+1 ₽")
        self.assertEqual(conky_markets.select_line(text, "crypto"), "${color 888888}-2 $$")

    def test_cache_age_is_90_minutes(self):
        self.assertEqual(conky_markets.CACHE_MAX_AGE, 90 * 60)

    def test_does_not_start_desks(self):
        src = __import__("inspect").getsource(conky_markets)
        self.assertNotIn("start_background", src)
        self.assertNotIn("_trade_auto", src)

    def test_speech_amount_and_mood(self):
        self.assertEqual(conky_markets.speech_amount(1234.4, rub=True), "+1 234 рублей")
        self.assertEqual(conky_markets.speech_amount(-50.2, rub=False), "минус 50 долларов")
        self.assertEqual(conky_markets.speech_amount(0, rub=True), "0 рублей")
        self.assertEqual(conky_markets.speech_amount(None, rub=False), "нет данных")
        briefing = conky_markets.mood_briefing_for_prompt(10, -3)
        self.assertIn("биржа +10 рублей", briefing)
        self.assertIn("крипта минус 3 долларов", briefing)
        self.assertIn("нормально", briefing.lower())
        spoken = conky_markets.mood_speech_from(0, 0, 0, 12)
        self.assertIn("0 рублей за день", spoken)
        self.assertIn("0 рублей с покупки", spoken)
        self.assertIn("0 долларов за сутки", spoken)
        self.assertIn("+12 долларов с покупки", spoken)
        self.assertNotIn("23", spoken)
        empty = conky_markets.mood_briefing_for_prompt(None, None)
        self.assertIn("цифр нет", empty)

    def test_failed_life_keeps_previous_slash(self):
        import os
        import tempfile

        folder = tempfile.mkdtemp()
        pnl = os.path.join(folder, "pnl.json")
        conky_markets.write_pnl_cache(0, 0, 0, 12, cache_path=pnl)
        with (
            mock.patch.object(conky_markets, "PNL_CACHE_PATH", pnl),
            mock.patch.object(conky_markets, "write_cache"),
            mock.patch.object(conky_markets, "fetch_stocks_life", return_value=None),
            mock.patch.object(conky_markets, "fetch_crypto_life", return_value=None),
        ):
            text = conky_markets._store(18, 0, stocks_life=None, crypto_life=None)
        stocks = conky_markets.select_line(text, "stocks")
        crypto = conky_markets.select_line(text, "crypto")
        self.assertIn("+18 ₽ / ", stocks)
        self.assertIn("0 ₽", stocks)
        self.assertIn("0 $$ / ", crypto)
        self.assertIn("+12 $$", crypto)

    def test_failed_day_keeps_previous_number(self):
        import os
        import tempfile

        folder = tempfile.mkdtemp()
        pnl = os.path.join(folder, "pnl.json")
        conky_markets.write_pnl_cache(18, 4, 0, 12, cache_path=pnl)
        with (
            mock.patch.object(conky_markets, "PNL_CACHE_PATH", pnl),
            mock.patch.object(conky_markets, "write_cache"),
            mock.patch.object(conky_markets, "fetch_stocks_life", return_value=None),
            mock.patch.object(conky_markets, "fetch_crypto_life", return_value=None),
        ):
            text = conky_markets._store(None, None, stocks_life=None, crypto_life=None)
        stocks = conky_markets.select_line(text, "stocks")
        crypto = conky_markets.select_line(text, "crypto")
        self.assertIn("+18 ₽", stocks)
        self.assertNotIn("—", stocks)
        self.assertIn("+4 $$", crypto)
        self.assertNotIn("—", crypto)
        saved = conky_markets._read_pnl_raw(pnl)
        self.assertTrue(saved.get("day_stale"))

    def test_how_are_you(self):
        self.assertTrue(conky_markets.is_how_are_you("как дела"))
        self.assertTrue(conky_markets.is_how_are_you("Ну как ты?"))
        self.assertTrue(conky_markets.is_how_are_you("как настроение"))
        self.assertFalse(conky_markets.is_how_are_you("какая погода"))
        self.assertFalse(conky_markets.is_how_are_you("как там акции"))

    def test_mood_followup_detectors(self):
        self.assertTrue(conky_markets.is_today_clarification("это за сегодня?"))
        self.assertTrue(conky_markets.is_today_clarification("за сутки?"))
        self.assertFalse(conky_markets.is_today_clarification("как дела"))
        self.assertTrue(conky_markets.is_all_time_ask("а за всё время?"))
        self.assertTrue(conky_markets.is_all_time_ask("с покупки сколько"))
        self.assertFalse(conky_markets.is_all_time_ask("как дела"))
        self.assertTrue(conky_markets.is_iron_pace_talk("такими темпами скоро переедешь на новое железо"))
        self.assertTrue(conky_markets.is_iron_pace_talk("не скоро"))
        self.assertFalse(conky_markets.is_iron_pace_talk("как дела"))

    def test_today_and_lifetime_briefings(self):
        today = conky_markets.today_clarification_briefing()
        self.assertIn("за сегодня", today.lower())
        self.assertIn("да", today.lower())
        life = conky_markets.lifetime_briefing_for_prompt(100, 20)
        self.assertIn("биржа +100 рублей", life)
        self.assertIn("крипта +20 долларов", life)
        self.assertIn("с покупки", life.lower())
        no_crypto = conky_markets.lifetime_briefing_for_prompt(50, None)
        self.assertIn("по крипте с покупки цифры нет", no_crypto)
        iron = conky_markets.iron_pace_briefing()
        self.assertIn("ради того и тружусь", iron)

    def test_pnl_cache_roundtrip(self):
        import os
        import tempfile

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "pnl.json")
        conky_markets.write_pnl_cache(12.6, -4.1, cache_path=path)
        stocks, crypto = conky_markets.read_pnl_cache(cache_path=path)
        self.assertEqual(stocks, 12.6)
        self.assertEqual(crypto, -4.1)

    def test_cache_fresh_until_journal(self):
        import os
        import tempfile
        import time

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cache = os.path.join(tmp.name, "cache.txt")
        journal = os.path.join(tmp.name, "trades.json")
        with open(cache, "w", encoding="utf-8") as handle:
            handle.write("old")
        os.utime(cache, (time.time(), time.time()))
        with mock.patch.object(conky_markets, "JOURNAL_PATHS", (journal,)):
            self.assertTrue(conky_markets.cache_is_fresh(cache_path=cache))
            time.sleep(0.02)
            with open(journal, "w", encoding="utf-8") as handle:
                handle.write("[]")
            self.assertFalse(conky_markets.cache_is_fresh(cache_path=cache))


if __name__ == "__main__":
    unittest.main()
