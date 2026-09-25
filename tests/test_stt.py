import unittest

from skill_settings import (
    get_stt_mode,
    is_online_stt_enabled,
    normalize_stt_mode,
    set_stt_mode,
    stt_mode_choices,
)
from stt import PhraseAudioBuffer, transcribe_audio


class TestSTT(unittest.TestCase):
    def test_phrase_audio_buffer_lifecycle(self):
        buf = PhraseAudioBuffer(sample_rate=16000, pre_roll_sec=1.0, max_phrase_sec=5.0)
        # 1.0s pre-roll = 32000 bytes.
        # Push 2.0s of idle audio
        buf.add_chunk(b"\x00" * 32000, has_speech=False)
        buf.add_chunk(b"\x00" * 32000, has_speech=False)
        self.assertEqual(len(buf._buffer), 32000)

        # Push speech audio 0.5s = 16000 bytes
        buf.add_chunk(b"\x01" * 16000, has_speech=True)
        self.assertEqual(len(buf._buffer), 48000)

        # Get and reset
        data = buf.get_and_reset()
        self.assertEqual(len(data), 48000)
        self.assertEqual(len(buf._buffer), 0)

        # Clear
        buf.add_chunk(b"\x02" * 1000, has_speech=True)
        buf.clear()
        self.assertEqual(len(buf._buffer), 0)

    def test_stt_settings_vosk_only(self):
        self.assertEqual(normalize_stt_mode("vosk"), "vosk")
        self.assertEqual(normalize_stt_mode("hybrid"), "vosk")
        self.assertEqual(normalize_stt_mode("unknown"), "vosk")

        choices = dict(stt_mode_choices())
        self.assertIn("vosk", choices)
        self.assertNotIn("hybrid", choices)

        set_stt_mode("hybrid")
        self.assertEqual(get_stt_mode(), "vosk")
        self.assertFalse(is_online_stt_enabled())

    def test_transcribe_audio_returns_vosk_text(self):
        res = transcribe_audio(b"\x00" * 16000, fallback_text="включи свет")
        self.assertEqual(res, "включи свет")


if __name__ == "__main__":
    unittest.main()
