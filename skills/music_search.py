# skills/music_search.py
# Поиск и скачивание трека в ~/Музыка/Jarvis, плеер — Audacious.
# Пауза / следующий / голое «включи музыку» остаются в AudaciousSkill.

import logging
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
        "--restrict-filenames",
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
    for line in reversed((result.stdout or "").splitlines()):
        line = line.strip()
        if line.startswith("/") and Path(line).is_file():
            return Path(line)
    after = [item for item in dest.iterdir() if item.is_file() and item.resolve() not in before]
    audio_new = [item for item in after if item.suffix.lower() in AUDIO_EXTS]
    if audio_new:
        audio_new.sort(key=lambda item: item.stat().st_mtime, reverse=True)
        return audio_new[0]
    return _newest_audio(dest) if result.returncode == 0 else None


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
