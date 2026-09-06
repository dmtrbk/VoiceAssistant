# skills/movie_skill.py

import json
import logging
import os
import re
import shutil
import socket
import subprocess
import urllib.parse
import urllib.request
from typing import List, Optional

from browser import open_url
from player_control import stop_player_session
from skills.ai_chat import log_system_action
from skills.base import BaseSkill, RequestContext

logger = logging.getLogger(__name__)

_DEVNULL = subprocess.DEVNULL
MPV_SOCKET = "/tmp/mpv_jarvis.sock"


def _send_mpv_ipc(command: List[object]) -> bool:
    """Отправляет команду в JSON-IPC сокет плеера MPV."""
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


def _clapper_mpris_cmd(method: str) -> bool:
    """Отправляет команду D-Bus MPRIS плееру Clapper."""
    targets = [
        "com.github.rafostar.Clapper",
        "org.mpris.MediaPlayer2.com.github.rafostar.Clapper",
        "org.mpris.MediaPlayer2.clapper",
        "org.mpris.MediaPlayer2.Clapper",
    ]
    for dest in targets:
        try:
            res = subprocess.run(
                [
                    "dbus-send",
                    "--type=method_call",
                    f"--dest={dest}",
                    "/org/mpris/MediaPlayer2",
                    f"org.mpris.MediaPlayer2.Player.{method}",
                ],
                stdout=_DEVNULL,
                stderr=_DEVNULL,
                timeout=1,
            )
            if res.returncode == 0:
                return True
        except Exception:
            pass
    return False


def _stop_video_players() -> None:
    """Закрывает и останавливает видеоплееры (MPV и Clapper)."""
    _send_mpv_ipc(["quit"])
    _clapper_mpris_cmd("Stop")
    try:
        subprocess.Popen(["pkill", "-x", "mpv"], stdout=_DEVNULL, stderr=_DEVNULL)
        subprocess.Popen(["pkill", "-x", "clapper"], stdout=_DEVNULL, stderr=_DEVNULL)
    except Exception as exc:
        logger.debug("[MovieSkill] Ошибка закрытия плееров: %s", exc)


def search_vk_video(query: str) -> Optional[str]:
    """Ищет видео на VK Video через веб-эндпоинт и возвращает прямую страницу ролика."""
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
        with urllib.request.urlopen(req, timeout=5) as resp:
            content = resp.read().decode("cp1251", errors="ignore")
            match = re.search(r"video(-?\d+_\d+)", content)
            if match:
                video_id = match.group(1)
                return f"https://vkvideo.ru/video{video_id}"
    except Exception as exc:
        logger.debug("[MovieSkill] Ошибка поиска VK Video: %s", exc)
    return None


def play_video(target: Optional[str] = None) -> None:
    """Запускает видео в полноэкранном MPV (или Clapper, если MPV не установлен)."""
    # Удаляем старый сокет перед новым запуском
    if os.path.exists(MPV_SOCKET):
        try:
            os.remove(MPV_SOCKET)
        except OSError:
            pass

    has_mpv = shutil.which("mpv") is not None

    if has_mpv:
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
        subprocess.Popen(cmd, stdout=_DEVNULL, stderr=_DEVNULL)
    else:
        cmd = ["clapper"]
        if target:
            cmd.append(target)
        subprocess.Popen(cmd, stdout=_DEVNULL, stderr=_DEVNULL)


