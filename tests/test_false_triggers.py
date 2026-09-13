import unittest
from skills.base import RequestContext
from skills.pentagon import PentagonSkill
from skills.web_search import WebSearchSkill
from runtime_state import bump_session_epoch, session_epoch


class TestFalseTriggers(unittest.TestCase):
    def test_web_search_bare_browser(self):
        skill = WebSearchSkill()
        self.assertFalse(skill.can_handle(RequestContext(raw_text="какой браузер лучше")))
        self.assertFalse(skill.can_handle(RequestContext(raw_text="браузер")))
        self.assertTrue(skill.can_handle(RequestContext(raw_text="открой браузер")))
        self.assertTrue(skill.can_handle(RequestContext(raw_text="найди в интернете погоду")))

    def test_pentagon_bare_matrix(self):
        skill = PentagonSkill()
        self.assertFalse(skill.can_handle(RequestContext(raw_text="матрица")))
        self.assertFalse(skill.can_handle(RequestContext(raw_text="матрицу")))
        self.assertTrue(skill.can_handle(RequestContext(raw_text="включи матрицу")))
        self.assertTrue(skill.can_handle(RequestContext(raw_text="пентагон")))

    def test_session_epoch_bumps(self):
        before = session_epoch()
        after = bump_session_epoch()
        self.assertEqual(after, before + 1)
        self.assertEqual(session_epoch(), after)


if __name__ == "__main__":
    unittest.main()
