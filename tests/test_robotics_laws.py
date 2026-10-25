# tests/test_robotics_laws.py

import unittest

from robotics_laws import LAW_0, LAW_1, LAW_2, LAW_3, prepend_to_prompt, prompt_block


class TestRoboticsLaws(unittest.TestCase):
    def test_prompt_block_has_all_laws(self):
        text = prompt_block()
        for law in (LAW_0, LAW_1, LAW_2, LAW_3):
            self.assertIn(law, text)

    def test_prepend_once(self):
        once = prepend_to_prompt("Ты Джарвис.")
        twice = prepend_to_prompt(once)
        self.assertEqual(once.count(LAW_0), 1)
        self.assertEqual(twice, once)

    def test_persona_includes_laws(self):
        from skills.persona import get_preset_prompt, PERSONA_JARVIS

        prompt = get_preset_prompt(PERSONA_JARVIS)
        self.assertIn(LAW_0, prompt)
        self.assertIn(LAW_1, prompt)
        self.assertIn("Джарвис", prompt)


if __name__ == "__main__":
    unittest.main()
