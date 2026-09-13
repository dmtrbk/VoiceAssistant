import unittest

from commands import _dispatch_single, execute
from dialogue_repair import (
    LISTEN_ACKS,
    REPAIR_CLOSE,
    REPAIR_FIRST,
    REPAIR_SECOND,
    current_replayable,
    early_dialogue_turn,
    is_oir_phrase,
    is_thinking_pause,
    next_no_match_line,
    remember_spoken,
    repair_count,
    reset,
    should_release_session,
    slot_clarify,
)


class TestDialogueRepair(unittest.TestCase):
    def setUp(self):
        reset()

    def tearDown(self):
        reset()

    def test_thinking_pause_is_silent(self):
        self.assertTrue(is_thinking_pause("мм"))
        self.assertTrue(is_thinking_pause("эм"))
        self.assertEqual(early_dialogue_turn("мм"), ("silent", None))
        self.assertIsNone(early_dialogue_turn("включи свет"))

    def test_oir_without_reply_is_listen_or_silent(self):
        self.assertTrue(is_oir_phrase("что"))
        self.assertTrue(is_oir_phrase("не понял"))
        self.assertFalse(is_oir_phrase("что такое атом"))
        self.assertEqual(early_dialogue_turn("а"), ("silent", None))
        kind, reply = early_dialogue_turn("что")
        self.assertEqual(kind, "speak")
        self.assertIn(reply, LISTEN_ACKS)

    def test_oir_replays_last_meaningful_reply(self):
        remember_spoken("В Москве плюс двадцать, ясно.")
        self.assertEqual(early_dialogue_turn("что"), ("speak", "В Москве плюс двадцать, ясно."))
        self.assertEqual(early_dialogue_turn("а"), ("speak", "В Москве плюс двадцать, ясно."))

    def test_repair_phrases_are_not_replayed(self):
        remember_spoken("Готово.")
        remember_spoken("Ещё раз?")
        self.assertEqual(current_replayable(), "Готово.")

    def test_three_repairs_then_release(self):
        first = next_no_match_line()
        self.assertIn(first, REPAIR_FIRST)
        self.assertEqual(repair_count(), 1)
        self.assertFalse(should_release_session())

        second = next_no_match_line()
        self.assertIn(second, REPAIR_SECOND)
        self.assertFalse(should_release_session())

        third = next_no_match_line()
        self.assertIn(third, REPAIR_CLOSE)
        self.assertTrue(should_release_session())

    def test_successful_reply_resets_repair_count(self):
        next_no_match_line()
        next_no_match_line()
        self.assertEqual(repair_count(), 2)
        remember_spoken("Включаю.")
        self.assertEqual(repair_count(), 0)

    def test_slot_clarify_destination(self):
        self.assertIn(slot_clarify("destination"), ("Куда?", "Куда ехать?", "В какое место?"))

    def test_router_garbled_uses_repair_line(self):
        spoken = []
        slept = _dispatch_single("крпт", spoken.append, channel="voice")
        self.assertFalse(slept)
        self.assertEqual(len(spoken), 1)
        self.assertIn(spoken[0], REPAIR_FIRST)

    def test_router_oir_replays(self):
        remember_spoken("Ищу аптеку рядом.")
        spoken = []
        slept = execute("что", spoken.append, channel="cli")
        self.assertFalse(slept)
        self.assertEqual(spoken, ["Ищу аптеку рядом."])

    def test_router_thinking_pause_is_silent(self):
        spoken = []
        slept = execute("мм", spoken.append, channel="voice")
        self.assertFalse(slept)
        self.assertEqual(spoken, [])


if __name__ == "__main__":
    unittest.main()
