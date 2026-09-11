# music_library.py
# Локальная музыка: ~/Музыка (или ~/Music), скачанное в подпапке Jarvis,
# плейлисты .m3u, выбор папки голосом.

from __future__ import annotations

import logging
import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional

from skills.text_utils import has_any_word as _has_any_word, has_word as _has_word, norm as _norm

logger = logging.getLogger(__name__)

AUDIO_EXTS = {".mp3", ".opus", ".m4a", ".ogg", ".flac", ".wav", ".aac", ".wma"}
PLAYLIST_EXTS = {".m3u", ".m3u8"}
_PLAY_VERBS = ("включи", "запусти", "вруби", "поставь")
_SEARCH_VERBS = ("найди", "найти", "поищи", "ищи", "скачай", "загрузи")
# «песней» — Vosk вместо «песню»; плюс живые падежи.
_SONG_NOUNS = (
    "песню", "песня", "песни", "песней", "песне", "песен",
    "песенку", "песенка", "песену",
)
_TRACK_NOUNS = _SONG_NOUNS + ("трек", "трека", "альбом")
_MUSIC_NOUNS = _TRACK_NOUNS + ("музыку", "музыка", "музыки")
_ROOT_NAMES = ("музыка", "музыки", "music")
_SONG_ALT = "|".join(_SONG_NOUNS + ("трек", "трека", "музыку", "альбом"))


def library_root() -> Path:
    ru = Path.home() / "Музыка"
    en = Path.home() / "Music"
    if ru.is_dir():
        return ru
    if en.is_dir():
        return en
    return ru


def jarvis_dir(*, create: bool = False) -> Path:
    path = library_root() / "Jarvis"
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def dir_has_audio(path: Path) -> bool:
    if not path.is_dir():
        return False
    try:
        for item in path.rglob("*"):
            if item.is_file() and item.suffix.lower() in AUDIO_EXTS:
                return True
    except OSError as exc:
        logger.debug("[Музыка] Не удалось прочитать %s: %s", path, exc)
    return False


def default_play_path() -> Optional[Path]:
    """Сначала Jarvis, если пусто — вся ~/Музыка."""
    downloaded = jarvis_dir(create=False)
    if dir_has_audio(downloaded):
        return downloaded
    root = library_root()
    if dir_has_audio(root):
        return root
    return None


def is_local_music_command(text: str) -> bool:
    """Включить папку / плейлист / всю или дефолтную библиотеку. Не «песню»."""
    lowered = _norm(text)
    if not _has_any_word(lowered, _PLAY_VERBS):
        return False
    if "плейлист" in lowered:
        return True
    if _has_any_word(lowered, ("папка", "папку", "папки")):
        return True
    if "всю музыку" in lowered:
        return True
    if _has_any_word(lowered, ("музыку", "музыка")) and not _has_any_word(
        lowered, _TRACK_NOUNS
    ):
        return True
    return False


def extract_playlist_name(text: str) -> str:
    lowered = _norm(text)
    match = re.search(r"плейлист(?:у|а|ы|ов)?\s+(.+)$", lowered)
    if not match:
        return ""
    return _strip_tail(match.group(1))


def extract_folder_name(text: str) -> str:
    lowered = _norm(text)
    match = re.search(r"(?:из\s+папки|папку|папка)\s+(.+)$", lowered)
    if not match:
        return ""
    return _strip_tail(match.group(1))


def extract_music_query(text: str) -> str:
    cleaned = _norm(text)
    cleaned = re.sub(
        rf"^.*?(?:найди|найти|поищи|ищи|скачай|загрузи|поставь|включи|запусти|вруби)\s+"
        rf"(?:мне\s+)?(?:{_SONG_ALT})\s*",
        "",
        cleaned,
    )
    cleaned = re.sub(r"\s+пожалуйста$", "", cleaned)
    return cleaned.strip(" .")


