import unittest
from triggers import (
    normalize_utterance,
    is_quick_command,
    is_emergency_stop,
    is_hold_interrupt,
    is_sleep_command,
    is_filler,
    is_garbled_utterance,
    is_weak_stt_for_chat,
    is_self_echo,
    split_quick_compound,
)


class TestTriggers(unittest.TestCase):
    def test_normalize_utterance(self):
        self.assertEqual(normalize_utterance("Ёлка, Стоп!"), "елка стоп")
        self.assertEqual(normalize_utterance(""), "")

    def test_emergency_and_session_triggers(self):
        self.assertTrue(is_emergency_stop("стоп"))
        self.assertTrue(is_emergency_stop("джарвис стоп"))
        self.assertFalse(is_emergency_stop("стопка книг"))

        self.assertTrue(is_hold_interrupt("замолчи"))
        self.assertTrue(is_hold_interrupt("подожди"))
        self.assertFalse(is_hold_interrupt("замолчали птицы"))

        self.assertTrue(is_sleep_command("спать"))
        self.assertTrue(is_sleep_command("отбой"))
        self.assertTrue(is_sleep_command("все хватит"))
        self.assertFalse(is_sleep_command("расскажи анекдот"))

    def test_quick_command(self):
        self.assertTrue(is_quick_command("громче"))
        self.assertTrue(is_quick_command("следующий трек"))
        self.assertTrue(is_quick_command("пауза"))
        self.assertFalse(is_quick_command("расскажи анекдот"))

    def test_compound_split(self):
        parts = split_quick_compound("следующий трек и громче")
        self.assertEqual(len(parts), 2)
        self.assertIn("следующий трек", parts[0])
        self.assertIn("громче музыку", parts[1])

        single = split_quick_compound("расскажи про погоду и новости")
        self.assertEqual(len(single), 1)

    def test_filler_and_garbled(self):
        self.assertTrue(is_filler("ээ"))
        self.assertTrue(is_filler("мм"))
        self.assertTrue(is_filler("а"))
        self.assertFalse(is_filler("погода"))

        self.assertTrue(is_garbled_utterance("крпт"))
        self.assertTrue(is_garbled_utterance("аааааааа"))
        self.assertFalse(is_garbled_utterance("включи свет"))
        self.assertFalse(is_garbled_utterance("хорошооо"))

        self.assertFalse(is_weak_stt_for_chat("как дела"))
        self.assertFalse(is_weak_stt_for_chat("расскажи про погоду"))
        self.assertTrue(is_weak_stt_for_chat("крпт шкв брн ткв"))

    def test_self_echo(self):
        spoken = "Сейчас в Москве плюс двадцать градусов, ясно."
        self.assertTrue(is_self_echo("плюс двадцать градусов ясно", spoken))
        self.assertFalse(is_self_echo("включи радио", spoken))


if __name__ == "__main__":
    unittest.main()
