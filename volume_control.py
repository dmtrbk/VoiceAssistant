# volume_control.py

import os
import json
import subprocess
import logging
import threading

class VolumeController:
    """Модуль управления автоматическим приглушением громкости (ducking) в плеере Audacious с защитой от сбоев."""
    
    def __init__(self):
        self.lock = threading.Lock()
        base_dir = os.path.dirname(os.path.abspath(__file__))
        self.cache_path = os.path.join(base_dir, "volume_cache.json")
        
        # Целевой уровень приглушения музыки изменен на 20%
        self.target_percent = 20
        # Порог защиты: если звук в плеере ниже этого уровня, мы считаем его УЖЕ приглушенным
        self.safe_limit = 25
        
        with self.lock:
            self._original_volume = self._load_cache()
            
        # Самовосстановление при холодном старте: если обнаружен сохраненный объем громкости,
        # мы автоматически восстанавливаем его
        if self._original_volume is not None:
            logging.info(f"[Volume] Обнаружен сохраненный уровень громкости {self._original_volume}% после прошлого сеанса. Восстановление...")
            self.restore()

        # Если кэша не было, проверяем, не застряла ли громкость на тихом уровне (ниже safe_limit)
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
        """Записывает сохраненную громкость в JSON-файла."""
        try:
            with open(self.cache_path, "w", encoding="utf-8") as f:
                json.dump({"original_volume": value}, f, ensure_ascii=False)
            logging.debug(f"[Volume] Кэш громкости обновлен на диске: {value}")
        except Exception as e:
            logging.error(f"[Volume] Ошибка записи кэша громкости на диск: {e}")

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
                    logging.warning(
                        f"[Volume] Команда '{' '.join(cmd)}' вернула код {res.returncode}. "
                        f"Ошибка: {res.stderr.strip()}"
                    )
            except Exception as e:
                logging.debug(f"[Volume] Ошибка при выполнении '{' '.join(cmd)}': {e}")
        return None

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
                else:
                    logging.warning(f"[Volume] Команда '{' '.join(cmd)}' вернула код {res.returncode}: {res.stderr.strip()}")
            except Exception as e:
                logging.debug(f"[Volume] Исключение при выполнении '{' '.join(cmd)}': {e}")
        return False

    def duck(self):
        """
        Временно приглушает громкость Audacious до заданного уровня target_percent.
        Сохраняет исходный уровень для последующего восстановления. Метод потокобезопасен.
        """
        with self.lock:
            if self._original_volume is not None:
                logging.debug("[Volume] Громкость уже приглушена, пропускаем.")
                return

            current_vol = self.get_current_volume()
            if current_vol is None:
                logging.warning("[Volume] Не удалось получить текущую громкость Audacious. Приглушение отменено.")
                return

            # Защитный барьер: если текущая громкость плеера уже тихая (ниже или равна safe_limit),
            # мы считаем его уже приглушенным и игнорируем вызов, чтобы не перезаписать оригинальный звук!
            if current_vol <= self.safe_limit:
                logging.info(
                    f"[Volume] Текущая громкость ({current_vol}%) является тихой (<= {self.safe_limit}%). "
                    f"Приглушение пропущено во избежание утери оригинального звука."
                )
                return

            self._original_volume = current_vol
            self._save_cache(current_vol)  # Кэшируем исходную громкость на диск перед убавлением
            
            if self._set_volume(self.target_percent):
                logging.info(f"[Volume] Музыка приглушена: {self._original_volume}% -> {self.target_percent}%")
            else:
                logging.error("[Volume] Не удалось приглушить музыку.")
                self._original_volume = None
                self._save_cache(None)

    def restore(self):
        """Восстанавливает громкость до исходного уровня. Метод потокобезопасен и самовосстанавливается."""
        with self.lock:
            # 1. Если ОЗУ-переменная оригинального звука пуста, пробуем поднять резервную копию с диска
            if self._original_volume is None:
                self._original_volume = self._load_cache()

            # 2. Если и на диске пусто, проверяем физическое состояние плеера прямо сейчас
            if self._original_volume is None:
                current_vol = self.get_current_volume()
                if current_vol is not None and current_vol <= self.safe_limit:
                    logging.warning(
                        f"[Volume] Память громкости пуста, но обнаружен застрявший тихий уровень {current_vol}%. "
                        f"Экстренный сброс на безопасные 80%..."
                    )
                    self._original_volume = 80
                else:
                    # Плеер не приглушен и кэша нет - восстанавливать нечего
                    return

            # 3. Выполняем восстановление громкости
            if self._set_volume(self._original_volume):
                logging.info(f"[Volume] Громкость музыки восстановлена до {self._original_volume}%")
            else:
                logging.error(f"[Volume] Не удалось восстановить громкость музыки до {self._original_volume}%")
            
            # 4. Полностью сбрасываем состояние
            self._original_volume = None
            self._save_cache(None)  # Очищаем кэш громкости на диске
