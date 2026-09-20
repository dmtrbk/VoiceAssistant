# tests/test_crypto_sentiment.py
# Fear & Greed для советника: кэш, формат, без сети в тестах.

import json
import os
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

from skills.crypto import sentiment


class TestFearGreed(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "fng.json")

    def test_format(self):
        self.assertEqual(
            sentiment.format_fear_greed({"value": 28, "label": "Fear"}),
            "Fear & Greed: 28 (Fear)",
        )
        self.assertEqual(sentiment.format_fear_greed(None), "")

    def test_uses_fresh_cache(self):
        payload = {"value": 55, "label": "Greed", "ts": time.time()}
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        with patch.object(sentiment, "fetch_fear_greed") as fetch:
            snap = sentiment.fear_greed_snapshot(cache_path=self.path)
        fetch.assert_not_called()
        self.assertEqual(snap, {"value": 55, "label": "Greed"})

    def test_fetches_and_writes_cache(self):
        session = MagicMock()
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {
            "data": [{"value": "22", "value_classification": "Extreme Fear"}]
        }
        session.get.return_value = resp
        snap = sentiment.fear_greed_snapshot(cache_path=self.path, session=session, force=True)
        self.assertEqual(snap, {"value": 22, "label": "Extreme Fear"})
        with open(self.path, encoding="utf-8") as handle:
            raw = json.load(handle)
        self.assertEqual(raw["value"], 22)
        self.assertEqual(raw["label"], "Extreme Fear")

    def test_stale_cache_on_network_fail(self):
        payload = {"value": 40, "label": "Fear", "ts": time.time() - 7 * 3600}
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        with patch.object(sentiment, "fetch_fear_greed", return_value=None):
            snap = sentiment.fear_greed_snapshot(cache_path=self.path, force=True)
        self.assertEqual(snap, {"value": 40, "label": "Fear"})

    def test_alloc_facts_includes_fng(self):
        from skills.crypto.skill import CryptoSkill

        skill = CryptoSkill()
        with (
            patch.object(skill, "_api_key", ""),
            patch(
                "skills.crypto.sentiment.fear_greed_line",
                return_value="Fear & Greed: 30 (Fear)",
            ),
            patch("skills.crypto.journal.cooldown_tickers", return_value=set()),
        ):
            text = skill._alloc_facts([])
        self.assertIn("Fear & Greed: 30 (Fear)", text)


if __name__ == "__main__":
    unittest.main()
