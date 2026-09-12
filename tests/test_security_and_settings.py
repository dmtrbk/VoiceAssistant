import unittest
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


if __name__ == "__main__":
    unittest.main()