def is_music_search(text: str) -> bool:
    """Найди/скачай/включи песню. Не папка, не плейлист, не радио, не фильм."""
    lowered = _norm(text)
    if not lowered:
        return False
    if _has_any_word(lowered, ("фильм", "кино", "радио", "сериал")):
        return False
    if "рабочий стол" in lowered:
        return False
    if "плейлист" in lowered or extract_folder_name(text):
        return False
    if not _has_any_word(lowered, _MUSIC_NOUNS):
        return False
    has_search = _has_any_word(lowered, _SEARCH_VERBS)
    has_play_track = _has_any_word(lowered, _PLAY_VERBS) and _has_any_word(lowered, _TRACK_NOUNS)
    if not (has_search or has_play_track):
        return False
    return True


def _strip_tail(name: str) -> str:
    cleaned = name.strip(" .")
    cleaned = re.sub(r"\s+пожалуйста$", "", cleaned)
    return cleaned.strip()


def _iter_subdirs(root: Path) -> list[tuple[str, Path]]:
    found: list[tuple[str, Path]] = []
    if not root.is_dir():
        return found
    try:
        for child in root.iterdir():
            if child.is_dir() and not child.name.startswith("."):
                found.append((child.name, child))
    except OSError as exc:
        logger.debug("[Музыка] Список папок %s: %s", root, exc)
    return found


def _iter_playlists(root: Path) -> list[tuple[str, Path]]:
    found: list[tuple[str, Path]] = []
    if not root.is_dir():
        return found
    try:
        for item in root.rglob("*"):
            if item.is_file() and item.suffix.lower() in PLAYLIST_EXTS:
                found.append((item.stem, item))
    except OSError as exc:
        logger.debug("[Музыка] Список плейлистов %s: %s", root, exc)
    return found


def _best_match(query: str, candidates: list[tuple[str, Path]]) -> Optional[Path]:
    needle = _norm(query)
    if not needle or not candidates:
        return None
    ranked: list[tuple[float, Path]] = []
    for name, path in candidates:
        title = _norm(name)
        if needle == title:
            return path
        if needle in title or title in needle:
            score = 0.9
        else:
            score = SequenceMatcher(None, needle, title).ratio()
        ranked.append((score, path))
    ranked.sort(key=lambda row: row[0], reverse=True)
    if ranked[0][0] < 0.55:
        return None
    return ranked[0][1]


def resolve_playlist(name: str) -> Optional[Path]:
    return _best_match(name, _iter_playlists(library_root()))


def resolve_folder(name: str) -> Optional[Path]:
    cleaned = _norm(name)
    if cleaned in _ROOT_NAMES:
        root = library_root()
        return root if root.is_dir() else None
    candidates = _iter_subdirs(library_root()) + _iter_subdirs(jarvis_dir(create=False))
    return _best_match(name, candidates)


def resolve_play_source(text: str) -> Optional[tuple[str, Optional[Path]]]:
    """
    ('playlist'|'folder'|'all'|'default', path_or_none).
    None — это не команда локальной музыки.
    """
    if not is_local_music_command(text):
        return None
    lowered = _norm(text)
    playlist_name = extract_playlist_name(text)
    if playlist_name:
        return "playlist", resolve_playlist(playlist_name)
    folder_name = extract_folder_name(text)
    if folder_name:
        if _norm(folder_name) in _ROOT_NAMES:
            return "all", library_root() if library_root().is_dir() else None
        return "folder", resolve_folder(folder_name)
    if "всю музыку" in lowered:
        root = library_root()
        return "all", root if root.is_dir() else None
    return "default", default_play_path()


def find_local_track(query: str) -> Optional[Path]:
    needle = _norm(query)
    if not needle:
        return None
    roots = [jarvis_dir(create=False), library_root()]
    ranked: list[tuple[float, Path]] = []
    seen: set[str] = set()
    for root in roots:
        if not root.is_dir():
            continue
        try:
            for item in root.rglob("*"):
                if not item.is_file() or item.suffix.lower() not in AUDIO_EXTS:
                    continue
                key = str(item)
                if key in seen:
                    continue
                seen.add(key)
                title = _norm(item.stem)
                if needle == title:
                    return item
                if needle in title:
                    score = 0.92
                else:
                    score = SequenceMatcher(None, needle, title).ratio()
                ranked.append((score, item))
        except OSError as exc:
            logger.debug("[Музыка] Поиск файла в %s: %s", root, exc)
    if not ranked:
        return None
    ranked.sort(key=lambda row: row[0], reverse=True)
    if ranked[0][0] < 0.58:
        return None
    return ranked[0][1]
