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
