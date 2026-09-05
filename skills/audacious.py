# skills/audacious.py

import os
import re
import logging
import random
import time
import subprocess
from skills.base import BaseSkill, RequestContext
from volume_control import VolumeController
from skills.ai_chat import log_system_action
from player_control import start_player_session, stop_player_session
from triggers import MUSIC_VOLUME_HINTS

RADIO_STATIONS = {
    "рекорд": {
        "name": "Радио Рекорд",
        "url": "http://online.radiorecord.ru:8101/rr_128",
        "keywords": ["рекорд", "record", "танцевальное", "клубное"]
    },
    "европа": {
        "name": "Европа Плюс",
        "url": "http://ep128.hostingradio.ru:8030/ep128",
        "keywords": ["европа", "europa", "европу", "плюс"]
    },
    "дорожное": {
        "name": "Дорожное Радио",
        "url": "http://dorognoe.hostingradio.ru:8000/dorognoe",
        "keywords": ["дорожное", "дорожного", "попутное"]
    },
    "ретро": {
        "name": "Ретро ФМ",
        "url": "http://retroserver.streamr.ru:8043/retro128",
        "keywords": ["ретро", "retro", "ретро фм"]
    },
    "наше": {
        "name": "Наше Радио",
        "url": "http://nashe1.hostingradio.ru/nashe-128.mp3",
        "keywords": ["наше", "нашего", "рок", "наше радио"]
    },
    "панки_хой": {
        "name": "Наше Радио - Панки Хой!",
        "url": "http://nashe1.hostingradio.ru/nashepunks.mp3",
        "keywords": ["панки хой", "панк хой", "панки", "хой"]
    },
    "щас_спою": {
        "name": "Наше Радио - Щас Спою!",
        "url": "http://nashe1.hostingradio.ru/nashesongs.mp3",
        "keywords": ["щас спою", "сейчас спою", "спою"]
    },
    "вести": {
        "name": "Вести ФМ",
        "url": "http://icecast.vgtrk.cdnvideo.ru/vestifm_mp3_128kbps",
        "keywords": ["вести", "вести фм", "новости", "новостное"]
    },
    "маяк": {
        "name": "Радио Маяк",
        "url": "http://icecast.vgtrk.cdnvideo.ru/mayakfm_mp3_128kbps",
        "keywords": ["маяк", "радио маяк"]
    },
    # Фирменные звуки природы и релаксации (в стиле Алисы)
    "дождь": {
        "name": "Шум дождя",
        "url": "http://stream.zeno.fm/f3wvbbqmdg8uv",
        "keywords": ["дождь", "дождя", "шум дождя", "звуки дождя", "ливень"]
    },
    "лес": {
        "name": "Звуки леса",
        "url": "http://stream.zeno.fm/0r0xa792kwzuv",
        "keywords": ["лес", "леса", "звуки леса", "звуки природы", "пение птиц", "природа", "природы"]
    },
    "море": {
        "name": "Шум моря",
        "url": "http://stream.zeno.fm/yn9yr6shvg8uv",
        "keywords": ["море", "моря", "шум моря", "океан", "прибой", "волны"]
    },
    "костер": {
        "name": "Звуки костра",
        "url": "http://stream.zeno.fm/6wz2bkvhdg8uv",
        "keywords": ["костер", "костра", "огонь", "камин", "звуки костра"]
    },
    "белый_шум": {
        "name": "Белый шум",
        "url": "http://stream.zeno.fm/c3g4m3vhdg8uv",
        "keywords": ["белый шум", "шум для сна", "звуки для сна"]
    }
}


def generate_m3u_playlist(playlist_path: str):
    try:
        with open(playlist_path, "w", encoding="utf-8") as f:
            f.write("#EXTM3U\n")
            for key, station in RADIO_STATIONS.items():
                f.write(f"#EXTINF:-1,{station['name']}\n")
                f.write(f"{station['url']}\n")
        logging.info(f"[Audacious] Плейлист успешно обновлен: {playlist_path}")
    except Exception as e:
        logging.error(f"[Audacious] Ошибка генерации плейлиста: {e}")


