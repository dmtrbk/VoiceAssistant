import unittest
from context_manager import (
    set_active_context,
    clear_active_context,
    is_in_context,
    get_active_context,
    handle_context_input,
)


class TestContextManager(unittest.TestCase):
    def tearDown(self):
        clear_active_context()

    def test_context_lifecycle(self):
        self.assertFalse(is_in_context())
        self.assertIsNone(get_active_context())

        exit_called = []
        def handler(text, speak):
            if "угадал" in text:
                speak("Победа!")
                return True
            speak("Попробуй еще")
            return False

        set_active_context(
            name="test_game",
            handler=handler,
            timeout_sec=30.0,
            on_exit=lambda sp: exit_called.append(True),
        )

        self.assertTrue(is_in_context())
        ctx = get_active_context()
        self.assertIsNotNone(ctx)
        self.assertEqual(ctx.name, "test_game")

        # Test turn
        spoken = []
        handled, should_sleep = handle_context_input("50", spoken.append)
        self.assertTrue(handled)
        self.assertFalse(should_sleep)
        self.assertIn("Попробуй еще", spoken)
        self.assertTrue(is_in_context())

        # Test exit phrase
        exit_spoken = []
        handled, should_sleep = handle_context_input("сдаюсь", exit_spoken.append)
        self.assertTrue(handled)
        self.assertFalse(is_in_context())
        self.assertEqual(len(exit_called), 1)


if __name__ == "__main__":
    unittest.main()
