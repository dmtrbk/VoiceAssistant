# skills/browser_media.py
# Голос «останови» / «продолжи» для вкладки браузера (MPRIS Chrome / Firefox / Яндекс).

from __future__ import annotations

import logging
import re
import subprocess

from skills.base import BaseSkill, RequestContext

logger = logging.getLogger(__name__)

_NAME_RE = re.compile(
    r'"(org\.mpris\.MediaPlayer2\.(?:chromium|chrome|firefox|yandex)[^"]*)"'
)
_STATUS_RE = re.compile(r"<(?:'(Playing|Paused|Stopped)'|\"(Playing|Paused|Stopped)\")>")
_DEVNULL = subprocess.DEVNULL

PAUSE_PHRASES = frozenset({"останови"})
RESUME_PHRASES = frozenset({"продолжи"})


def _dbus(*args: str) -> str:
    try:
        completed = subprocess.run(
            ["dbus-send", "--session", "--dest=org.freedesktop.DBus", "--type=method_call", "--print-reply", *args],
            capture_output=True,
            text=True,
            timeout=2.0,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.debug("[Браузер] dbus-send: %s", exc)
        return ""
    return completed.stdout or ""


def _gdbus(dest: str, method: str, *extra: str) -> str:
    try:
        completed = subprocess.run(
            [
                "gdbus", "call", "--session",
                "--dest", dest,
                "--object-path", "/org/mpris/MediaPlayer2",
                "--method", method,
                *extra,
            ],
            capture_output=True,
            text=True,
            timeout=2.0,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.debug("[Браузер] gdbus: %s", exc)
        return ""
    if completed.returncode != 0:
        return ""
    return completed.stdout or ""


def browser_player_statuses() -> list[tuple[str, str]]:
    """[(bus name, Playing|Paused|Stopped), ...] только для браузерных плееров."""
    raw = _dbus("/org/freedesktop/DBus", "org.freedesktop.DBus.ListNames")
    rows: list[tuple[str, str]] = []
    for dest in _NAME_RE.findall(raw):
        status_raw = _gdbus(
            dest,
            "org.freedesktop.DBus.Properties.Get",
            "org.mpris.MediaPlayer2.Player",
            "PlaybackStatus",
        )
        match = _STATUS_RE.search(status_raw)
        if match:
            rows.append((dest, match.group(1) or match.group(2)))
    return rows


def browser_is_paused() -> bool:
    return any(status == "Paused" for _dest, status in browser_player_statuses())


def browser_is_playing() -> bool:
    return any(status == "Playing" for _dest, status in browser_player_statuses())


def _call_ok(dest: str, action: str) -> bool:
    try:
        completed = subprocess.run(
            [
                "gdbus", "call", "--session",
                "--dest", dest,
                "--object-path", "/org/mpris/MediaPlayer2",
                "--method", f"org.mpris.MediaPlayer2.Player.{action}",
            ],
            capture_output=True,
            text=True,
            timeout=2.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def pause_browser() -> bool:
    """Ставит на паузу играющую вкладку. False, если ничего не играет."""
    playing = [dest for dest, status in browser_player_statuses() if status == "Playing"]
    if not playing:
        return False
    return any(_call_ok(dest, "Pause") for dest in playing)


def resume_browser() -> bool:
    """Продолжает вкладку на паузе. False, если паузы нет."""
    paused = [dest for dest, status in browser_player_statuses() if status == "Paused"]
    if not paused:
        return False
    return any(_call_ok(dest, "Play") for dest in paused)


class BrowserMediaSkill(BaseSkill):
    """Пауза и продолжение ролика в браузере."""

    def can_handle(self, context: RequestContext) -> bool:
        text = context.raw_text.lower().strip().replace("ё", "е")
        if text in PAUSE_PHRASES:
            return browser_is_playing()
        if text in RESUME_PHRASES:
            return browser_is_paused()
        return False

    def execute(self, context: RequestContext) -> None:
        text = context.raw_text.lower().strip().replace("ё", "е")
        if text in PAUSE_PHRASES:
            if pause_browser():
                logger.info("[Браузер] Пауза.")
            else:
                context.speak("Не вышло.")
            return
        if resume_browser():
            logger.info("[Браузер] Продолжаю.")
        else:
            context.speak("Не вышло.")
