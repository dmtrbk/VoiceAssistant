import unittest
from unittest.mock import MagicMock, patch

from cursor_bridge import ask_cursor, telegram_chunks
from cursor_sdk import CursorAgentError
import telegram_listener


class TestCursorTelegram(unittest.TestCase):
    def test_chunks_split_long_text(self):
        parts = telegram_chunks("а" * 10, limit=4)
        self.assertEqual(parts, ["аааа", "аааа", "аа"])

    def test_missing_key_does_not_call_sdk(self):
        with patch.dict("os.environ", {"CURSOR_API_KEY": ""}, clear=False):
            self.assertEqual(ask_cursor("привет"), "В .env нет CURSOR_API_KEY.")

    def test_resume_sends_follow_up_without_preamble(self):
        agent = MagicMock()
        agent.agent_id = "agent-1"
        run = MagicMock()
        run.id = "run-1"
        run.wait.return_value = MagicMock(status="finished", result="Готово.")
        agent.send.return_value = run
        cm = MagicMock()
        cm.__enter__.return_value = agent
        cm.__exit__.return_value = False

        with (
            patch.dict("os.environ", {"CURSOR_API_KEY": "cursor_test"}, clear=False),
            patch("cursor_bridge._load_agent_id", return_value="agent-1"),
            patch("cursor_bridge._save_agent_id") as save,
            patch("cursor_sdk.Agent.resume", return_value=cm) as resume,
            patch("cursor_sdk.Agent.create") as create,
        ):
            self.assertEqual(ask_cursor("ещё раз"), "Готово.")
        resume.assert_called_once()
        create.assert_not_called()
        sent = agent.send.call_args.args[0]
        self.assertEqual(sent, "ещё раз")
        save.assert_called_with("agent-1")

    def test_resume_failure_starts_a_new_chat(self):
        agent = MagicMock()
        agent.agent_id = "agent-2"
        run = MagicMock()
        run.id = "run-2"
        run.wait.return_value = MagicMock(status="finished", result="Новый.")
        agent.send.return_value = run
        cm = MagicMock()
        cm.__enter__.return_value = agent
        cm.__exit__.return_value = False

        with (
            patch.dict("os.environ", {"CURSOR_API_KEY": "cursor_test"}, clear=False),
            patch("cursor_bridge._load_agent_id", return_value="stale"),
            patch("cursor_bridge._forget_agent_id") as forget,
            patch("cursor_bridge._save_agent_id"),
            patch("cursor_sdk.Agent.resume", side_effect=CursorAgentError("gone")),
            patch("cursor_sdk.Agent.create", return_value=cm) as create,
        ):
            self.assertEqual(ask_cursor("заново"), "Новый.")
        forget.assert_called_once()
        create.assert_called_once()
        self.assertIn("заново", agent.send.call_args.args[0])

    def test_listener_uses_cursor_when_enabled(self):
        with (
            patch("telegram_listener.send_reply") as send,
            patch("skill_settings.is_cursor_telegram_enabled", return_value=True),
            patch("cursor_bridge.ask_cursor", return_value="Ответ агента"),
            patch("telegram_listener.execute_command") as execute,
        ):
            telegram_listener.dispatch_telegram_text("привет", "1")
        execute.assert_not_called()
        self.assertEqual(send.call_args_list[0].args[1], "Думаю.")
        self.assertEqual(send.call_args_list[1].args[1], "Ответ агента")

    def test_listener_keeps_jarvis_when_disabled(self):
        with (
            patch("skill_settings.is_cursor_telegram_enabled", return_value=False),
            patch("telegram_listener.execute_command") as execute,
            patch("cursor_bridge.ask_cursor") as ask,
        ):
            telegram_listener.dispatch_telegram_text("который час", "1")
        ask.assert_not_called()
        execute.assert_called_once()
        self.assertEqual(execute.call_args.kwargs["channel"], "telegram")


if __name__ == "__main__":
    unittest.main()
