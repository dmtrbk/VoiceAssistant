import base64
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from skills.groq_client import (
    CAP_CHAT,
    CAP_GUARD,
    CAP_IMAGE_GEN,
    CAP_SPEECH,
    CAP_TTS,
    CAP_VISION,
    GROQ_CATALOG_IDS,
    format_groq_models_report,
    groq_draws_images,
    groq_model_caps,
)
from skills.image_gen import POLLINATIONS_URL, generate_image

TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


class TestGroqModelCatalog(unittest.TestCase):
    def test_chat_and_speech_caps(self):
        self.assertEqual(groq_model_caps("openai/gpt-oss-20b"), frozenset({CAP_CHAT}))
        self.assertEqual(groq_model_caps("whisper-large-v3-turbo"), frozenset({CAP_SPEECH}))
        self.assertEqual(groq_model_caps("canopylabs/orpheus-v1-english"), frozenset({CAP_TTS}))
        self.assertEqual(
            groq_model_caps("meta-llama/llama-prompt-guard-2-22m"),
            frozenset({CAP_GUARD}),
        )

    def test_qwen_sees_images_but_does_not_draw(self):
        caps = groq_model_caps("qwen/qwen3.6-27b")
        self.assertIn(CAP_CHAT, caps)
        self.assertIn(CAP_VISION, caps)
        self.assertNotIn(CAP_IMAGE_GEN, caps)
        self.assertFalse(groq_draws_images(["qwen/qwen3.6-27b", "qwen/qwen3.8-27b"]))

    def test_official_catalog_has_no_image_generation(self):
        self.assertFalse(groq_draws_images(GROQ_CATALOG_IDS))
        self.assertFalse(groq_draws_images())
        report = format_groq_models_report(GROQ_CATALOG_IDS)
        self.assertIn("нет моделей рисования", report)
        self.assertIn("Pollinations", report)
        self.assertIn("зрение (понимает картинку, не рисует)", report)
        self.assertNotIn("генерация картинок", report)

    def test_flux_id_would_count_as_drawing(self):
        self.assertTrue(groq_draws_images(["black-forest-labs/flux-schnell"]))
        self.assertIn("можно рисовать через Groq", format_groq_models_report(["flux"]))

    def test_list_models_prints_catalog(self):
        catalog = [
            SimpleNamespace(id="openai/gpt-oss-20b"),
            SimpleNamespace(id="qwen/qwen3.6-27b"),
            SimpleNamespace(id="whisper-large-v3-turbo"),
        ]
        with (
            patch.dict(os.environ, {"GROQ_API_KEY": "test-key"}, clear=False),
            patch("list_models.fetch_model_ids", return_value=[item.id for item in catalog]),
            patch("list_models.load_dotenv"),
        ):
            from list_models import main

            with patch("builtins.print") as printed:
                self.assertEqual(main([]), 0)
            text = "\n".join(str(call.args[0]) for call in printed.call_args_list)
            self.assertIn("openai/gpt-oss-20b", text)
            self.assertIn("нет моделей рисования", text)

    def test_list_models_requires_key(self):
        with (
            patch.dict(os.environ, {"GROQ_API_KEY": ""}, clear=False),
            patch("list_models.load_dotenv"),
            patch("builtins.print") as printed,
        ):
            from list_models import main

            self.assertEqual(main([]), 1)
            self.assertIn("API-ключ", printed.call_args.args[0])

    def test_pixels_come_from_pollinations_not_groq(self):
        self.assertIn("pollinations.ai", POLLINATIONS_URL)
        captured: dict[str, str] = {}

        class _Img:
            status_code = 200
            content = TINY_PNG
            headers = {"content-type": "image/png"}

        def getter(url, **_kwargs):
            captured["url"] = url
            return _Img()

        generate_image("кот", get=getter, models=("flux",), seed=1)
        self.assertIn("pollinations.ai", captured["url"])
        self.assertNotIn("groq.com", captured["url"])


if __name__ == "__main__":
    unittest.main()
