# theme_colors.py
# Палитра окна настроек из темы GNOME / libadwaita, не из зашитых hex.

from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass

_DEFINE = re.compile(r"@define-color\s+(\w+)\s+([^;]+);")
_ACCENT_VAR = re.compile(r"--accent-([a-z]+)\s*:\s*(#[0-9a-fA-F]{3,8})")
_HEX = re.compile(r"^#([0-9a-fA-F]{3,8})$")
_RGBA = re.compile(
    r"^rgba\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*([0-9.]+)\s*\)$",
    re.IGNORECASE,
)
_REF = re.compile(r"^@(\w+)$")

# Имена из org.gnome.desktop.interface accent-color. Значения — libadwaita.
ADW_ACCENTS = {
    "blue": "#3584e4",
    "teal": "#2190a4",
    "green": "#3a944a",
    "yellow": "#c88800",
    "orange": "#ed5b00",
    "red": "#e62d42",
    "pink": "#d56199",
    "purple": "#9141ac",
    "slate": "#6f8396",
    "maia": "#16a085",
}

_THEME_ROOTS = (
    os.path.expanduser("~/.themes"),
    os.path.expanduser("~/.local/share/themes"),
    "/usr/share/themes",
)
_USER_CSS = (
    os.path.expanduser("~/.config/gtk-4.0/gtk.css"),
    os.path.expanduser("~/.config/gtk-3.0/gtk.css"),
)


@dataclass(frozen=True)
class ThemePalette:
    window: str
    card: str
    fg: str
    muted: str
    divider: str
    accent: str
    accent_fg: str
    off_track: str
    off_knob: str
    on_knob: str
    scroll: str
    submenu: str

    def stylesheet(self) -> str:
        p = self
        return f"""
QWidget#root {{
    background: {p.window};
    color: {p.fg};
    font-size: 14px;
}}
QLabel#lead {{
    color: {p.muted};
    font-size: 12px;
}}
QLabel#section {{
    font-size: 13px;
    font-weight: 500;
    color: {p.fg};
}}
QLabel#rowTitle {{
    font-size: 14px;
    font-weight: 400;
    color: {p.fg};
}}
QLabel#hint {{
    color: {p.muted};
    font-size: 12px;
}}
QFrame#card {{
    background: {p.card};
    border: none;
    border-radius: 12px;
}}
QFrame#cardRow {{
    background: transparent;
    border: none;
}}
QFrame#divider {{
    background: {p.divider};
    border: none;
    max-height: 1px;
    min-height: 1px;
}}
QFrame#submenu {{
    background: {p.submenu};
    border: none;
    border-radius: 8px;
}}
QComboBox {{
    background: {p.window};
    color: {p.fg};
    padding: 8px 12px;
    border: 1px solid {p.divider};
    border-radius: 8px;
    min-height: 28px;
}}
QComboBox:hover, QComboBox:focus {{ border-color: {p.accent}; }}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox QAbstractItemView {{
    background: {p.card};
    color: {p.fg};
    border: 1px solid {p.divider};
    selection-background-color: {p.accent};
    padding: 4px;
}}
QCheckBox#toggle {{ background: transparent; }}
QFrame#nav {{
    background: {p.card};
    border: none;
    border-radius: 10px;
}}
QPushButton#navBtn {{
    background: transparent;
    color: {p.muted};
    border: none;
    padding: 8px 12px;
    border-radius: 8px;
    font-size: 13px;
    font-weight: 500;
}}
QPushButton#navBtn:hover {{ color: {p.fg}; }}
QPushButton#navBtn:checked {{
    background: {p.window};
    color: {p.fg};
}}
QStackedWidget {{ background: transparent; }}
QScrollArea {{ background: transparent; border: none; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}
QScrollBar:vertical {{
    background: transparent;
    width: 8px;
    margin: 4px 0;
}}
QScrollBar::handle:vertical {{
    background: {p.scroll};
    border-radius: 4px;
    min-height: 28px;
}}
QScrollBar::handle:vertical:hover {{ background: {p.off_track}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: none; }}
"""


_active: ThemePalette | None = None
_LIVE_TTL = 4.0
_live_loaded_at = 0.0