class AudaciousSkill(BaseSkill):
    def __init__(self):
        super().__init__()
        self.vol_ctrl = VolumeController()

    def _start_playback(self, target_path: str):
        started = start_player_session()
        if started:
            time.sleep(0.8)
        subprocess.Popen(
            ["audacious", "-p", target_path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def _stop_playback(self):
        stop_player_session()

    def can_handle(self, context: RequestContext) -> bool:
        text = context.raw_text.lower().strip()

        volume_keywords = ["громче", "тише", "громкость плюс", "громкость минус", "громкость"]
        has_volume_cmd = any(w in text for w in volume_keywords)
        has_music_mention = any(w in text for w in MUSIC_VOLUME_HINTS)

        if has_volume_cmd and has_music_mention:
            return True

        media_controls = [
            "пауза", "стоп музыка", "играй", "возобнови", "плей",
            "следующий", "вперед", "дальше", "следующий трек",
            "предыдущий", "назад", "прошлый трек",
            "что играет", "какой трек",
            "включи музыку", "запусти музыку", "вруби музыку",
            "выключи музыку", "выруби музыку", "останови музыку", "тишина",
            "звуки природы", "шум дождя", "звуки леса", "шум моря", "звуки костра", "белый шум"
        ]
        has_media_control = any(w in text for w in media_controls)
        has_radio = any(w in text for w in ["радио", "радиостанци", "эфир"])

        if any(w in text for w in ["включи", "запусти", "открой", "вруби", "поставь"]):
            if has_media_control or has_radio:
                return True
            for key, station in RADIO_STATIONS.items():
                if any(kw in text for kw in station["keywords"]):
                    return True

        if has_media_control:
            return True

        return False

    def execute(self, context: RequestContext) -> None:
        text = context.raw_text.lower().strip()

        music_dir = os.path.expanduser("~/Музыка")
        if not os.path.exists(music_dir):
            music_dir = os.path.expanduser("~/Music")

        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        playlist_path = os.path.join(base_dir, "radio_playlist.m3u")

        # 1. Управление громкостью плеера
        if any(w in text for w in ["громче", "громкость плюс"]) and any(w in text for w in MUSIC_VOLUME_HINTS):
            current_vol = self.vol_ctrl.get_current_volume()
            if current_vol is not None:
                new_vol = min(current_vol + 15, 100)
                self.vol_ctrl._set_volume(new_vol)
                log_system_action(f"Пользователь увеличил громкость музыки до {new_vol}%")
            return

        if any(w in text for w in ["тише", "громкость минус"]) and any(w in text for w in MUSIC_VOLUME_HINTS):
            current_vol = self.vol_ctrl.get_current_volume()
            if current_vol is not None:
                new_vol = max(current_vol - 15, 0)
                self.vol_ctrl._set_volume(new_vol)
                log_system_action(f"Пользователь уменьшил громкость музыки до {new_vol}%")
            return

        # 2. Остановка музыки
        if any(w in text for w in ["выключи", "останови", "выруби", "тишина"]):
            self._stop_playback()
            log_system_action("Пользователь остановил воспроизведение аудио")
            return

        # 3. Пауза / возобновление
        if any(w in text for w in ["пауза", "стоп музыка", "играй", "возобнови", "плей"]):
            subprocess.Popen(["audtool", "--playback-playpause"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            log_system_action("Пользователь поставил музыку на паузу или возобновил воспроизведение")
            return

        # 4. Переключение треков
        if any(w in text for w in ["следующий", "вперед", "дальше", "следующий трек"]):
            subprocess.Popen(["audtool", "--playlist-advance"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            log_system_action("Пользователь включил следующий трек")
            return

        if any(w in text for w in ["предыдущий", "назад", "прошлый трек"]):
            subprocess.Popen(["audtool", "--playlist-reverse"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            log_system_action("Пользователь включил предыдущий трек")
            return

        # 5. Определение текущего трека
        if "что играет" in text or "какой трек" in text:
            try:
                song = subprocess.check_output(["audtool", "--current-song"], text=True).strip()
                if song:
                    context.speak(f"Сейчас играет: {song}")
                else:
                    context.speak("Сейчас ничего не играет.")
            except Exception:
                context.speak("Плеер Audacious не запущен.")
            return

        # 6. Включение фоновых звуков и радиостанций
        selected_station = None
        for key, station in RADIO_STATIONS.items():
            if any(kw in text for kw in station["keywords"]):
                selected_station = station
                break

        if selected_station:
            generate_m3u_playlist(playlist_path)
            context.speak(f"Включаю {selected_station['name']}.")
            self._start_playback(selected_station["url"])
            log_system_action(f"Пользователь включил {selected_station['name']}")
            return

        # 7. Общее включение радио
        if any(w in text for w in ["радио", "радиостанци", "эфир"]):
            generate_m3u_playlist(playlist_path)
            context.speak("Включаю радио.")
            self._start_playback(playlist_path)
            log_system_action("Пользователь включил радио")
            return

        # 8. Локальная музыка
        if any(w in text for w in ["включи музыку", "запусти музыку", "вруби музыку", "поставь музыку"]):
            if not os.path.exists(music_dir):
                context.speak("Я не нашёл папку Музыка в вашей домашней директории.")
                return

            context.speak("Включаю вашу музыку.")
            self._start_playback(music_dir)
            log_system_action("Пользователь включил локальную музыку")
            return
