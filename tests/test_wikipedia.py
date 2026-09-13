import unittest
from unittest.mock import MagicMock, patch

from skill_settings import OPTIONAL_IDS, skill_id_of
from skills.base import RequestContext
from skills.system import SystemSkill
from skills.wikipedia import WikipediaSkill, extract_wiki_query, is_wiki_command
from skills import wikipedia_skill


class TestWikipediaSkill(unittest.TestCase):
    def setUp(self):
        self.skill = WikipediaSkill()
        self.system = SystemSkill()

    def test_command_positive(self):
        self.assertTrue(is_wiki_command("что такое квант"))
        self.assertTrue(is_wiki_command("кто такой пушкин"))
        self.assertTrue(is_wiki_command("кто такая кюри"))
        self.assertTrue(is_wiki_command("википедия марс"))
        self.assertTrue(self.skill.can_handle(RequestContext(raw_text="что такое атом")))

    def test_command_negative(self):
        self.assertFalse(is_wiki_command("кто ты"))
        self.assertFalse(is_wiki_command("кто ты такой"))
        self.assertFalse(is_wiki_command("как тебя зовут"))
        self.assertFalse(is_wiki_command("какая погода"))
        self.assertFalse(self.skill.can_handle(RequestContext(raw_text="открой терминал")))

    def test_system_no_longer_handles_wiki(self):
        self.assertFalse(self.system.can_handle(RequestContext(raw_text="что такое атом")))
        self.assertFalse(self.system.can_handle(RequestContext(raw_text="кто такой пушкин")))

    def test_extract_query(self):
        self.assertEqual(extract_wiki_query("что такое квантовая физика"), "квантовая физика")
        self.assertEqual(extract_wiki_query("википедия"), "")
        self.assertEqual(extract_wiki_query("кто такой"), "")

    def test_execute_asks_for_query(self):
        spoken = []
        self.skill.execute(RequestContext(raw_text="что такое", speak=spoken.append))
        self.assertEqual(spoken, ["Что найти?"])

    def test_execute_reads_summary(self):
        spoken = []
        page = MagicMock()
        page.exists.return_value = True
        page.summary = "Первое предложение. Второе предложение. Третье."
        wiki = MagicMock()
        wiki.page.return_value = page
        with patch("skills.wikipedia._wiki_client", return_value=wiki):
            self.skill.execute(RequestContext(raw_text="что такое атом", speak=spoken.append))
        self.assertEqual(spoken, ["Ищу.", "Первое предложение. Второе предложение."])

    def test_registered_as_optional(self):
        self.assertIn("wikipedia", OPTIONAL_IDS)
        self.assertEqual(skill_id_of(wikipedia_skill), "wikipedia")


if __name__ == "__main__":
    unittest.main()