def current_palette() -> ThemePalette:
    return _active or fallback_palette(dark=True)


def set_active_palette(palette: ThemePalette) -> ThemePalette:
    global _active
    _active = palette
    return palette


def fallback_palette(*, dark: bool, accent: str = "#6f8396") -> ThemePalette:
    if dark:
        window, fg = "#222226", "#eeeeec"
    else:
        window, fg = "#fafafb", "#323233"
    return _from_base(window, fg, accent, dark=dark)


def _gsettings(schema: str, key: str) -> str:
    try:
        raw = subprocess.check_output(
            ["gsettings", "get", schema, key],
            text=True,
            timeout=1.5,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.SubprocessError):
        return ""
    if raw.startswith("'") and raw.endswith("'"):
        return raw[1:-1]
    return raw


def _theme_dir(name: str) -> str:
    if not name:
        return ""
    for root in _THEME_ROOTS:
        path = os.path.join(root, name)
        if os.path.isdir(path):
            return path
    return ""


def _read_head(path: str, limit: int = 16000) -> str:
    try:
        with open(path, encoding="utf-8", errors="ignore") as handle:
            return handle.read(limit)
    except OSError:
        return ""


def parse_gtk_defines(text: str) -> dict[str, str]:
    raw = {match.group(1): match.group(2).strip() for match in _DEFINE.finditer(text)}
    resolved: dict[str, str] = {}

    def resolve(name: str, depth: int = 0) -> str:
        if name in resolved:
            return resolved[name]
        if depth > 8 or name not in raw:
            return ""
        value = raw[name]
        ref = _REF.match(value)
        if ref:
            got = resolve(ref.group(1), depth + 1)
            if got:
                resolved[name] = got
            return got
        if value.lower() == "white":
            resolved[name] = "#ffffff"
            return "#ffffff"
        if value.lower() == "black":
            resolved[name] = "#000000"
            return "#000000"
        resolved[name] = value
        return value

    for key in raw:
        resolve(key)
    return resolved


def parse_accent_vars(text: str) -> dict[str, str]:
    return {match.group(1): match.group(2).lower() for match in _ACCENT_VAR.finditer(text)}


def _hex_to_rgb(color: str) -> tuple[int, int, int] | None:
    match = _HEX.match(color.strip())
    if not match:
        return None
    hex_part = match.group(1)
    if len(hex_part) == 3:
        hex_part = "".join(ch * 2 for ch in hex_part)
    if len(hex_part) < 6:
        return None
    return int(hex_part[0:2], 16), int(hex_part[2:4], 16), int(hex_part[4:6], 16)


def _rgb_to_hex(red: int, green: int, blue: int) -> str:
    return f"#{max(0, min(255, red)):02x}{max(0, min(255, green)):02x}{max(0, min(255, blue)):02x}"


def mix_hex(left: str, right: str, amount: float) -> str:
    start = _hex_to_rgb(left)
    end = _hex_to_rgb(right)
    if start is None or end is None:
        return left
    return _rgb_to_hex(
        int(start[0] + (end[0] - start[0]) * amount),
        int(start[1] + (end[1] - start[1]) * amount),
        int(start[2] + (end[2] - start[2]) * amount),
    )


