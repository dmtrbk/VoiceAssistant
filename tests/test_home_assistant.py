import unittest
from skills.home_assistant import (
    HomeAssistantSkill,
    allows_cover_command,
    allows_lock_command,
)


class TestHomeAssistant(unittest.TestCase):
    def test_lock_and_cover_need_explicit_words(self):
        self.assertFalse(allows_lock_command("включи свет"))
        self.assertFalse(allows_lock_command("выключи дверь"))
        self.assertTrue(allows_lock_command("отопри замок"))
        self.assertTrue(allows_lock_command("заблокируй дверь"))

        self.assertFalse(allows_cover_command("открой окно"))
        self.assertFalse(allows_cover_command("включи гостиную"))
        self.assertTrue(allows_cover_command("открой шторы"))
        self.assertTrue(allows_cover_command("закрой жалюзи"))

    def test_match_skips_lock_without_hint(self):
        skill = HomeAssistantSkill()
        skill._states = [
            {
                "entity_id": "lock.front_door",
                "domain": "lock",
                "name": "Входная дверь",
                "name_norm": "входная дверь",
                "state": "locked",
            },
            {
                "entity_id": "light.hall",
                "domain": "light",
                "name": "Свет в прихожей",
                "name_norm": "свет в прихожей",
                "state": "off",
            },
        ]
        self.assertIsNone(skill._match_entity("включи входную дверь"))
        self.assertIsNotNone(skill._match_entity("отопри входную дверь"))
        light = skill._match_entity("включи свет в прихожей")
        self.assertIsNotNone(light)
        self.assertEqual(light["domain"], "light")


if __name__ == "__main__":
    unittest.main()
