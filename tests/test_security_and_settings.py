import os
import unittest
from unittest.mock import patch

from skill_settings import (
    _env_voice_trade_default,
    get_effective_groq_model,
    is_cursor_running,
)
from skills.groq_client import FAST_MODEL, STRONG_MODEL, groq_model_choices, model_chain
from skills.security import SecuritySkill
from skills.assistant_settings import AssistantSettingsSkill
from skills.base import RequestContext


class TestSecurityAndSettings(unittest.TestCase):
    def test_security_arm_disarm(self):
        sec = SecuritySkill()

        # Arm
        self.assertTrue(sec._is_arm_command("включи охрану"))
        self.assertTrue(sec._is_arm_command("я ухожу"))
        self.assertTrue(sec._is_arm_command("активируй режим охраны"))
        self.assertTrue(sec._is_arm_command("джарвис я ухожу"))
        self.assertTrue(sec._is_arm_command("режим охраны"))

        # Substrings in casual conversation must not trigger arm
        self.assertFalse(sec._is_arm_command("когда я ухожу из дома"))
        self.assertFalse(sec._is_arm_command("что такое режим охраны"))
        self.assertFalse(sec._is_arm_command("я ухожу от этой темы"))

        # Disarm
        self.assertTrue(sec._is_disarm_command("я тут"))
        self.assertTrue(sec._is_disarm_command("я дома"))
        self.assertTrue(sec._is_disarm_command("выключи охрану"))
        self.assertTrue(sec._is_disarm_command("джарвис я тут"))

        # Substrings in casual conversation must not trigger disarm
        self.assertFalse(sec._is_disarm_command("я тут подумал о планах"))
        self.assertFalse(sec._is_disarm_command("я тут сижу работаю"))
        self.assertFalse(sec._is_disarm_command("я дома сделаю потом"))

    def test_assistant_settings_skill(self):
        asst = AssistantSettingsSkill()

        self.assertTrue(asst.can_handle(RequestContext(raw_text="открой настройки")))
        self.assertTrue(asst.can_handle(RequestContext(raw_text="настройки джарвиса")))
        self.assertTrue(asst.can_handle(RequestContext(raw_text="параметры ассистента")))

        # System settings should not be hijacked
        self.assertFalse(asst.can_handle(RequestContext(raw_text="открой системные настройки")))
        self.assertFalse(asst.can_handle(RequestContext(raw_text="открой настройки сети")))
        self.assertFalse(asst.can_handle(RequestContext(raw_text="настройки экрана")))

    def test_voice_trade_env_default_off(self):
        with patch.dict(os.environ, {"TINKOFF_VOICE_TRADE": ""}, clear=False):
            self.assertFalse(_env_voice_trade_default())
        with patch.dict(os.environ, {"TINKOFF_VOICE_TRADE": "true"}, clear=False):
            self.assertTrue(_env_voice_trade_default())

    def test_groq_model_chain_puts_strong_first(self):
        chain = model_chain(STRONG_MODEL)
        self.assertEqual(chain[0], STRONG_MODEL)
        self.assertIn(FAST_MODEL, chain)
        labels = [title for _mid, title in groq_model_choices()]
        self.assertTrue(any("Быстрая" in title for title in labels))
        self.assertTrue(any("Сильная" in title for title in labels))

    def test_effective_model_stays_fast_while_cursor_open(self):
        with (
            patch("skill_settings.get_groq_model", return_value=STRONG_MODEL),
            patch("skill_settings.is_cursor_running", return_value=True),
        ):
            self.assertEqual(get_effective_groq_model(), FAST_MODEL)
        with (
            patch("skill_settings.get_groq_model", return_value=STRONG_MODEL),
            patch("skill_settings.is_cursor_running", return_value=False),
        ):
            self.assertEqual(get_effective_groq_model(), STRONG_MODEL)
        self.assertIsInstance(is_cursor_running(), bool)


if __name__ == "__main__":
    unittest.main()
