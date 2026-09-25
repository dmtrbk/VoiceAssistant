import io
import unittest
import wave
from unittest.mock import MagicMock, patch

from skill_settings import (
    get_stt_mode,
    is_online_stt_enabled,
    normalize_stt_mode,
    set_stt_mode,
    stt_mode_choices,
)
from stt import (
    PhraseAudioBuffer,
    clean_whisper_text,
    pcm_to_wav,
    transcribe_audio,
    transcribe_groq_whisper,
)


class TestSTT(unittest.TestCase):
    def test_pcm_to_wav(self):
        pcm = b"\x00\x00" * 8000  # 0.5 sec of 16kHz 16-bit mono
        wav_bytes = pcm_to_wav(pcm, sample_rate=16000, channels=1, sampwidth=2)
        self.assertTrue(wav_bytes.startswith(b"RIFF"))
        self.assertIn(b"WAVE", wav_bytes)

        # Parse WAV with wave module
        with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
            self.assertEqual(wf.getnchannels(), 1)
            self.assertEqual(wf.getsampwidth(), 2)
            self.assertEqual(wf.getframerate(), 16000)
            self.assertEqual(wf.getnframes(), 8000)

    def test_clean_whisper_text(self):
        self.assertEqual(clean_whisper_text("Привет!"), "Привет!")
        self.assertEqual(clean_whisper_text("[музыка] Привет (смех)"), "Привет")
        self.assertEqual(clean_whisper_text("Продолжение следует..."), "")
        self.assertEqual(clean_whisper_text("..."), "")
        self.assertEqual(clean_whisper_text("«Включи радио»"), "Включи радио")
        self.assertEqual(clean_whisper_text("Субтитры создавал Dima Torzok"), "")

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

    def test_stt_settings(self):
        self.assertEqual(normalize_stt_mode("vosk"), "vosk")
        self.assertEqual(normalize_stt_mode("offline"), "vosk")
        self.assertEqual(normalize_stt_mode("hybrid"), "hybrid")
        self.assertEqual(normalize_stt_mode("unknown"), "hybrid")

        choices = dict(stt_mode_choices())
        self.assertIn("hybrid", choices)
        self.assertIn("vosk", choices)

        with patch.dict("os.environ", {"GROQ_API_KEY": "test_key", "STT_MODE": "hybrid"}):
            set_stt_mode("hybrid")
            self.assertEqual(get_stt_mode(), "hybrid")
            self.assertTrue(is_online_stt_enabled())

            set_stt_mode("vosk")
            self.assertEqual(get_stt_mode(), "vosk")
            self.assertFalse(is_online_stt_enabled())

    def test_transcribe_audio_offline_fallback(self):
        # When online STT is disabled, returns fallback text immediately
        with patch("skill_settings.is_online_stt_enabled", return_value=False):
            res = transcribe_audio(b"\x00" * 16000, fallback_text="включи свет")
            self.assertEqual(res, "включи свет")

    def test_transcribe_audio_online_success(self):
        mock_client = MagicMock()
        mock_client.audio.transcriptions.create.return_value = "Включи свет на кухне"

        with patch("skill_settings.is_online_stt_enabled", return_value=True), \
             patch("skills.groq_client.get_client", return_value=mock_client):
            # Audio with > 0.35s PCM (16000 * 2 * 0.4 = 12800 bytes)
            audio = b"\x00" * 16000
            res = transcribe_audio(audio, fallback_text="включи свет")
            self.assertEqual(res, "Включи свет на кухне")

    def test_transcribe_audio_online_failure_falls_back(self):
        mock_client = MagicMock()
        mock_client.audio.transcriptions.create.side_effect = RuntimeError("Network error")

        with patch("skill_settings.is_online_stt_enabled", return_value=True), \
             patch("skills.groq_client.get_client", return_value=mock_client):
            audio = b"\x00" * 16000
            res = transcribe_audio(audio, fallback_text="включи свет")
            self.assertEqual(res, "включи свет")


if __name__ == "__main__":
    unittest.main()
