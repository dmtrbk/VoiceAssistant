import unittest
from unittest.mock import patch

from commands import execute
from dialogue_repair import early_dialogue_turn, remember_interrupted
from skills.base import RequestContext
from skills.browser_media import BrowserMediaSkill, browser_player_statuses


_NAMES = '''
string "org.mpris.MediaPlayer2.chromium.instance9"
string "org.freedesktop.DBus"
'''


class TestBrowserMedia(unittest.TestCase):
    def test_parse_status(self):
        with (
            patch("skills.browser_media._dbus", return_value=_NAMES),
            patch("skills.browser_media._gdbus", return_value="(<'Playing'>,)"),
        ):
            self.assertEqual(
                browser_player_statuses(),
                [("org.mpris.MediaPlayer2.chromium.instance9", "Playing")],
            )

    def test_stop_pauses_playing_tab(self):
        skill = BrowserMediaSkill()
        ctx = RequestContext(raw_text="останови", speak=lambda _t: None)
        with (
            patch("skills.browser_media.browser_is_playing", return_value=True),
            patch("skills.browser_media.pause_browser", return_value=True) as pause,
        ):
            self.assertTrue(skill.can_handle(ctx))
            skill.execute(ctx)
        pause.assert_called_once()

    def test_continue_resumes_paused_tab(self):
        spoken = []
        with (
            patch("skills.browser_media.browser_is_paused", return_value=True),
            patch("skills.browser_media.browser_is_playing", return_value=False),
            patch("skills.browser_media.resume_browser", return_value=True) as play,
        ):
            self.assertIsNone(early_dialogue_turn("продолжи"))
            execute("продолжи", spoken.append, channel="cli")
        play.assert_called()
        self.assertEqual(spoken, [])

    def test_continue_still_resumes_speech_when_browser_idle(self):
        remember_interrupted("Оборванная фраза.")
        spoken = []
        with patch("skills.browser_media.browser_is_paused", return_value=False):
            execute("продолжи", spoken.append, channel="cli")
        self.assertEqual(spoken, ["Оборванная фраза."])

    def test_stop_with_object_is_not_browser(self):
        skill = BrowserMediaSkill()
        ctx = RequestContext(raw_text="останови музыку", speak=lambda _t: None)
        with patch("skills.browser_media.browser_is_playing", return_value=True):
            self.assertFalse(skill.can_handle(ctx))


if __name__ == "__main__":
    unittest.main()
