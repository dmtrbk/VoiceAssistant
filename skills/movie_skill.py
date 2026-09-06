# skills/movie_skill.py
# Плеер — MPV (Clapper не тянет потоки ВК). Пока MPV жив, «пауза»/«вперед»
# забираем сами: иначе Audacious перехватит до follow-up.

import json
import logging
import os
import re
import shutil
import socket
import subprocess
import urllib.parse
import urllib.request
from typing import Any, List, Optional

from browser import open_url
from player_control import stop_player_session
from skills.ai_chat import log_system_action
from skills.base import BaseSkill, RequestContext

logger = logging.getLogger(__name__)

_DEVNULL = subprocess.DEVNULL
MPV_SOCKET = "/tmp/mpv_jarvis.sock"

_WORD = r"[а-яёa-z0-9]"
_MOVIE_NOUNS = (
    "фильме", "фильма", "фильму", "фильм",
    "сериале", "сериала", "сериалу", "сериал",
    "кино", "видео", "ролик", "трейлер",
    "мультика", "мультик", "мультфильм",
)
_LAUNCH_VERBS = ("включи", "запусти", "поставь", "открой", "вруби", "найди", "поищи", "ищи", "покажи")
_CLOSE_WORDS = ("закрой", "выключи", "выруби", "останови")
_PAUSE_WORDS = ("пауза", "продолжи", "возобнови", "играй", "плей")
_SEEK_WORDS = ("перемотай", "перемотка")
_CLIP_JUNK = (
    "обзор", "нарезка", "прикол", "реакци", "shorts", "тикток",
    "момент", "смешн", "клип", "нарезк",
)

_mpv_proc: Optional[subprocess.Popen] = None


def _has_word(text: str, word: str) -> bool:
    return re.search(rf"(?<!{_WORD}){re.escape(word)}(?!{_WORD})", text) is not None


def _has_any_word(text: str, words) -> bool:
    return any(_has_word(text, w) for w in words)


def _send_mpv_ipc(command: List[object]) -> bool:
    """Отправляет команду в JSON-IPC сокет нашего MPV."""
    if not os.path.exists(MPV_SOCKET):
        return False
    try:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(1.0)
        client.connect(MPV_SOCKET)
        payload = json.dumps({"command": command}) + "\n"
        client.sendall(payload.encode("utf-8"))
        client.recv(1024)
        client.close()
        return True
    except Exception as exc:
        logger.debug("[MovieSkill MPV IPC] Ошибка: %s", exc)
        return False


def _player_alive() -> bool:
    """Наш MPV ещё играет. Мёртвый сокет в /tmp не считаем сессией."""
    if _mpv_proc is not None:
        return _mpv_proc.poll() is None
    if not os.path.exists(MPV_SOCKET):
        return False
    try:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(0.15)
        client.connect(MPV_SOCKET)
        client.close()
        return True
    except Exception:
        try:
            os.remove(MPV_SOCKET)
        except OSError:
            pass
        return False


def _stop_our_player() -> None:
    """Закрывает только MPV, который запустил навык, без pkill по всем окнам."""
    global _mpv_proc
    _send_mpv_ipc(["quit"])
    proc = _mpv_proc
    _mpv_proc = None
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
            except Exception as exc:
                logger.debug("[MovieSkill] Не удалось завершить MPV: %s", exc)
    if os.path.exists(MPV_SOCKET):
        try:
            os.remove(MPV_SOCKET)
        except OSError:
            pass


def _item_field(item: Any, index: int) -> Any:
    if isinstance(item, list) and len(item) > index:
        return item[index]
    if isinstance(item, dict):
        return item.get(str(index), item.get(index))
    return None


def _duration_seconds(raw: Any) -> int:
    if raw is None:
        return 0
    text = str(raw).strip()
    if not text:
        return 0
    parts = text.split(":")
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return 0
    if len(nums) == 1:
        return nums[0]
    if len(nums) == 2:
        return nums[0] * 60 + nums[1]
    if len(nums) >= 3:
        return nums[0] * 3600 + nums[1] * 60 + nums[2]
    return 0


