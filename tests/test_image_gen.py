import base64
import os
import tempfile
import unittest
from unittest.mock import patch

from skills.base import RequestContext
from skills.image_gen import (
    ImageBackendNotConfigured,
    ImageGenSkill,
    ImageQuotaError,
    _default_save_dir,
    expand_prompt,
    extract_prompt,
    fallback_prompt,
    generate_image,
    is_draw_command,
    is_followup_redraw,
)
from skill_settings import OPTIONAL_IDS, skill_id_of


TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


class TestImageGenSkill(unittest.TestCase):
    def setUp(self):
        self.skill = ImageGenSkill()

    def test_draw_command_positive(self):
        self.assertTrue(is_draw_command("нарисуй рыжего кота"))
        self.assertTrue(is_draw_command("джарвис нарисуй мне космос"))
        self.assertTrue(is_draw_command("нарисуй-ка дракона"))
        self.assertTrue(is_draw_command("сгенерируй картинку про горы"))
        self.assertTrue(is_draw_command("создай изображение с роботом"))
        self.assertTrue(is_draw_command("сделай картинку кота"))
        self.assertTrue(is_draw_command("джарвис нарисует рыжего кота"))
        self.assertTrue(is_draw_command("нарисуешь кота"))
        self.assertTrue(is_draw_command("все рыжего кота на деревянном крыльце"))
        self.assertTrue(is_draw_command("рыжего кота на деревянном крыльце"))
        self.assertTrue(self.skill.can_handle(RequestContext(raw_text="нарисуй кота")))

    def test_draw_command_negative(self):
        self.assertFalse(is_draw_command("как нарисовать кота"))
        self.assertFalse(is_draw_command("что нарисовать"))
        self.assertFalse(is_draw_command("умеешь рисовать"))
        self.assertFalse(is_draw_command("можешь рисовать портреты"))
        self.assertFalse(is_draw_command("научи меня рисовать"))
        self.assertFalse(is_draw_command("хочу нарисовать закат"))
        self.assertFalse(is_draw_command("покажи картинку"))
        self.assertFalse(is_draw_command("кота видел на крыльце"))
        self.assertFalse(self.skill.can_handle(RequestContext(raw_text="какая погода")))

    def test_extract_prompt(self):
        self.assertEqual(extract_prompt("нарисуй рыжего кота в шляпе"), "рыжего кота в шляпе")
        self.assertEqual(extract_prompt("джарвис нарисует рыжего кота"), "рыжего кота")
        self.assertEqual(extract_prompt("нарисуй мне космос"), "космос")
        self.assertEqual(extract_prompt("сгенерируй картинку про горы на закате"), "горы на закате")
        self.assertEqual(extract_prompt("создай изображение с роботом"), "роботом")
        self.assertEqual(extract_prompt("нарисуй"), "")
        self.assertEqual(
            extract_prompt("все рыжего кота на деревянном крыльце"),
            "рыжего кота на деревянном крыльце",
        )

    def test_followup(self):
        self.assertTrue(is_followup_redraw("ещё"))
        self.assertTrue(is_followup_redraw("другую картинку"))
        self.assertFalse(is_followup_redraw("нарисуй собаку"))
        self.skill._last_prompt = "кот"
        self.assertTrue(self.skill.accepts_followup(RequestContext(raw_text="ещё раз")))
        self.skill._last_prompt = ""
        self.assertFalse(self.skill.accepts_followup(RequestContext(raw_text="ещё")))

    def test_execute_asks_for_subject(self):
        spoken = []
        self.skill.execute(RequestContext(raw_text="нарисуй", speak=spoken.append))
        self.assertEqual(spoken, ["Что нарисовать?"])

    def test_generate_image_decodes_cloudflare_jpeg(self):
        jpeg = b"\xff\xd8\xff" + b"fakejpeg"
        captured = {}

        class _Resp:
            status_code = 200
            content = b"{}"
            headers = {"content-type": "application/json"}

            def json(self):
                return {"success": True, "result": {"image": base64.b64encode(jpeg).decode()}}

        def fake_post(url, **kwargs):
            captured["url"] = url
            captured["json"] = kwargs["json"]
            captured["headers"] = kwargs["headers"]
            return _Resp()

        raw, model = generate_image(
            "кот",
            post=fake_post,
            steps=4,
            account_id="acc123",
            api_token="tok456",
        )
        self.assertEqual(raw, jpeg)
        self.assertEqual(model, "flux-1-schnell")
        self.assertIn("acc123", captured["url"])
        self.assertIn("flux-1-schnell", captured["url"])
        self.assertEqual(captured["json"]["prompt"], "кот")
        self.assertEqual(captured["json"]["steps"], 4)
        self.assertNotIn("seed", captured["json"])
        self.assertEqual(captured["headers"]["Authorization"], "Bearer tok456")

    def test_generate_image_requires_keys(self):
        with patch.dict(os.environ, {"CLOUDFLARE_ACCOUNT_ID": "", "CLOUDFLARE_API_TOKEN": ""}):
            with self.assertRaises(ImageBackendNotConfigured):
                generate_image("кот", post=lambda *_a, **_k: None)

    def test_generate_image_quota(self):
        class _Resp:
            status_code = 429
            content = b"{}"
            headers = {"content-type": "application/json"}

            def json(self):
                return {"success": False, "errors": [{"message": "quota"}]}

        with self.assertRaises(ImageQuotaError):
            generate_image(
                "кот",
                post=lambda *_a, **_k: _Resp(),
                account_id="acc",
                api_token="tok",
            )

    def test_fallback_prompt_keeps_subject(self):
        text = fallback_prompt("рыжего кота")
        self.assertIn("рыжего кота", text)
        self.assertIn("photorealistic", text)

    def test_expand_prompt_uses_model_and_fallback(self):
        expanded = expand_prompt("рыжего кота", complete=lambda *_a, **_k: "A fluffy ginger tabby cat sitting on a wooden porch")
        self.assertIn("ginger", expanded.lower())
        failed = expand_prompt("рыжего кота", complete=lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("down")))
        self.assertIn("рыжего кота", failed)

    def test_execute_sends_telegram(self):
        spoken = []
        with tempfile.TemporaryDirectory() as tmp:
            skill = ImageGenSkill(save_dir=tmp)
            with (
                patch("skills.image_gen.cloudflare_configured", return_value=True),
                patch("skills.image_gen.expand_prompt", side_effect=lambda prompt: prompt),
                patch("skills.image_gen.generate_image", return_value=(TINY_PNG, "flux-1-schnell")),
                patch("skills.image_gen.telegram_configured", return_value=True),
                patch("skills.image_gen.send_telegram_notification", return_value=True) as send,
            ):
                skill.execute(
                    RequestContext(
                        raw_text="нарисуй рыжего кота",
                        speak=spoken.append,
                        channel="voice",
                    )
                )
            self.assertEqual(send.call_count, 1)
            args, kwargs = send.call_args
            self.assertEqual(args[0], "рыжего кота")
            self.assertTrue(os.path.exists(kwargs["photo_path"]))
            self.assertFalse(kwargs["background"])
        self.assertIn("Рисую.", spoken)
        self.assertIn("Отправил.", spoken)

    def test_execute_without_cloudflare_keys(self):
        spoken = []
        with patch.dict(os.environ, {"CLOUDFLARE_ACCOUNT_ID": "", "CLOUDFLARE_API_TOKEN": ""}):
            self.skill.execute(RequestContext(raw_text="нарисуй рыжего кота", speak=spoken.append))
        self.assertEqual(spoken, ["Нет ключа Cloudflare."])

    def test_default_save_dir_is_pictures_jarvis(self):
        path = _default_save_dir()
        self.assertTrue(path.endswith(os.path.join("Jarvis")))
        self.assertTrue(path.endswith("Изображения/Jarvis") or path.endswith("Pictures/Jarvis"))

    def test_skill_is_optional_toggle(self):
        from skills import image_gen_skill

        self.assertIn("image_gen", OPTIONAL_IDS)
        self.assertEqual(skill_id_of(image_gen_skill), "image_gen")


if __name__ == "__main__":
    unittest.main()
