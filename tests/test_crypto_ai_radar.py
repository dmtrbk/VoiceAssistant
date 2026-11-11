# tests/test_crypto_ai_radar.py
# AI-радар: Fail-Open, кэш veto, без блокировки лока.

import os
import tempfile
import time
import unittest
from unittest.mock import patch

from skills.crypto.crypto_ai_radar import AIVerdict, CryptoAIRadar, _parse_json_object, radar_enabled
from skills.crypto.settings import store as settings_store


class TestCryptoAIRadar(unittest.TestCase):
    def setUp(self):
        # Боевые тесты радара — с явным вкл; по умолчанию в проде выкл.
        self._env = patch.dict("os.environ", {"CRYPTO_AI_RADAR": "1"})
        self._env.start()
        self.addCleanup(self._env.stop)

    def test_check_veto_fail_open_empty(self):
        radar = CryptoAIRadar()
        self.assertIsNone(radar.check_veto("CYBER"))

    def test_check_veto_pass_and_block(self):
        radar = CryptoAIRadar()
        now = time.time()
        with radar._lock:
            radar._cache["CYBER"] = AIVerdict(
                ticker="CYBER",
                pass_trade=True,
                score=88,
                veto_reason="OK",
                eval_ts=now,
            )
            radar._cache["DOGS"] = AIVerdict(
                ticker="DOGS",
                pass_trade=False,
                score=30,
                veto_reason="тонкий стакан",
                eval_ts=now,
            )
        self.assertIsNone(radar.check_veto("CYBER"))
        veto = radar.check_veto("DOGS")
        self.assertIsNotNone(veto)
        assert veto is not None
        self.assertIn("тонкий стакан", veto)
        self.assertIn("AI 30/100", veto)

    def test_stale_cache_is_fail_open(self):
        radar = CryptoAIRadar()
        with radar._lock:
            radar._cache["OLD"] = AIVerdict(
                ticker="OLD",
                pass_trade=False,
                score=10,
                veto_reason="излёт",
                eval_ts=time.time() - 9999,
            )
        self.assertIsNone(radar.check_veto("OLD"))

    def test_offer_candidates_snapshot(self):
        radar = CryptoAIRadar()
        radar.offer_candidates(
            [
                {"ticker": "btc", "price": 1, "chg": 1, "turnover": 1e9},
                {"ticker": "CYBER", "price": 0.3, "chg": 12.0, "turnover": 2e6, "high": 0.31},
                {"ticker": "DOGS", "price": 0.001, "chg": 55.0, "turnover": 400_000, "high": 0.0012},
            ]
        )
        with radar._lock:
            names = [row["ticker"] for row in radar._candidates]
        self.assertNotIn("BTC", names)
        self.assertEqual(names, ["CYBER", "DOGS"])

    def test_parse_json_object_strips_noise(self):
        data = _parse_json_object('тут текст {"pass_trade": false, "score": 20, "reason": "памп"} хвост')
        self.assertFalse(data["pass_trade"])
        self.assertEqual(data["score"], 20)

    def test_disabled_env_fail_open(self):
        radar = CryptoAIRadar()
        with radar._lock:
            radar._cache["X"] = AIVerdict(
                ticker="X",
                pass_trade=False,
                score=1,
                veto_reason="нет",
                eval_ts=time.time(),
            )
        with patch.dict("os.environ", {"CRYPTO_AI_RADAR": "0"}):
            self.assertIsNone(radar.check_veto("X"))

    def test_radar_enabled_settings_and_env(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "settings.json")
        old = settings_store._PATH
        settings_store._PATH = path
        settings_store.reset_cache()
        self.addCleanup(lambda: (setattr(settings_store, "_PATH", old), settings_store.reset_cache()))
        env = {k: v for k, v in os.environ.items() if k != "CRYPTO_AI_RADAR"}
        with patch.dict("os.environ", env, clear=True):
            settings_store.update(ai_radar_on=False)
            self.assertFalse(radar_enabled())
            settings_store.update(ai_radar_on=True)
            self.assertTrue(radar_enabled())
        with patch.dict("os.environ", {"CRYPTO_AI_RADAR": "0"}):
            self.assertFalse(radar_enabled())


if __name__ == "__main__":
    unittest.main()
