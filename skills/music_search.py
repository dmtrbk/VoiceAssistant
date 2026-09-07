# skills/music_search.py
# Поиск и скачивание трека в ~/Музыка/Jarvis, плеер — Audacious.
# Пауза / следующий / голое «включи музыку» остаются в AudaciousSkill.

import logging
import re
import shutil
import subprocess
import time
from pathlib import Path

from music_library import (
    AUDIO_EXTS,
    extract_music_query,
    find_local_track,
    is_music_search,
    jarvis_dir,
)
from player_control import start_player_session
from skills.ai_chat import log_system_action
from skills.base import BaseSkill, RequestContext
from skills.movie_skill import stop_movie_player

logger = logging.getLogger(__name__)

_MAX_SONG_SEC = 15 * 60


def _newest_audio(folder: Path) -> Path | None:
    files = [
        item
        for item in folder.iterdir()
        if item.is_file() and item.suffix.lower() in AUDIO_EXTS
    ]
    if not files:
        return None
    files.sort(key=lambda item: item.stat().st_mtime, reverse=True)
    return files[0]


_BLANK_STEM = re.compile(r"^[-_.\s]*$")


def _safe_filename_stem(query: str) -> str:
    stem = re.sub(r'[\\/:*?"<>|]+', " ", query or "").strip()
    stem = re.sub(r"\s+", " ", stem)
    return stem[:120] or "track"


def _rename_if_blank(path: Path, query: str) -> Path:
    if not _BLANK_STEM.match(path.stem):
        return path
    dest = path.with_name(f"{_safe_filename_stem(query)}{path.suffix.lower()}")
    if dest.resolve() == path.resolve():
        return path
    if dest.exists():
        n = 2
        while True:
            alt = path.with_name(f"{_safe_filename_stem(query)} {n}{path.suffix.lower()}")
            if not alt.exists():
                dest = alt
                break
            n += 1
    try:
        path.rename(dest)
        return dest
    except OSError as exc:
        logger.debug("[MusicSearch] Не удалось переименовать %s: %s", path, exc)
        return path


def download_track(query: str) -> Path | None:
    if not shutil.which("yt-dlp"):
        return None
    dest = jarvis_dir(create=True)
    before = {item.resolve() for item in dest.iterdir() if item.is_file()}
    tmpl = str(dest / "%(title)s.%(ext)s")
    cmd = [
        "yt-dlp",
        "-x",
        "--audio-format", "m4a",
        "--audio-quality", "0",
        "--no-playlist",
        "--no-overwrites",
        "--windows-filenames",
        "--trim-filenames", "180",
        "--match-filter", f"duration < {_MAX_SONG_SEC}",
        "-o", tmpl,
        "--print", "after_move:filepath",
        f"ytsearch1:{query}",
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except Exception as exc:
        logger.debug("[MusicSearch] yt-dlp: %s", exc)
        return None
    found = None
    for line in reversed((result.stdout or "").splitlines()):
        line = line.strip()
        if line.startswith("/") and Path(line).is_file():
            found = Path(line)
            break
    if found is None:
        after = [item for item in dest.iterdir() if item.is_file() and item.resolve() not in before]
        audio_new = [item for item in after if item.suffix.lower() in AUDIO_EXTS]
        if audio_new:
            audio_new.sort(key=lambda item: item.stat().st_mtime, reverse=True)
            found = audio_new[0]
        elif result.returncode == 0:
            found = _newest_audio(dest)
    if found is None:
        return None
    return _rename_if_blank(found, query)


def play_file(path: Path) -> None:
    stop_movie_player()
    start_player_session()
    time.sleep(0.4)
    subprocess.Popen(
        ["audacious", "-p", str(path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


class MusicSearchSkill(BaseSkill):
    """Ищет песню локально, иначе скачивает в Jarvis и включает в Audacious."""

    def can_handle(self, context: RequestContext) -> bool:
        return is_music_search(context.raw_text)

    def execute(self, context: RequestContext) -> None:
        query = extract_music_query(context.raw_text)
        if not query:
            context.speak("Какую песню найти?")
            return

        local = find_local_track(query)
        if local:
            context.speak(f"Включаю {local.stem}.")
            play_file(local)
            log_system_action(f"Пользователь включил локальный трек {local.name}")
            return

        context.speak(f"Ищу {query}.")
        found = download_track(query)
        if not found:
            context.speak("Не удалось скачать трек.")
            return
        context.speak(f"Включаю {found.stem}.")
        play_file(found)
        log_system_action(f"Пользователь скачал трек {found.name}")