def resolve_color(value: str, *, onto: str = "#000000") -> str:
    value = (value or "").strip()
    if _hex_to_rgb(value):
        return value.lower() if value.startswith("#") else f"#{value}"
    match = _RGBA.match(value)
    if match:
        overlay = _rgb_to_hex(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        return mix_hex(onto, overlay, float(match.group(4)))
    return ""


def _from_base(window: str, fg: str, accent: str, *, dark: bool) -> ThemePalette:
    card = mix_hex(window, "#ffffff" if dark else "#000000", 0.08)
    if dark:
        muted = mix_hex(fg, window, 0.42)
        divider = mix_hex(window, "#000000", 0.18)
        off_track = mix_hex(card, fg, 0.22)
        off_knob = mix_hex("#ffffff", window, 0.18)
        on_knob = "#ffffff"
        submenu = mix_hex(card, "#000000", 0.12)
    else:
        muted = mix_hex(fg, window, 0.35)
        divider = mix_hex(window, "#000000", 0.08)
        off_track = mix_hex(card, fg, 0.18)
        off_knob = mix_hex("#ffffff", fg, 0.12)
        on_knob = "#ffffff"
        submenu = mix_hex(card, "#000000", 0.04)
    return ThemePalette(
        window=window,
        card=card,
        fg=fg,
        muted=muted,
        divider=divider,
        accent=accent,
        accent_fg="#ffffff",
        off_track=off_track,
        off_knob=off_knob,
        on_knob=on_knob,
        scroll=off_track,
        submenu=submenu,
    )


def _pick_css(theme_dir: str, *, dark: bool) -> list[str]:
    names = ("gtk-3.0/gtk-dark.css", "gtk-3.0/gtk.css") if dark else ("gtk-3.0/gtk.css",)
    found = []
    for name in names:
        path = os.path.join(theme_dir, name)
        if os.path.isfile(path):
            found.append(path)
            break
    for extra in ("gtk-4.0/libadwaita-tweaks.css", "gtk-4.0/libadwaita.css", "gtk-4.0/gtk.css"):
        path = os.path.join(theme_dir, extra)
        if os.path.isfile(path):
            found.append(path)
    return found


def load_palette(
    *,
    gtk_theme: str | None = None,
    color_scheme: str | None = None,
    accent_name: str | None = None,
    include_user_css: bool = True,
) -> ThemePalette:
    global _live_loaded_at
    live = gtk_theme is None and color_scheme is None and accent_name is None
    now = time.time()
    if live and _active is not None and now - _live_loaded_at < _LIVE_TTL:
        return _active
    theme = gtk_theme if gtk_theme is not None else _gsettings(
        "org.gnome.desktop.interface", "gtk-theme"
    )
    scheme = color_scheme if color_scheme is not None else _gsettings(
        "org.gnome.desktop.interface", "color-scheme"
    )
    accent_key = accent_name if accent_name is not None else _gsettings(
        "org.gnome.desktop.interface", "accent-color"
    )
    dark = "prefer-light" not in scheme.lower()
    if "prefer-dark" in scheme.lower() or "dark" in theme.lower():
        dark = True
    if "prefer-light" in scheme.lower():
        dark = False

    defines: dict[str, str] = {}
    accents = dict(ADW_ACCENTS)
    theme_dir = _theme_dir(theme)
    css_paths = _pick_css(theme_dir, dark=dark) if theme_dir else []
    if include_user_css:
        css_paths.extend(path for path in _USER_CSS if os.path.isfile(path))
    for path in css_paths:
        text = _read_head(path)
        defines.update(parse_gtk_defines(text))
        accents.update(parse_accent_vars(text))

    window = resolve_color(defines.get("window_bg_color", ""), onto="#000000")
    fg = resolve_color(defines.get("window_fg_color", ""), onto=window or "#000000")
    if not window or not fg:
        palette = fallback_palette(dark=dark, accent=accents.get(accent_key, ADW_ACCENTS["blue"]))
        result = set_active_palette(palette)
        if live:
            _live_loaded_at = now
        return result

    accent = accents.get(accent_key) or resolve_color(
        defines.get("accent_bg_color", ""), onto=window
    ) or ADW_ACCENTS["blue"]
    card = resolve_color(defines.get("card_bg_color", ""), onto=window)
    header = resolve_color(defines.get("headerbar_bg_color", ""), onto=window)
    if not card or card.lower() == window.lower():
        card = header or mix_hex(window, "#ffffff" if dark else "#000000", 0.08)

    palette = _from_base(window, fg, accent, dark=dark)
    palette = ThemePalette(
        window=window,
        card=card,
        fg=fg,
        muted=palette.muted,
        divider=palette.divider,
        accent=accent,
        accent_fg=resolve_color(defines.get("accent_fg_color", ""), onto=accent) or "#ffffff",
        off_track=palette.off_track,
        off_knob=palette.off_knob,
        on_knob=palette.on_knob,
        scroll=palette.scroll,
        submenu=mix_hex(card, "#000000" if dark else "#ffffff", 0.08),
    )
    result = set_active_palette(palette)
    if live:
        _live_loaded_at = now
    return result
