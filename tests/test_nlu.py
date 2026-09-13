import unittest

import nlu
from skills.base import RequestContext
from skills.local_nlu import LocalNLUSkill


class TestNLU(unittest.TestCase):
    def setUp(self):
        self.clf = nlu.NLUClassifier()
        self.assertTrue(self.clf.train())
        self.skill = LocalNLUSkill()

    def test_exact_farewell_and_coin(self):
        intent, conf = self.clf.predict("пока")
        self.assertEqual(intent, "farewell")
        self.assertEqual(conf, 1.0)
        intent, conf = self.clf.predict("подбрось монетку")
        self.assertEqual(intent, "coin")
        self.assertEqual(conf, 1.0)

    def test_smalltalk_does_not_win(self):
        intent, conf = self.clf.predict("как дела")
        self.assertTrue(intent is None or conf < 0.8)
        intent, conf = self.clf.predict("спасибо")
        self.assertTrue(intent is None or conf < 0.8)

    def test_local_skill_accepts_exact(self):
        context = RequestContext(raw_text="до свидания", intent="farewell", confidence=1.0)
        self.assertTrue(self.skill.can_handle(context))

    def test_no_sklearn_import(self):
        with open(nlu.__file__, encoding="utf-8") as handle:
            source = handle.read()
        self.assertNotIn("from sklearn", source)
        self.assertNotIn("import sklearn", source)


if __name__ == "__main__":
    unittest.main()
