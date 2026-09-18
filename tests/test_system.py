import unittest
from unittest.mock import patch

from context_manager import clear_active_context, handle_context_input, is_in_context
from skills.base import RequestContext
from skills.home_assistant import HomeAssistantSkill
from skills.movie_skill import MovieSkill, _is_close_command
from skills.system import SystemSkill, detect_display_power_action, is_shutdown_text


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


class TestShutdownConfirmation(unittest.TestCase):
    def setUp(self):
        self.skill = SystemSkill()
        self.spoken = []
        clear_active_context()

    def tearDown(self):
        clear_active_context()

    def _ask_shutdown(self, popen):
        self.skill.execute(
            RequestContext(raw_text="выключи компьютер", speak=self.spoken.append)
        )
        self.assertEqual(self.spoken, ["Точно выключить компьютер?"])
        popen.assert_not_called()
        self.assertTrue(is_in_context())

    def test_detects_only_pc_shutdown(self):
        self.assertTrue(is_shutdown_text("выключи компьютер"))
        self.assertTrue(is_shutdown_text("отключи пк"))
        self.assertFalse(is_shutdown_text("выключи экран"))
        self.assertFalse(is_shutdown_text("выключи свет"))

    def test_asks_before_shutdown(self):
        with patch("skills.system.subprocess.Popen") as popen:
            self._ask_shutdown(popen)

    def test_yes_shuts_down(self):
        with (
            patch("skills.system.time.sleep"),
            patch("skills.system.subprocess.Popen") as popen,
        ):
            self._ask_shutdown(popen)
            handled, _sleep = handle_context_input("да", self.spoken.append)

        self.assertTrue(handled)
        popen.assert_called_once_with(["shutdown", "now"])
        self.assertEqual(self.spoken[-1], "Выключаю.")
        self.assertFalse(is_in_context())

    def test_no_cancels(self):
        with (
            patch("skills.system.time.sleep"),
            patch("skills.system.subprocess.Popen") as popen,
        ):
            self._ask_shutdown(popen)
            handled, _sleep = handle_context_input("нет", self.spoken.append)

        self.assertTrue(handled)
        popen.assert_not_called()
        self.assertEqual(self.spoken[-1], "Отменил.")
        self.assertFalse(is_in_context())

    def test_other_command_cancels_and_goes_to_skills(self):
        with (
            patch("skills.system.time.sleep"),
            patch("skills.system.subprocess.Popen") as popen,
        ):
            self._ask_shutdown(popen)
            handled, _sleep = handle_context_input("включи свет", self.spoken.append)

        popen.assert_not_called()
        self.assertEqual(self.spoken[-1], "Отменил.")
        # handled=False — фразу должен добрать обычный маршрутизатор.
        self.assertFalse(handled)
        self.assertFalse(is_in_context())


if __name__ == "__main__":
    unittest.main()