def extract_movie_query(text: str) -> str:
    """Очищает запрос от вспомогательных слов и команд."""
    cleaned = text.lower().strip()

    if cleaned in ["клаппер", "clapper", "мпв", "mpv", "открой клаппер", "запусти клаппер", "включи клаппер"]:
        return ""

    patterns = [
        r"^.*?(?:включи|запусти|поставь|открой|вруби|найди|поищи|ищи|покажи)\s+(?:на\s+вк\s+видео|на\s+вк|в\s+вк|вк\s+видео)\s*(?:фильм|сериал|кино|видео|ролик|трейлер|мультик|мультфильм)?",
        r"^.*?(?:включи|запусти|поставь|открой|вруби|найди|поищи|ищи|покажи)\s+(?:фильм|сериал|кино|видео|ролик|трейлер|мультик|мультфильм)",
        r"^.*?(?:на\s+вк\s+видео|на\s+вк|в\s+вк|вк\s+видео)",
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


class MovieSkill(BaseSkill):
    """Навык для поиска и полноэкранного воспроизведения фильмов, сериалов и ВК Видео."""

    def can_handle(self, context: RequestContext) -> bool:
        text = context.raw_text.lower().strip()

        # 1. Управление видеоплеером
        control_triggers = [
            "пауза в фильме", "пауза фильм", "пауза видео", "стоп фильм", "стоп видео",
            "продолжи фильм", "возобнови фильм", "продолжи видео", "играй фильм",
            "закрой фильм", "выключи фильм", "выруби фильм", "закрой кино", "выключи кино",
            "закрой видео", "выключи видео", "закрой плеер", "закрой clapper", "выключи плеер",
            "закрой клаппер", "выключи клаппер", "перемотай вперед", "перемотай назад",
            "на весь экран", "полный экран"
        ]
        if any(w in text for w in control_triggers):
            return True

        # 2. Простое открытие/запуск плеера
        if any(w in text for w in ["открой клаппер", "запусти клаппер", "включи клаппер", "открой плеер", "запусти плеер"]):
            if not any(m in text for m in ["музык", "трек", "песн", "радио", "audacious"]):
                return True

        # 3. Поиск / Запуск видео и фильмов
        movie_actions = ["включи", "запусти", "поставь", "открой", "вруби", "найди", "поищи", "ищи", "покажи"]
        movie_objects = [
            "фильм", "сериал", "кино", "видео", "ролик", "трейлер",
            "мультик", "мультфильм", "вк видео", "на вк", "клаппер", "clapper"
        ]

        has_action = any(act in text for act in movie_actions)
        has_object = any(obj in text for obj in movie_objects)

        if has_action and has_object:
            # Исключаем команды музыкального плеера
            if any(w in text for w in ["музыку", "песню", "трек", "радио"]):
                return False
            return True

        return False

    def execute(self, context: RequestContext) -> None:
        text = context.raw_text.lower().strip()

        # 1. Остановка / Закрытие плеера
        if any(w in text for w in ["закрой", "выключи", "выруби", "стоп"]):
            _stop_video_players()
            context.speak("Закрываю плеер.")
            log_system_action("Пользователь остановил и закрыл видеоплеер")
            return

        # 2. Пауза / Возобновление
        if any(w in text for w in ["пауза", "продолжи", "возобнови", "играй"]):
            handled_mpv = _send_mpv_ipc(["cycle", "pause"])
            if not handled_mpv:
                _clapper_mpris_cmd("PlayPause")
            log_system_action("Пользователь переключил паузу в видеоплеере")
            return

        # 3. Перемотка
        if "перемотай" in text or "перемотка" in text:
            seconds = 60
            if "назад" in text:
                seconds = -60
            _send_mpv_ipc(["seek", seconds, "relative"])
            log_system_action(f"Пользователь выполнил перемотку видео на {seconds} секунд")
            return

        # 4. Полноэкранный режим
        if any(w in text for w in ["на весь экран", "полный экран"]):
            _send_mpv_ipc(["cycle", "fullscreen"])
            return

        # 5. Полное отключение музыки перед любым запуском видео/поиска
        stop_player_session()

        # 6. Простое открытие плеера без видео
        if any(w in text for w in ["открой клаппер", "запусти клаппер", "включи клаппер", "открой плеер", "запусти плеер"]):
            if not any(w in text for w in ["фильм", "сериал", "кино", "видео", "ролик", "трейлер", "мультик"]):
                play_video(None)
                context.speak("Открываю видеоплеер.")
                log_system_action("Пользователь открыл видеоплеер")
                return

        # 7. Поиск в браузере (если пользователь явно просил «найди...»)
        is_search_only = any(w in text for w in ["найди", "поищи", "ищи"]) and not any(
            w in text for w in ["включи", "запусти", "поставь", "открой", "вруби", "покажи"]
        )

        query = extract_movie_query(text)

        if is_search_only:
            if not query:
                context.speak("Какой фильм или видео вы хотите найти?")
                return

            encoded = urllib.parse.quote_plus(query)
            search_url = f"https://vkvideo.ru/search?q={encoded}"
            context.speak(f"Ищу на ВК Видео: {query}.")
            open_url(search_url)
            log_system_action(f"Пользователь открыл поиск ВК Видео для: {query}")
            return

        # 8. Запуск видео в полноэкранном плеере
        if not query:
            context.speak("Какой фильм или видео вы хотите включить?")
            return

        # Обогащаем поисковый запрос категорией, если нужно
        search_query = query
        if "сериал" in text and "сериал" not in query:
            search_query = f"{query} сериал"
        elif "трейлер" in text and "трейлер" not in query:
            search_query = f"{query} трейлер"
        elif any(w in text for w in ["мультик", "мультфильм"]) and "мультфильм" not in query:
            search_query = f"{query} мультфильм"
        elif any(w in text for w in ["фильм", "кино"]) and "фильм" not in query:
            search_query = f"{query} фильм"

        context.speak(f"Ищу и включаю {query}.")

        # 8.1 Ищем на ВК Видео
        vk_url = search_vk_video(search_query)

        # 8.2 Если нашли на ВК — запускаем напрямую в MPV
        if vk_url:
            play_video(vk_url)
            log_system_action(f"Пользователь запустил видео '{query}' на ВК Видео")
            return

        # 8.3 Резервный поиск через ytsearch в MPV
        fallback_target = f"ytdl://ytsearch1:{search_query}"
        play_video(fallback_target)
        log_system_action(f"Пользователь запустил видео '{query}' через поиск")
