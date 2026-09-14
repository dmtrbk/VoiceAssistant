import unittest
from context_manager import (
    FSMHandler,
    clear_active_context,
    clear_session_data,
    get_active_context,
    get_context_data,
    get_context_state,
    get_session_data,
    handle_context_input,
    is_in_context,
    pop_context,
    push_context,
    set_active_context,
    set_context_data,
    set_context_state,
    set_session_data,
    update_session_data,
)


class TestContextManager(unittest.TestCase):
    def setUp(self):
        clear_active_context()
        clear_session_data()

    def tearDown(self):
        clear_active_context()
        clear_session_data()

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

    def test_session_state_management(self):
        self.assertEqual(get_session_data(), {})
        set_session_data("user_city", "Екатеринбург")
        self.assertEqual(get_session_data("user_city"), "Екатеринбург")
        self.assertEqual(get_session_data("non_existing", "default_val"), "default_val")

        update_session_data({"volume": 70, "favorite_genre": "synthwave"})
        self.assertEqual(get_session_data("volume"), 70)
        self.assertEqual(get_session_data("favorite_genre"), "synthwave")

        clear_session_data()
        self.assertEqual(get_session_data(), {})

    def test_context_stack_push_pop(self):
        # 1. Start main survey
        main_exit = []
        sub_exit = []

        def main_handler(text, speak):
            speak("Главный опрос")
            return False

        def sub_handler(text, speak):
            if "да" in text:
                speak("Подтверждено")
                return True  # finish sub-context
            speak("Жду подтверждения")
            return False

        push_context("main_survey", main_handler, on_exit=lambda sp: main_exit.append(True))
        self.assertEqual(get_active_context().name, "main_survey")

        # 2. Push nested confirmation step
        push_context("confirm_step", sub_handler, on_exit=lambda sp: sub_exit.append(True))
        self.assertEqual(get_active_context().name, "confirm_step")

        spoken = []
        handled, _ = handle_context_input("что?", spoken.append)
        self.assertTrue(handled)
        self.assertEqual(spoken, ["Жду подтверждения"])
        self.assertEqual(get_active_context().name, "confirm_step")

        # 3. Confirm sub-step -> pop back to main_survey
        spoken.clear()
        handled, _ = handle_context_input("да", spoken.append)
        self.assertTrue(handled)
        self.assertEqual(spoken, ["Подтверждено"])
        self.assertEqual(get_active_context().name, "main_survey")

        # 4. Turn in main_survey
        spoken.clear()
        handled, _ = handle_context_input("дальше", spoken.append)
        self.assertTrue(handled)
        self.assertEqual(spoken, ["Главный опрос"])

        # 5. Clear all
        clear_active_context(call_on_exit=True)
        self.assertFalse(is_in_context())
        self.assertEqual(len(main_exit), 1)

    def test_fsm_handler_state_machine(self):
        fsm = FSMHandler(initial_state="ask_name")

        @fsm.on("ask_name")
        def step_name(text, speak, ctx):
            ctx.set_data("user_name", text.title())
            ctx.set_state("ask_age")
            speak(f"Привет, {text.title()}! Сколько тебе лет?")
            return False

        @fsm.on("ask_age")
        def step_age(text, speak, ctx):
            ctx.set_data("user_age", text)
            ctx.set_state("done")
            name = ctx.get_data("user_name")
            speak(f"Отлично, {name}, возраст {text} записан.")
            return True  # завершить диалог

        set_active_context("profile_fsm", fsm, state="ask_name")
        self.assertEqual(get_context_state(), "ask_name")

        # Step 1: name
        spoken = []
        handled, _ = handle_context_input("дмитрий", spoken.append)
        self.assertTrue(handled)
        self.assertEqual(spoken, ["Привет, Дмитрий! Сколько тебе лет?"])
        self.assertEqual(get_context_state(), "ask_age")
        self.assertEqual(get_context_data("user_name"), "Дмитрий")

        # Step 2: age
        spoken.clear()
        handled, _ = handle_context_input("28", spoken.append)
        self.assertTrue(handled)
        self.assertEqual(spoken, ["Отлично, Дмитрий, возраст 28 записан."])
        self.assertFalse(is_in_context())

    def test_graceful_fallback_in_context(self):
        """
        Если allow_fallback=True и обработчик возвращает False,
        handle_context_input возвращает handled=False, чтобы общий роутер
        обработал реплику (через Groq/навыки), но контекст остался активным.
        """
        def quiz_handler(text, speak):
            if text in {"42", "сорок два"}:
                speak("Правильный ответ!")
                return True
            # Не знаем ответ — не поглощаем ход, отдаём роутеру/Groq
            return False

        set_active_context("quiz", quiz_handler, allow_fallback=True)

        spoken = []
        # Пользователь задает сторонний вопрос
        handled, _ = handle_context_input("какая погода на улице", spoken.append)
        self.assertFalse(handled)  # allow fallback to commands.py
        self.assertTrue(is_in_context())  # context is still alive!

        # Пользователь отвечает на квиз
        handled, _ = handle_context_input("42", spoken.append)
        self.assertTrue(handled)
        self.assertEqual(spoken, ["Правильный ответ!"])
        self.assertFalse(is_in_context())


if __name__ == "__main__":
    unittest.main()
