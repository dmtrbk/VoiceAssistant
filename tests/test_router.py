import unittest
from unittest.mock import patch

from commands import _dispatch_single
from skills import wikipedia_skill


class TestRouter(unittest.TestCase):
    def test_disabled_skill_speaks_once(self):
        spoken = []

        def enabled(skill):
            return skill is not wikipedia_skill

        with patch("commands.is_skill_enabled", side_effect=enabled):
            slept = _dispatch_single("что такое атом", spoken.append, channel="cli")
        self.assertFalse(slept)
        self.assertEqual(spoken, ["Этот навык сейчас выключен."])


if __name__ == "__main__":
    unittest.main()
