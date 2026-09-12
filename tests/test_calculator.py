import unittest
from skills.calculator import (
    CalculatorSkill,
    words_to_number_phrase,
    format_calc_result,
)
from skills.base import RequestContext


class TestCalculator(unittest.TestCase):
    def setUp(self):
        self.skill = CalculatorSkill()

    def test_words_to_number_phrase(self):
        self.assertEqual(words_to_number_phrase("два плюс три"), "2 плюс 3")
        self.assertEqual(words_to_number_phrase("сто двадцать умножить на пять"), "120 умножить на 5")

    def test_format_calc_result(self):
        self.assertEqual(format_calc_result(42.0), "42")
        self.assertEqual(format_calc_result(42), "42")
        self.assertEqual(format_calc_result(3.14), "3.14")
        self.assertEqual(format_calc_result(3.10), "3.1")

    def test_can_handle(self):
        ctx_yes = RequestContext(raw_text="сколько будет два плюс два")
        self.assertTrue(self.skill.can_handle(ctx_yes))

        ctx_math = RequestContext(raw_text="посчитай 15 умножить на 4")
        self.assertTrue(self.skill.can_handle(ctx_math))

        ctx_no = RequestContext(raw_text="какие главные плюсы у линукса")
        self.assertFalse(self.skill.can_handle(ctx_no))

    def test_execution(self):
        spoken = []
        ctx = RequestContext(raw_text="сколько будет двадцать умножить на пять", speak=spoken.append)
        self.skill.execute(ctx)
        self.assertTrue(len(spoken) > 0)
        self.assertIn("100", spoken[0])


if __name__ == "__main__":
    unittest.main()
