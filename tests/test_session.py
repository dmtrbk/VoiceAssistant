import unittest
from unittest.mock import patch

from commands import execute
from context_manager import clear_active_context, is_in_context, set_active_context
from runtime_state import register_voice_interrupt, reset_interrupt_hooks, speak_epoch
from session_policy import (
    attention_timeout_sec,
    is_speaker_leak,
    should_expire_attention,
    voice_stop_goes_to_skill,
)
from skills.games import GuessNumberGame


class TestAttentionTimeout(unittest.TestCase):
    def test_quiet_is_12_seconds(self):
        self.assertEqual(
            attention_timeout_sec(media_on=False, has_pending=False, timeout=12.0, timeout_music=5.0),
            12.0,
        )

    def test_music_is_5_seconds(self):
        self.assertEqual(
            attention_timeout_sec(media_on=True, has_pending=False, timeout=12.0, timeout_music=5.0),
            5.0,
        )

    def test_pending_slot_not_shorter_than_12_with_music(self):
        self.assertEqual(
            attention_timeout_sec(media_on=True, has_pending=True, timeout=12.0, timeout_music=5.0),
            12.0,
        )

    def test_expire_only_when_idle_long_enough(self):
        self.assertTrue(
            should_expire_attention(
                is_active=True,
                is_speaking=False,
                is_thinking=False,
                in_context=False,
                idle_for=12.1,
                timeout_sec=12.0,
            )
        )
        self.assertFalse(
            should_expire_attention(
                is_active=True,
                is_speaking=False,
                is_thinking=False,
                in_context=False,
                idle_for=11.9,
                timeout_sec=12.0,
            )
        )

    def test_no_expire_while_speaking_thinking_or_game(self):
        base = dict(
            is_active=True,
            idle_for=30.0,
            timeout_sec=12.0,
        )
        self.assertFalse(should_expire_attention(is_speaking=True, is_thinking=False, in_context=False, **base))
        self.assertFalse(should_expire_attention(is_speaking=False, is_thinking=True, in_context=False, **base))
        self.assertFalse(should_expire_attention(is_speaking=False, is_thinking=False, in_context=True, **base))
        self.assertFalse(
            should_expire_attention(
                is_active=False,
                is_speaking=False,
                is_thinking=False,
                in_context=False,
                idle_for=30.0,
                timeout_sec=12.0,
            )
        )


class TestSpeakerLeak(unittest.TestCase):
    def test_quiet_ducked_audio_is_leak(self):
        self.assertTrue(
            is_speaker_leak(
                50.0,
                False,
                movie_playing=False,
                session_active=True,
                is_movie_control=False,
                min_speech_rms=200.0,
                ducked_or_playing=True,
            )
        )

    def test_loud_voice_with_music_is_not_leak(self):
        self.assertFalse(
            is_speaker_leak(
                400.0,
                False,
                movie_playing=False,
                session_active=True,
                is_movie_control=False,
                min_speech_rms=200.0,
                ducked_or_playing=True,
            )
        )

    def test_wake_word_never_leak(self):
        self.assertFalse(
            is_speaker_leak(
                10.0,
                True,
                movie_playing=True,
                session_active=False,
                is_movie_control=False,
                min_speech_rms=200.0,
                ducked_or_playing=True,
            )
        )

    def test_movie_without_wake_is_leak(self):
        self.assertTrue(
            is_speaker_leak(
                999.0,
                False,
                movie_playing=True,
                session_active=False,
                is_movie_control=False,
                min_speech_rms=200.0,
                ducked_or_playing=False,
            )
        )

    def test_movie_control_in_session_is_not_leak(self):
        self.assertFalse(
            is_speaker_leak(
                999.0,
                False,
                movie_playing=True,
                session_active=True,
                is_movie_control=True,
                min_speech_rms=200.0,
                ducked_or_playing=False,
            )
        )

    def test_filter_off_when_rms_zero(self):
        self.assertFalse(
            is_speaker_leak(
                1.0,
                False,
                movie_playing=False,
                session_active=True,
                is_movie_control=False,
                min_speech_rms=0.0,
                ducked_or_playing=True,
            )
        )


class TestStopInGame(unittest.TestCase):
    def tearDown(self):
        clear_active_context()
        reset_interrupt_hooks()

    def test_policy_routes_stop_to_skill_in_context(self):
        self.assertTrue(voice_stop_goes_to_skill(True))
        self.assertFalse(voice_stop_goes_to_skill(False))

    def test_game_stop_reveals_number_without_media_kill(self):
        game = GuessNumberGame(secret=42)
        spoken = []
        kinds = []
        register_voice_interrupt(kinds.append)
        set_active_context(name="game_more_less", handler=game.handle_turn, timeout_sec=60.0)
        self.assertTrue(is_in_context())
        with patch("player_control.emergency_silence") as silence:
            slept = execute("стоп", spoken.append, channel="telegram")
        self.assertFalse(slept)
        self.assertFalse(is_in_context())
        self.assertEqual(kinds, ["hold"])
        silence.assert_not_called()

    def test_game_turn_stop_phrase(self):
        game = GuessNumberGame(secret=17)
        spoken = []
        self.assertTrue(game.handle_turn("стоп", spoken.append))
        self.assertEqual(spoken, ["Было 17."])


