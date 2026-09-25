import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from skills import model_catalog


class TestModelCatalog(unittest.TestCase):
    def test_is_free_by_suffix_and_pricing(self):
        self.assertTrue(model_catalog._is_free_row({"id": "qwen/x:free", "pricing": {"prompt": "1"}}))
        self.assertTrue(
            model_catalog._is_free_row({"id": "foo/bar", "pricing": {"prompt": "0", "completion": "0"}})
        )
        self.assertFalse(
            model_catalog._is_free_row({"id": "foo/bar", "pricing": {"prompt": "0.001", "completion": "0"}})
        )

    def test_settings_choices_include_favorites_and_cache(self):
        with tempfile.TemporaryDirectory(dir="/home/real/VoiceAssistant") as tmp:
            path = os.path.join(tmp, "cache.json")
            payload = {
                "fetched_at": 9e12,
                "models": [
                    {"id": "z-ai/glm-5.2:free", "title": "GLM 5.2"},
                    {"id": "qwen/qwen3.8-27b:free", "title": "Qwen"},
                ],
                "probed": {"z-ai/glm-5.2:free": "ok", "qwen/qwen3.8-27b:free": "fail"},
            }
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
            with patch.object(model_catalog, "CACHE_PATH", path):
                # cached_free_models подгружает probed в модульный статус
                model_catalog.cached_free_models(allow_stale=True)
                rows = model_catalog.settings_model_choices("custom/model")
            ids = [mid for mid, _title in rows]
            self.assertEqual(ids[0], model_catalog.FAST_MODEL)
            self.assertIn("z-ai/glm-5.2:free", ids)
            self.assertIn("custom/model", ids)
            labels = dict(rows)
            self.assertTrue(labels["z-ai/glm-5.2:free"].startswith("✓"))
            self.assertTrue(labels["qwen/qwen3.8-27b:free"].startswith("✗"))

    def test_fetch_free_models_parses_api(self):
        fake = {
            "data": [
                {
                    "id": "qwen/qwen3.8-27b:free",
                    "name": "Qwen3.8 27B",
                    "pricing": {"prompt": "0", "completion": "0"},
                    "architecture": {"output_modalities": ["text"]},
                },
                {
                    "id": "paid/model",
                    "name": "Paid",
                    "pricing": {"prompt": "0.1", "completion": "0.2"},
                    "architecture": {"output_modalities": ["text"]},
                },
            ]
        }
        response = MagicMock()
        response.raise_for_status = MagicMock()
        response.json.return_value = fake
        with tempfile.TemporaryDirectory(dir="/home/real/VoiceAssistant") as tmp:
            path = os.path.join(tmp, "cache.json")
            with (
                patch.object(model_catalog, "CACHE_PATH", path),
                patch.object(model_catalog, "_api_key", return_value="test-key"),
                patch.object(model_catalog, "_api_base", return_value="https://openrouter.ai/api/v1"),
                patch("skills.model_catalog.requests.get", return_value=response),
            ):
                rows = model_catalog.fetch_free_models(force=True)
            ids = [mid for mid, _title in rows]
            self.assertEqual(ids, ["qwen/qwen3.8-27b:free"])
            self.assertTrue(os.path.exists(path))

    def test_probe_model_ok_and_fail(self):
        client = MagicMock()
        client.chat.completions.create.return_value = MagicMock()
        with patch.object(model_catalog, "get_client", return_value=client):
            self.assertEqual(model_catalog.probe_model("a/b:free"), "ok")
        client.chat.completions.create.side_effect = RuntimeError("down")
        with patch.object(model_catalog, "get_client", return_value=client):
            self.assertEqual(model_catalog.probe_model("a/b:free"), "fail")
        with patch.object(model_catalog, "get_client", return_value=None):
            self.assertEqual(model_catalog.probe_model("a/b:free"), "fail")


if __name__ == "__main__":
    unittest.main()
