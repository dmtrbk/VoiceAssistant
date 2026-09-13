import unittest

from theme_colors import (
    ADW_ACCENTS,
    fallback_palette,
    load_palette,
    mix_hex,
    parse_accent_vars,
    parse_gtk_defines,
    resolve_color,
)


class TestThemeColors(unittest.TestCase):
    def test_parse_defines_and_refs(self):
        text = """
        @define-color blue_3 #3584e4;
        @define-color accent_bg_color @blue_3;
        @define-color window_bg_color #222226;
        @define-color window_fg_color white;
        @define-color card_bg_color rgba(255, 255, 255, 0.08);
        """
        colors = parse_gtk_defines(text)
        self.assertEqual(colors["blue_3"], "#3584e4")
        self.assertEqual(colors["accent_bg_color"], "#3584e4")
        self.assertEqual(colors["window_fg_color"], "#ffffff")
        self.assertEqual(resolve_color(colors["card_bg_color"], onto="#222226"), mix_hex("#222226", "#ffffff", 0.08))

    def test_accent_vars(self):
        text = ":root { --accent-slate: #6f8396; --accent-blue: #3584e4; }"
        self.assertEqual(parse_accent_vars(text)["slate"], "#6f8396")

    def test_load_palette_uses_gnome_accent_not_css_blue(self):
        palette = load_palette(
            gtk_theme="adw-gtk3-dark",
            color_scheme="prefer-dark",
            accent_name="slate",
            include_user_css=False,
        )
        self.assertEqual(palette.accent.lower(), ADW_ACCENTS["slate"])
        self.assertRegex(palette.window.lower(), r"^#[0-9a-f]{6}$")
        self.assertNotEqual(palette.window.lower(), ADW_ACCENTS["blue"])
        self.assertIn(palette.window, palette.stylesheet())
        self.assertIn(palette.accent, palette.stylesheet())

    def test_light_fallback(self):
        palette = fallback_palette(dark=False, accent="#3584e4")
        self.assertEqual(palette.window, "#fafafb")
        self.assertEqual(palette.accent, "#3584e4")


if __name__ == "__main__":
    unittest.main()