class TestTelegramHushesSpeech(unittest.TestCase):
    def setUp(self):
        reset_interrupt_hooks()
        clear_active_context()
        self.kinds = []
        register_voice_interrupt(self.kinds.append)

    def tearDown(self):
        reset_interrupt_hooks()
        clear_active_context()

    def test_stop_without_game(self):
        before = speak_epoch()
        with patch("player_control.emergency_silence") as silence:
            slept = execute("стоп", lambda _t: None, channel="telegram")
        self.assertTrue(slept)
        self.assertEqual(self.kinds, ["stop"])
        silence.assert_called_once()
        self.assertGreater(speak_epoch(), before)

    def test_hold_zamolchi(self):
        with patch("player_control.emergency_silence") as silence:
            slept = execute("замолчи", lambda _t: None, channel="telegram")
        self.assertFalse(slept)
        self.assertEqual(self.kinds, ["hold"])
        silence.assert_not_called()

    def test_sleep(self):
        with patch("player_control.emergency_silence") as silence:
            slept = execute("спать", lambda _t: None, channel="telegram")
        self.assertTrue(slept)
        self.assertEqual(self.kinds, ["sleep"])
        silence.assert_not_called()

    def test_voice_channel_does_not_hush_here(self):
        with (
            patch("commands._dispatch_single", return_value=False),
            patch("player_control.emergency_silence") as silence,
        ):
            execute("стоп", lambda _t: None, channel="voice")
        self.assertEqual(self.kinds, [])
        silence.assert_not_called()

    def test_cli_stop_also_hushes(self):
        with patch("player_control.emergency_silence"):
            execute("стоп", lambda _t: None, channel="cli")
        self.assertEqual(self.kinds, ["stop"])


class TestAssistantSession(unittest.TestCase):
    def setUp(self):
        import assistant as asst

        self.asst = asst
        asst.is_active = False
        asst.awaiting_followup = False
        asst.is_speaking = False
        asst.is_thinking = False
        asst.last_active_time = 0.0
        reset_interrupt_hooks()
        clear_active_context()

    def tearDown(self):
        self.asst.is_active = False
        self.asst.awaiting_followup = False
        reset_interrupt_hooks()
        clear_active_context()

    def test_wake_session(self):
        with patch.object(self.asst, "put_status") as status, patch.object(self.asst, "volume_ctrl") as vol:
            self.asst.wake_session()
        self.assertTrue(self.asst.is_active)
        status.assert_called_with("listening")
        vol.duck.assert_called_once()

    def test_go_idle_clears_followup_and_restores_volume(self):
        self.asst.is_active = True
        self.asst.awaiting_followup = True
        with (
            patch.object(self.asst, "put_status") as status,
            patch.object(self.asst, "volume_ctrl") as vol,
            patch.object(self.asst, "stop_speaking") as stop,
        ):
            self.asst.go_idle(stop_tts=True)
        self.assertFalse(self.asst.is_active)
        self.assertFalse(self.asst.awaiting_followup)
        stop.assert_called_once_with(to_idle=True)
        vol.restore.assert_called_once()
        status.assert_called_with("idle")

    def test_remote_hold_stops_piper_keeps_session(self):
        self.asst.is_active = True
        with (
            patch.object(self.asst, "stop_speaking") as stop,
            patch.object(self.asst, "put_status"),
            patch.object(self.asst, "volume_ctrl"),
        ):
            self.asst.on_remote_voice_interrupt("hold")
        stop.assert_called_once_with(to_idle=False)
        self.assertTrue(self.asst.is_active)

    def test_remote_stop_stops_piper_and_media(self):
        self.asst.is_active = True
        with (
            patch.object(self.asst, "stop_speaking") as stop,
            patch.object(self.asst, "emergency_silence") as silence,
            patch.object(self.asst, "put_status"),
            patch.object(self.asst, "volume_ctrl"),
        ):
            self.asst.on_remote_voice_interrupt("stop")
        stop.assert_called()
        silence.assert_called_once()
        self.assertFalse(self.asst.is_active)

    def test_music_leak_uses_policy(self):
        with (
            patch.object(self.asst, "is_movie_playing", return_value=False),
            patch.object(self.asst, "volume_ctrl") as vol,
        ):
            vol.is_ducked_or_playing.return_value = True
            self.asst.MIN_SPEECH_RMS = 200.0
            self.asst.is_active = True
            self.assertTrue(self.asst.is_music_leak(40.0, None, "ла-ла-ла"))
            self.assertFalse(self.asst.is_music_leak(40.0, "джарвис", "джарвис стоп"))


if __name__ == "__main__":
    unittest.main()
