import os
import tempfile
import unittest

from skill_settings import OPTIONAL_IDS, skill_id_of
from skills.base import RequestContext
from skills.site_apps import (
    SiteAppsSkill,
    discover_site_apps,
    match_site_app,
    parse_desktop,
)
from skills.system import SystemSkill
from skills import site_apps_skill

_YOUTUBE = """[Desktop Entry]
Type=Application
Name=YouTube
Exec=/opt/google/chrome/google-chrome --profile-directory=Default --app-id=agimnkijcaahngcdmfeangaknmldooml
"""

_HIDDEN = """[Desktop Entry]
Type=Application
Name=Яндекс Почта
Exec=/opt/yandex/browser/yandex-browser --app-id=bcadigmkecmhhknameopgaidphameinh
NoDisplay=true
"""

_SONG = """[Desktop Entry]
Type=Application
Name=Songsterr Web
Exec=/opt/google/chrome/google-chrome --app-id=dadlbnbkdoflkdiglhklefcegfdnpgbj
"""


class TestSiteApps(unittest.TestCase):
    def setUp(self):
        self.skill = SiteAppsSkill()
        self.system = SystemSkill()

    def test_parse_and_aliases(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "chrome-agimnkijcaahngcdmfeangaknmldooml-Default.desktop")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(_YOUTUBE)
            app = parse_desktop(path)
            self.assertIsNotNone(app)
            self.assertEqual(app.app_id, "agimnkijcaahngcdmfeangaknmldooml")
            self.assertIn("ютуб", app.keywords)
            self.assertIn("youtube", app.keywords)

    def test_skips_hidden(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "chrome-bcadigmkecmhhknameopgaidphameinh-Default.desktop")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(_HIDDEN)
            self.assertIsNotNone(parse_desktop(path))
            self.assertTrue(parse_desktop(path).hidden)
            apps = discover_site_apps([folder])
            self.assertFalse(any(app.app_id.startswith("bcad") for app in apps))

    def test_discovers_new_pwa(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "chrome-dadlbnbkdoflkdiglhklefcegfdnpgbj-Default.desktop")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(_SONG)
            apps = discover_site_apps([folder])
            matched = match_site_app("открой songsterr", apps)
            self.assertIsNotNone(matched)
            self.assertEqual(matched.name, "Songsterr Web")
            self.assertIsNotNone(match_site_app("сонгстер", apps))

    def test_discover_cache_until_desktop_changes(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "chrome-agimnkijcaahngcdmfeangaknmldooml-Default.desktop")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(_YOUTUBE)
            first = discover_site_apps([folder])
            second = discover_site_apps([folder])
            self.assertIs(first, second)
            extra = os.path.join(folder, "chrome-dadlbnbkdoflkdiglhklefcegfdnpgbj-Default.desktop")
            with open(extra, "w", encoding="utf-8") as handle:
                handle.write(_SONG)
            third = discover_site_apps([folder])
            self.assertIsNot(first, third)
            self.assertTrue(any(app.app_id.startswith("dadl") for app in third))

    def test_voice_commands(self):
        self.assertTrue(self.skill.can_handle(RequestContext(raw_text="открой ютуб")))
        self.assertTrue(self.skill.can_handle(RequestContext(raw_text="включи тик ток")))
        self.assertTrue(self.skill.can_handle(RequestContext(raw_text="закрой youtube")))
        self.assertFalse(self.skill.can_handle(RequestContext(raw_text="открой терминал")))

    def test_system_no_longer_owns_them(self):
        self.assertFalse(self.system.can_handle(RequestContext(raw_text="открой ютуб")))
        self.assertFalse(self.system.can_handle(RequestContext(raw_text="тик ток")))
        self.assertFalse(self.system.can_handle(RequestContext(raw_text="запусти htop")))
        self.assertFalse(self.system.can_handle(RequestContext(raw_text="покажи neofetch")))
        self.assertTrue(self.system.can_handle(RequestContext(raw_text="открой терминал")))

    def test_registered(self):
        self.assertIn("site_apps", OPTIONAL_IDS)
        self.assertEqual(skill_id_of(site_apps_skill), "site_apps")


if __name__ == "__main__":
    unittest.main()
