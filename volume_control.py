# volume_control.py

import os
import json
import subprocess
import logging
import threading
import time


def _read_ducking_volume() -> int:
    raw = os.getenv("DUCKING_VOLUME", "8")
    try:
        val = int(raw)
        return max(0, min(val, 50))
    except (TypeError, ValueError):
        return 8


class VolumeController:
    """Модуль управления автоматическим приглушением громкости (ducking) или паузой в плеере Audacious с защитой от сбоев."""

    _instance = None
    _instance_lock = threading.Lock()

    def __new__(cls):
        with cls._instance_lock:
            if cls._instance is None:
                inst = super().__new__(cls)
                inst._initialized = False
                cls._instance = inst
            return cls._instance

    def __init__(self):
        if getattr(self, "_initialized", False):
            return
        self._initialized = True
        self.lock = threading.Lock()
        base_dir = os.path.dirname(os.path.abspath(__file__))
        self.cache_path = os.path.join(base_dir, "volume_cache.json")

        # Целевой уровень приглушения музыки (по умолчанию 8% для надежного отсечения микрофоном)
        self.target_percent = _read_ducking_volume()
        # Порог защиты: если звук в плеере ниже этого уровня, считаем его уже приглушенным
        self.safe_limit = max(self.target_percent + 4, 12)

        # Режим приглушения: "duck" (убавить громкость) или "pause" (временно поставить на паузу)
        self.mode = os.getenv("DUCKING_MODE", "duck").lower().strip()
        self._paused_by_ducking = False
        self._playback_cache = False
        self._playback_cache_ts = 0.0

        with self.lock:
            self._original_volume = self._load_cache()

        # Самовосстановление при холодном старте
        if self._original_volume is not None:
            logging.info(f"[Volume] Обнаружен сохраненный уровень громкости {self._original_volume}% после прошлого сеанса. Восстановление...")
            self.restore()
        else:
            current_vol = self.get_current_volume()
            if current_vol is not None and current_vol <= self.safe_limit:
                logging.info(f"[Volume] Обнаружена заниженная громкость плеера ({current_vol}%). Принудительный сброс на безопасные 80%...")
                self._set_volume(80)

    def _load_cache(self) -> int | None:
        """Считывает сохраненную громкость из JSON-файла."""
        if os.path.exists(self.cache_path):
            try:
                with open(self.cache_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    val = data.get("original_volume")
                    if val is not None:
                        return int(val)
            except Exception as e:
                logging.error(f"[Volume] Ошибка чтения кэша громкости с диска: {e}")
        return None

    def _save_cache(self, value: int | None):
        """Записывает сохраненную громкость в JSON-файл."""
        try:
            with open(self.cache_path, "w", encoding="utf-8") as f:
                json.dump({"original_volume": value}, f, ensure_ascii=False)
            logging.debug(f"[Volume] Кэш громкости обновлен на диске: {value}")
        except Exception as e:
            logging.error(f"[Volume] Ошибка записи кэша громкости на диск: {e}")

    def is_playback_active(self) -> bool:
        """Проверяет, играет ли сейчас трек в Audacious. Результат кэшируется на 0.6 с."""
        now = time.time()
        if now - self._playback_cache_ts < 0.6:
            return self._playback_cache

        playing = False
        for cmd in [["audtool", "playback-status"], ["audtool", "--playback-status"]]:
            try:
                res = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=1.0
                )
                if res.returncode == 0 and res.stdout.strip() == "playing":
                    playing = True
                    break
            except Exception:
                pass

        self._playback_cache = playing
        self._playback_cache_ts = now
        return playing

    def is_ducked_or_playing(self) -> bool:
        """Возвращает True, если звук приглушен, плеер на паузе из-за диалога или играет."""
        if self._original_volume is not None or self._paused_by_ducking:
            return True
        return self.is_playback_active()

    def get_current_volume(self) -> int | None:
        """Получает текущую громкость Audacious в процентах с автоподбором синтаксиса."""
        for cmd in [["audtool", "get-volume"], ["audtool", "--get-volume"]]:
            try:
                res = subprocess.run(
                    cmd, 
                    capture_output=True, 
                    text=True, 
                    timeout=1.5
                )
                if res.returncode == 0:
                    val_str = res.stdout.strip()
                    if val_str.isdigit():
                        return int(val_str)
                    else:
                        logging.warning(f"[Volume] Вывод команды '{' '.join(cmd)}' не является числом: '{val_str}'")
                else:
                    logging.debug(
                        f"[Volume] Команда '{' '.join(cmd)}' вернула код {res.returncode}."
                    )
            except Exception as e:
                logging.debug(f"[Volume] Ошибка при выполнении '{' '.join(cmd)}': {e}")
        return None

    def adjust_volume(self, delta: int) -> int | None:
        """Сдвигает громкость плеера. Если сейчас дакинг, двигает и текущий, и сохранённый уровень."""
        with self.lock:
            current_vol = self.get_current_volume()
            if current_vol is None:
                return None

            new_current = max(0, min(current_vol + delta, 100))
            if not self._set_volume(new_current):
                return None

            if self._original_volume is not None:
                self._original_volume = max(0, min(self._original_volume + delta, 100))
                self._save_cache(self._original_volume)
                logging.info(
                    f"[Volume] Громкость плеера {current_vol}% -> {new_current}% "
                    f"(сохранённый уровень {self._original_volume}%)"
                )
            else:
                logging.info(f"[Volume] Громкость плеера {current_vol}% -> {new_current}%")
            return new_current

    def _set_volume(self, percent: int) -> bool:
        """Вспомогательный метод установки громкости с автовыбором синтаксиса."""
        for cmd in [["audtool", "set-volume", str(percent)], ["audtool", "--set-volume", str(percent)]]:
            try:
                res = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=1.5
                )
                if res.returncode == 0:
                    return True
            except Exception as e:
                logging.debug(f"[Volume] Исключение при выполнении '{' '.join(cmd)}': {e}")
        return False

    def duck(self):
        """
        Временно приглушает громкость Audacious или ставит на паузу.
        Сохраняет исходный уровень для последующего восстановления. Метод потокобезопасен.
        """
        with self.lock:
            # Если режим "pause", ставим играющий плеер на паузу
            if self.mode == "pause":
                if self.is_playback_active() and not self._paused_by_ducking:
                    try:
                        subprocess.run(["audtool", "--playback-pause"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=1.0)
                        self._paused_by_ducking = True
                        logging.info("[Volume] Плеер временно приостановлен на время диалога.")
                    except Exception as e:
                        logging.debug(f"[Volume] Ошибка паузы плеера: {e}")
                return

            # Стандартный режим "duck" (убавление громкости)
            if self._original_volume is not None:
                logging.debug("[Volume] Громкость уже приглушена, пропускаем.")
                return

            current_vol = self.get_current_volume()
            if current_vol is None:
                logging.debug("[Volume] Плеер не активен, приглушение пропущено.")
                return

            if current_vol <= self.safe_limit:
                logging.debug(
                    f"[Volume] Текущая громкость ({current_vol}%) <= {self.safe_limit}%. "
                    f"Приглушение пропущено во избежание утери оригинального звука."
                )
                return

            self._original_volume = current_vol
            self._save_cache(current_vol)
            
            if self._set_volume(self.target_percent):
                logging.info(f"[Volume] Музыка приглушена: {self._original_volume}% -> {self.target_percent}%")
            else:
                logging.error("[Volume] Не удалось приглушить музыку.")
                self._original_volume = None
                self._save_cache(None)

    def restore(self):
        """Восстанавливает громкость или возобновляет воспроизведение после паузы."""
        with self.lock:
            # Восстановление паузы
            if self._paused_by_ducking:
                self._paused_by_ducking = False
                try:
                    subprocess.run(["audtool", "--playback-play"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=1.0)
                    logging.info("[Volume] Воспроизведение плеера возобновлено.")
                except Exception as e:
                    logging.debug(f"[Volume] Ошибка возобновления плеера: {e}")
                # В режиме pause громкость не трогали — не поднимаем её до 80%.
                if self.mode == "pause":
                    return

            # Восстановление громкости
            if self._original_volume is None:
                self._original_volume = self._load_cache()

            if self._original_volume is None:
                current_vol = self.get_current_volume()
                if current_vol is not None and current_vol <= self.safe_limit:
                    logging.warning(
                        f"[Volume] Память громкости пуста, но обнаружен тихий уровень {current_vol}%. "
                        f"Сброс на безопасные 80%..."
                    )
                    self._original_volume = 80
                else:
                    return

            if self._set_volume(self._original_volume):
                logging.info(f"[Volume] Громкость музыки восстановлена до {self._original_volume}%")
            else:
                logging.error(f"[Volume] Не удалось восстановить громкость музыки до {self._original_volume}%")
            
            self._original_volume = None
            self._save_cache(None)