def _query_kind(text: str) -> str:
    if _has_word(text, "трейлер"):
        return "trailer"
    if _has_any_word(text, ("сериал", "сериала")):
        return "series"
    if _has_any_word(text, ("мультик", "мультика", "мультфильм")):
        return "cartoon"
    return "film"


def score_vk_item(title: str, duration_sec: int, query: str, kind: str) -> int:
    """Оценка карточки ВК: совпадение названия + длина, минус клипы и трейлеры."""
    title_l = (title or "").lower()
    score = 0
    tokens = [t for t in re.findall(rf"{_WORD}+", query.lower()) if len(t) > 1]
    stop = {"фильм", "сериала", "сериал", "кино", "видео", "трейлер", "мультик", "мультфильм"}
    name_tokens = [t for t in tokens if t not in stop]
    if not name_tokens:
        name_tokens = tokens

    matched = sum(1 for token in name_tokens if token in title_l)
    score += matched * 12
    if name_tokens and matched == 0:
        score -= 8

    junk = list(_CLIP_JUNK)
    if kind != "trailer":
        junk.extend(("трейлер", "тизер"))
    if any(word in title_l for word in junk):
        score -= 22

    if kind == "trailer":
        if "трейлер" in title_l or "тизер" in title_l:
            score += 16
        if 0 < duration_sec < 400:
            score += 10
        if duration_sec >= 1800:
            score -= 16
    elif kind == "series":
        if any(word in title_l for word in ("сериал", "сезон", "серии", "серия")):
            score += 8
        if duration_sec >= 2400:
            score += 12
        elif 0 < duration_sec < 600:
            score -= 12
    else:
        if duration_sec >= 3600:
            score += 18
        elif duration_sec >= 2400:
            score += 10
        elif 0 < duration_sec < 600:
            score -= 16
        if "фильм" in title_l or "мульт" in title_l:
            score += 3
    return score


def _find_video_list(obj: Any) -> Optional[list]:
    if isinstance(obj, dict) and isinstance(obj.get("list"), list):
        return obj["list"]
    if isinstance(obj, list):
        for item in obj:
            found = _find_video_list(item)
            if found is not None:
                return found
    return None


def search_vk_video(query: str, kind: str = "film") -> Optional[tuple[str, str]]:
    """Ищет на ВК Видео и возвращает (url, title) лучшей карточки, не первой попавшейся."""
    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
            ),
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        data = urllib.parse.urlencode({"act": "search_video", "al": 1, "q": query}).encode("utf-8")
        req = urllib.request.Request("https://vk.com/al_video.php", data=data, headers=headers)
        with urllib.request.urlopen(req, timeout=6) as resp:
            raw = resp.read()
        payload = None
        for encoding in ("utf-8", "cp1251"):
            try:
                payload = json.loads(raw.decode(encoding))
                break
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
        if payload is None:
            return None

        items = _find_video_list(payload.get("payload")) or []
        ranked: list[tuple[int, str, str]] = []
        for item in items[:20]:
            owner = _item_field(item, 0)
            video_id = _item_field(item, 1)
            title = str(_item_field(item, 3) or "")
            duration_sec = _duration_seconds(_item_field(item, 5))
            if owner is None or video_id is None:
                continue
            url = f"https://vkvideo.ru/video{owner}_{video_id}"
            ranked.append((score_vk_item(title, duration_sec, query, kind), url, title))

        if not ranked:
            return None
        ranked.sort(key=lambda row: row[0], reverse=True)
        best_score, best_url, best_title = ranked[0]
        logger.info("[MovieSkill] Выбран ролик «%s» (оценка %s)", best_title, best_score)
        return best_url, best_title
    except Exception as exc:
        logger.debug("[MovieSkill] Ошибка поиска VK Video: %s", exc)
    return None


