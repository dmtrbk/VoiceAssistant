# skills/site_apps.py
# Приложения, которые ОС ставит из сайта (Chrome / Яндекс.Браузер --app-id).
# Каталог собирается из *.desktop; для голоса — синонимы по app-id.

from __future__ import annotations

import configparser
import logging
import os
import re
import shlex
import subprocess
import time
from dataclasses import dataclass

from browser import chrome_command
from skills.base import BaseSkill, RequestContext

logger = logging.getLogger(__name__)

_APP_ID_RE = re.compile(r"--app-id=([a-z0-9]+)", re.IGNORECASE)
_FILE_ID_RE = re.compile(r"chrome-([a-z0-9]+)-", re.IGNORECASE)
_CLOSE = ("закрой", "закрыть", "выключи", "выруби", "убери", "останови")
_DESKTOP_DIRS = (
    os.path.expanduser("~/.local/share/applications"),
    "/usr/share/applications",
)
_DEVNULL = subprocess.DEVNULL
_CACHE_TTL = 8.0
_cache: list[SiteApp] | None = None
_cache_at = 0.0
_cache_key: tuple | None = None

# Голосовые формы, которых нет в Name= десктопа. Ключ — стабильный app-id Chrome.
_ALIASES: dict[str, tuple[str, ...]] = {
    "agimnkijcaahngcdmfeangaknmldooml": (
        "ютуб", "ютубе", "ютьюб", "youtube",
    ),
    "nlalbmkafgmoifbeooblidblkmlhhpnc": (
        "тик ток", "тиктоку", "тикток", "tiktok",
    ),
    "dadlbnbkdoflkdiglhklefcegfdnpgbj": (
        "сонгстер", "сонгстерр", "songsterr",
    ),
    "bcadigmkecmhhknameopgaidphameinh": (
        "яндекс почта", "яндекс почту",
    ),
}


@dataclass(frozen=True)
class SiteApp:
    app_id: str
    name: str
    keywords: tuple[str, ...]
    argv: tuple[str, ...]
    desktop_id: str
    hidden: bool = False


def _norm(text: str) -> str:
    return str(text or "").lower().replace("ё", "е").strip()


def _keywords_from_name(name: str) -> list[str]:
    cleaned = _norm(name)
    for tail in (" web", " app", " приложение"):
        if cleaned.endswith(tail):
            cleaned = cleaned[: -len(tail)].strip()
    return [cleaned] if len(cleaned) >= 4 else []


def parse_desktop(path: str) -> SiteApp | None:
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read(path, encoding="utf-8")
        entry = parser["Desktop Entry"]
    except (OSError, KeyError, configparser.Error):
        return None
    if entry.get("Type", "Application") != "Application":
        return None
    exec_line = entry.get("Exec", "").strip()
    app_id = ""
    match = _APP_ID_RE.search(exec_line)
    if match:
        app_id = match.group(1).lower()
    if not app_id:
        file_match = _FILE_ID_RE.search(os.path.basename(path))
        if file_match:
            app_id = file_match.group(1).lower()
    if not app_id or not exec_line:
        return None
    try:
        argv = tuple(shlex.split(exec_line, posix=True))
    except ValueError:
        return None
    name = (entry.get("Name") or app_id).strip()
    hidden = entry.getboolean("NoDisplay", fallback=False)
    keywords = list(_keywords_from_name(name))
    keywords.extend(_ALIASES.get(app_id, ()))
    keywords = tuple(dict.fromkeys(k for k in (_norm(k) for k in keywords) if k))
    desktop_id = os.path.splitext(os.path.basename(path))[0]
    return SiteApp(
        app_id=app_id,
        name=name,
        keywords=keywords,
        argv=argv,
        desktop_id=desktop_id,
        hidden=hidden,
    )


