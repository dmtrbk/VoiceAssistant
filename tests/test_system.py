import unittest
from unittest.mock import patch

from skills.base import RequestContext
from skills.home_assistant import HomeAssistantSkill
from skills.movie_skill import MovieSkill, _is_close_command
from skills.system import SystemSkill, detect_display_power_action


class TestDisplayPowerCommand(unittest.TestCase):
    def setUp(self):
        self.skill = SystemSkill()

    def test_detect_off_phrases(self):
        for phrase in (
            "выключи экран",
            "Выключи экран",
            "погаси экран",
            "отключи монитор",
            "выруби дисплей",
            "экран выключи",
            "выключи экран пожалуйста",
            "выключи экран компьютера",
        ):
            self.assertEqual(detect_display_power_action(phrase), "off", phrase)
            self.assertTrue(self.skill.can_handle(RequestContext(raw_text=phrase)), phrase)

    def test_detect_on_phrases(self):
        self.assertEqual(detect_display_power_action("включи экран"), "on")
        self.assertEqual(detect_display_power_action("включи монитор"), "on")
        self.assertTrue(self.skill.can_handle(RequestContext(raw_text="включи экран")))

    def test_detect_negative(self):
        for phrase in (
            "настройки экрана",
            "открой настройки экрана",
            "на весь экран",
            "полный экран",
            "выключи системный монитор",
            "выключи свет",
            "выключи охрану",
            "выключи компьютер",
            "что на экране",
            "экран",
        ):
            self.assertIsNone(detect_display_power_action(phrase), phrase)

    def test_execute_turns_display_off(self):
        spoken = []
        with (
            patch("skills.system.time.sleep"),
            patch("skills.system.set_display_power", return_value=True) as power,
            patch("skills.ai_chat.log_system_action") as logged,
        ):
            self.skill.execute(RequestContext(raw_text="выключи экран", speak=spoken.append))
        power.assert_called_once_with(False)
        self.assertEqual(spoken, ["Выключаю."])
        logged.assert_called_once()

    def test_execute_turns_display_on(self):
        spoken = []
        with (
            patch("skills.system.set_display_power", return_value=True) as power,
            patch("skills.ai_chat.log_system_action"),
        ):
            self.skill.execute(RequestContext(raw_text="включи экран", speak=spoken.append))
        power.assert_called_once_with(True)
        self.assertEqual(spoken, ["Включил."])

    def test_execute_reports_failure(self):
        spoken = []
        with (
            patch("skills.system.time.sleep"),
            patch("skills.system.set_display_power", return_value=False),
        ):
            self.skill.execute(RequestContext(raw_text="выключи экран", speak=spoken.append))
        self.assertEqual(spoken, ["Выключаю.", "Не вышло."])

    def test_screen_off_does_not_shutdown_pc(self):
        spoken = []
        with (
            patch("skills.system.time.sleep"),
            patch("skills.system.set_display_power", return_value=True),
            patch("skills.ai_chat.log_system_action"),
            patch("skills.system.subprocess.Popen") as popen,
        ):
            self.skill.execute(
                RequestContext(raw_text="выключи экран компьютера", speak=spoken.append)
            )
        popen.assert_not_called()

    def test_home_assistant_does_not_steal_screen(self):
        ha = HomeAssistantSkill()
        ha._states = [
            {
                "entity_id": "switch.screen",
                "domain": "switch",
                "name": "Экран",
                "name_norm": "экран",
                "state": "on",
            }
        ]
        self.assertFalse(ha.can_handle(RequestContext(raw_text="выключи экран")))

    def test_movie_player_does_not_close_on_screen_off(self):
        with patch("skills.movie_skill._player_alive", return_value=True):
            self.assertFalse(_is_close_command("выключи экран"))
            self.assertFalse(MovieSkill().can_handle(RequestContext(raw_text="выключи экран")))


if __name__ == "__main__":
    unittest.main()