def play_video(target: Optional[str] = None) -> bool:
    """Запускает видео в полноэкранном MPV, предварительно закрывая предыдущий наш экземпляр."""
    global _mpv_proc
    if shutil.which("mpv") is None:
        return False

    _stop_our_player()
    cmd = [
        "mpv",
        f"--input-ipc-server={MPV_SOCKET}",
        "--fs",
        "--hwdec=auto",
        "--force-window=immediate",
        "--keep-open=yes",
    ]
    if target:
        cmd.append(target)
    _mpv_proc = subprocess.Popen(cmd, stdout=_DEVNULL, stderr=_DEVNULL)
    return True


def extract_movie_query(text: str) -> str:
    """Очищает запрос от вспомогательных слов и команд."""
    cleaned = text.lower().strip()
    if cleaned in ("открой видеоплеер", "запусти видеоплеер", "открой mpv", "запусти mpv"):
        return ""

    patterns = [
        r"^.*?(?:включи|запусти|поставь|открой|вруби|найди|поищи|покажи)\s+(?:на\s+вк\s+видео|на\s+вк|в\s+вк|вк\s+видео)\s*(?:фильм|сериал|кино|видео|ролик|трейлер|мультик|мультфильм)?",
        r"^.*?(?:включи|запусти|поставь|открой|вруби|найди|поищи|покажи)\s+(?:фильм|сериал|кино|видео|ролик|трейлер|мультик|мультфильм)",
        r"^.*?(?:на\s+вк\s+видео|на\s+вк|вк\s+видео)",
    ]
    for pat in patterns:
        cleaned = re.sub(pat, "", cleaned, flags=re.IGNORECASE).strip()

    suffixes = [
        r"\s+в\s+хорошем\s+качестве.*$",
        r"\s+на\s+весь\s+экран.*$",
        r"\s+онлайн.*$",
        r"\s+бесплатно.*$",
        r"\s+пожалуйста.*$",
    ]
    for sfx in suffixes:
        cleaned = re.sub(sfx, "", cleaned, flags=re.IGNORECASE).strip()
    return cleaned.strip()


def _is_player_control(text: str) -> bool:
    if any(phrase in text for phrase in ("на весь экран", "полный экран")):
        return True
    if _has_any_word(text, _PAUSE_WORDS + _SEEK_WORDS):
        return True
    if _has_any_word(text, ("вперед", "назад")) and (
        _has_any_word(text, _SEEK_WORDS + ("фильм", "кино", "видео", "сериал"))
        or text.strip() in ("вперед", "назад")
    ):
        return True
    if _has_any_word(text, _CLOSE_WORDS) and _has_any_word(
        text, _MOVIE_NOUNS + ("плеер", "mpv", "видеоплеер")
    ):
        return True
    return False


def _is_close_command(text: str) -> bool:
    if _has_any_word(text, _CLOSE_WORDS) and _has_any_word(
        text, _MOVIE_NOUNS + ("плеер", "mpv", "видеоплеер")
    ):
        return True
    if _player_alive() and _has_any_word(text, _CLOSE_WORDS) and not _has_any_word(
        text, ("свет", "музыку", "охрану", "компьютер")
    ):
        return True
    return False


