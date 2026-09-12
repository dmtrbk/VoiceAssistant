import unittest
from skills.text_utils import (
    norm,
    plural,
    words_to_number,
    fuzzy_phrase_match,
    has_word,
    has_any_word,
)


class TestTextUtils(unittest.TestCase):
    def test_norm(self):
        self.assertEqual(norm("  Привет, Мир! Ёлка.  "), "привет, мир! елка.")
        self.assertEqual(norm(""), "")
        self.assertEqual(norm(None), "")

    def test_plural(self):
        self.assertEqual(plural(1, "лот", "лота", "лотов"), "лот")
        self.assertEqual(plural(2, "лот", "лота", "лотов"), "лота")
        self.assertEqual(plural(5, "лот", "лота", "лотов"), "лотов")
        self.assertEqual(plural(11, "лот", "лота", "лотов"), "лотов")
        self.assertEqual(plural(21, "лот", "лота", "лотов"), "лот")
        self.assertEqual(plural(24, "лот", "лота", "лотов"), "лота")

    def test_words_to_number(self):
        self.assertEqual(words_to_number("пять"), 5)
        self.assertEqual(words_to_number("двадцать три"), 23)
        self.assertEqual(words_to_number("сто пятьдесят"), 150)
        self.assertEqual(words_to_number("не число"), 0)

    def test_fuzzy_phrase_match(self):
        self.assertTrue(fuzzy_phrase_match("какая сегодня погода на улице", "погода на улице", min_ratio=0.8))
        self.assertFalse(fuzzy_phrase_match("включи музыку", "выключи свет", min_ratio=0.8))

    def test_has_word_and_has_any_word(self):
        self.assertTrue(has_word("включи свет пожалуйста", "свет"))
        self.assertFalse(has_word("включи светильник", "свет"))
        self.assertTrue(has_any_word("закрой окно", ("окно", "дверь")))
        self.assertFalse(has_any_word("открой дверь", ("окно", "шторы")))


if __name__ == "__main__":
    unittest.main()