def _scan_key(directories: tuple[str, ...]) -> tuple:
    parts: list = []
    for folder in directories:
        try:
            st = os.stat(folder)
            parts.append((folder, st.st_mtime_ns))
        except OSError:
            parts.append((folder, 0))
            continue
        try:
            names = os.listdir(folder)
        except OSError:
            continue
        for name in names:
            if not name.startswith("chrome-") or not name.endswith(".desktop"):
                continue
            path = os.path.join(folder, name)
            try:
                parts.append((path, os.stat(path).st_mtime_ns))
            except OSError:
                parts.append((path, 0))
    return tuple(parts)


def discover_site_apps(directories: list[str] | None = None) -> list[SiteApp]:
    dirs = tuple(directories) if directories is not None else _DESKTOP_DIRS
    now = time.time()
    key = _scan_key(dirs)
    global _cache, _cache_at, _cache_key
    if _cache is not None and _cache_key == key and now - _cache_at < _CACHE_TTL:
        return _cache

    found: dict[str, SiteApp] = {}
    for folder in dirs:
        if not os.path.isdir(folder):
            continue
        try:
            names = os.listdir(folder)
        except OSError:
            continue
        for name in names:
            if not name.startswith("chrome-") or not name.endswith(".desktop"):
                continue
            app = parse_desktop(os.path.join(folder, name))
            if app is None or app.hidden:
                continue
            found[app.app_id] = app
    # Если десктопа нет, всё равно оставим известные YouTube / TikTok.
    for app_id, aliases in _ALIASES.items():
        if app_id in found:
            continue
        if app_id not in {
            "agimnkijcaahngcdmfeangaknmldooml",
            "nlalbmkafgmoifbeooblidblkmlhhpnc",
        }:
            continue
        chrome = tuple(chrome_command() + ["--profile-directory=Default", f"--app-id={app_id}"])
        found[app_id] = SiteApp(
            app_id=app_id,
            name="YouTube" if app_id.startswith("agim") else "TikTok",
            keywords=aliases,
            argv=chrome,
            desktop_id="",
        )
    result = list(found.values())
    _cache = result
    _cache_at = now
    _cache_key = key
    return result


def match_site_app(text: str, apps: list[SiteApp] | None = None) -> SiteApp | None:
    lowered = _norm(text)
    best: SiteApp | None = None
    best_len = -1
    for app in apps if apps is not None else discover_site_apps():
        for keyword in app.keywords:
            if keyword and keyword in lowered and len(keyword) > best_len:
                best = app
                best_len = len(keyword)
    return best


def _is_close(text: str) -> bool:
    return any(verb in text for verb in _CLOSE)


def launch_site_app(app: SiteApp) -> bool:
    if app.desktop_id:
        try:
            subprocess.Popen(
                ["gtk-launch", app.desktop_id],
                stdout=_DEVNULL,
                stderr=_DEVNULL,
            )
            return True
        except OSError:
            logger.debug("[Сайты] gtk-launch не вышел, пробую Exec")
    if app.argv:
        try:
            subprocess.Popen(list(app.argv), stdout=_DEVNULL, stderr=_DEVNULL)
            return True
        except OSError as exc:
            logger.error("[Сайты] Не запустил %s: %s", app.name, exc)
    return False


def close_site_app(app: SiteApp) -> None:
    subprocess.Popen(
        ["pkill", "-f", app.app_id],
        stdout=_DEVNULL,
        stderr=_DEVNULL,
    )


class SiteAppsSkill(BaseSkill):
    """Голос для установленных из браузера приложений-сайтов."""

    def can_handle(self, context: RequestContext) -> bool:
        return match_site_app(context.raw_text) is not None

    def execute(self, context: RequestContext) -> None:
        speak = context.speak
        if speak is None:
            return
        text = _norm(context.raw_text)
        app = match_site_app(text)
        if app is None:
            speak("Не нашёл.")
            return
        if _is_close(text):
            close_site_app(app)
            speak("Закрываю.")
            return
        if launch_site_app(app):
            speak("Открываю.")
            return
        speak("Не открыл.")