class MovieSkill(BaseSkill):
    """Поиск фильмов/сериалов на ВК Видео и воспроизведение в MPV."""

    def can_handle(self, context: RequestContext) -> bool:
        text = context.raw_text.lower().strip()

        if _player_alive() and (_is_player_control(text) or _is_close_command(text)):
            return True
        if _is_player_control(text) and _has_any_word(text, _MOVIE_NOUNS + ("плеер", "mpv", "видеоплеер")):
            return True

        if any(phrase in text for phrase in ("открой видеоплеер", "запусти видеоплеер", "открой mpv", "запусти mpv")):
            return True

        has_action = any(verb in text for verb in _LAUNCH_VERBS)
        has_object = _has_any_word(text, _MOVIE_NOUNS) or "вк видео" in text or "на вк" in text
        if has_action and has_object:
            if _has_any_word(text, ("музыку", "песню", "трек")) or _has_word(text, "радио"):
                return False
            return True
        return False

    def accepts_followup(self, context: RequestContext) -> bool:
        if not _player_alive():
            return False
        return _is_player_control(context.raw_text.lower().strip())

    def on_context_lost(self) -> None:
        return

    def execute(self, context: RequestContext) -> None:
        text = context.raw_text.lower().strip()

        if _is_close_command(text):
            _stop_our_player()
            context.speak("Закрываю плеер.")
            log_system_action("Пользователь закрыл видеоплеер MPV")
            return

        if _has_any_word(text, _PAUSE_WORDS):
            _send_mpv_ipc(["cycle", "pause"])
            log_system_action("Пользователь переключил паузу в MPV")
            return

        if _has_any_word(text, _SEEK_WORDS) or (
            _player_alive() and text.strip() in ("вперед", "назад")
        ) or (
            _has_any_word(text, ("вперед", "назад")) and _has_any_word(text, _MOVIE_NOUNS + _SEEK_WORDS)
        ):
            seconds = -60 if _has_word(text, "назад") else 60
            _send_mpv_ipc(["seek", seconds, "relative"])
            log_system_action(f"Пользователь перемотал видео на {seconds} секунд")
            return

        if any(phrase in text for phrase in ("на весь экран", "полный экран")):
            _send_mpv_ipc(["cycle", "fullscreen"])
            return

        if any(phrase in text for phrase in ("открой видеоплеер", "запусти видеоплеер", "открой mpv", "запусти mpv")):
            if not _has_any_word(text, _MOVIE_NOUNS):
                stop_player_session()
                if play_video(None):
                    context.speak("Открываю видеоплеер.")
                else:
                    context.speak("Плеер MPV не установлен.")
                return

        is_search_only = _has_any_word(text, ("найди", "поищи", "ищи")) and not any(
            verb in text for verb in ("включи", "запусти", "поставь", "открой", "вруби", "покажи")
        )
        query = extract_movie_query(text)

        if is_search_only:
            if not query:
                context.speak("Какой фильм или видео вы хотите найти?")
                return
            stop_player_session()
            encoded = urllib.parse.quote_plus(query)
            context.speak(f"Ищу на ВК Видео: {query}.")
            open_url(f"https://vkvideo.ru/search?q={encoded}")
            log_system_action(f"Пользователь открыл поиск ВК Видео для: {query}")
            return

        if not query:
            context.speak("Какой фильм или видео вы хотите включить?")
            return

        kind = _query_kind(text)
        search_query = query
        if kind == "series" and "сериал" not in query:
            search_query = f"{query} сериал"
        elif kind == "trailer" and "трейлер" not in query:
            search_query = f"{query} трейлер"
        elif kind == "cartoon" and "мультфильм" not in query:
            search_query = f"{query} мультфильм"
        elif kind == "film" and "фильм" not in query:
            search_query = f"{query} фильм"

        stop_player_session()
        found = search_vk_video(search_query, kind)
        if found:
            vk_url, title = found
            spoken_title = title.split("|")[0].strip() or query
            context.speak(f"Включаю {spoken_title}.")
            if not play_video(vk_url):
                context.speak("Плеер MPV не установлен.")
                return
            log_system_action(f"Пользователь запустил «{title}» в MPV")
            return

        context.speak(f"На ВК не нашёл точное совпадение, включаю {query}.")
        if not play_video(f"ytdl://ytsearch1:{search_query}"):
            context.speak("Плеер MPV не установлен.")
            return
        log_system_action(f"Пользователь запустил видео «{query}» через запасной поиск")
