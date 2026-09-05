# skills/timer.py

import os
import re
import time
import logging
import threading
from typing import List, Dict, Any
from skills.base import BaseSkill, RequestContext

logger = logging.getLogger(__name__)

NUM_WORDS = {
    "ноль": 0, "один": 1, "одна": 1, "одну": 1, "раз": 1,
    "два": 2, "две": 2, "три": 3, "четыре": 4, "пять": 5,
    "шесть": 6, "семь": 7, "восемь": 8, "девять": 9, "десять": 10,
    "одиннадцать": 11, "двенадцать": 12, "тринадцать": 13,
    "четырнадцать": 14, "пятнадцать": 15, "шестнадцать": 16,
    "семнадцать": 17, "восемнадцать": 18, "девятнадцать": 19,
    "двадцать": 20, "тридцать": 30, "сорок": 40, "пятьдесят": 50,
    "шестьдесят": 60, "сто": 100
}


def parse_duration_seconds(text: str) -> tuple[int, str]:
    """
    Парсит длительность из текста на русском языке.
    Возвращает (секунды, красивое_название_для_озвучки).
    """
    total_seconds = 0

    # Обработка спец-слов: полчаса, полминуты, полтора часа
    if "полчаса" in text or "пол часа" in text:
        return 1800, "30 минут"
    if "полминуты" in text or "пол минуты" in text:
        return 30, "30 секунд"
    if "полтора часа" in text or "полторы часа" in text:
        return 5400, "полтора часа"
    if "полторы минуты" in text or "полтора минуты" in text:
        return 90, "полторы минуты"

    hours = 0
    minutes = 0
    seconds = 0

    # Поиск часов
    hour_match = re.search(r"(\d+|[а-яё\s]+?)\s*(?:час(?:а|ов|ам)?)", text)
    if hour_match:
        val_str = hour_match.group(1).strip()
        if val_str.isdigit():
            hours = int(val_str)
        else:
            hours = _words_to_number(val_str)

    # Поиск минут
    min_match = re.search(r"(\d+|[а-яё\s]+?)\s*(?:минут(?:у|ы|ам)?)", text)
    if min_match:
        val_str = min_match.group(1).strip()
        if val_str.isdigit():
            minutes = int(val_str)
        else:
            minutes = _words_to_number(val_str)

    # Поиск секунд
    sec_match = re.search(r"(\d+|[а-яё\s]+?)\s*(?:секунд(?:у|ы|ам)?)", text)
    if sec_match:
        val_str = sec_match.group(1).strip()
        if val_str.isdigit():
            seconds = int(val_str)
        else:
            seconds = _words_to_number(val_str)

    # Если единицы не указаны, но есть число после слова "таймер на ..."
    if hours == 0 and minutes == 0 and seconds == 0:
        plain_match = re.search(r"таймер\s+на\s+(\d+)", text)
        if plain_match:
            # По умолчанию считаем минутами
            minutes = int(plain_match.group(1))

    total_seconds = hours * 3600 + minutes * 60 + seconds

    parts = []
    if hours > 0:
        parts.append(f"{hours} {_plural(hours, 'час', 'часа', 'часов')}")
    if minutes > 0:
        parts.append(f"{minutes} {_plural(minutes, 'минуту', 'минуты', 'минут')}")
    if seconds > 0:
        parts.append(f"{seconds} {_plural(seconds, 'секунду', 'секунды', 'секунд')}")

    display_name = " ".join(parts) if parts else f"{total_seconds} секунд"
    return total_seconds, display_name


def _words_to_number(words_str: str) -> int:
    tokens = words_str.split()
    current = 0
    for t in tokens:
        if t in NUM_WORDS:
            current += NUM_WORDS[t]
    return current


def _plural(n: int, form1: str, form2: str, form5: str) -> str:
    abs_n = abs(n)
    last_two = abs_n % 100
    last_one = abs_n % 10
    if 11 <= last_two <= 14:
        return form5
    if last_one == 1:
        return form1
    if 2 <= last_one <= 4:
        return form2
    return form5


def format_remaining_time(seconds_left: int) -> str:
    if seconds_left <= 0:
        return "меньше секунды"
    mins, secs = divmod(seconds_left, 60)
    hours, mins = divmod(mins, 60)

    parts = []
    if hours > 0:
        parts.append(f"{hours} {_plural(hours, 'час', 'часа', 'часов')}")
    if mins > 0:
        parts.append(f"{mins} {_plural(mins, 'минуту', 'минуты', 'минут')}")
    if secs > 0 or not parts:
        parts.append(f"{secs} {_plural(secs, 'секунду', 'секунды', 'секунд')}")
    return " ".join(parts)


class ActiveTimer:
    def __init__(self, duration_sec: int, label: str, speak_callback):
        self.duration_sec = duration_sec
        self.label = label
        self.start_time = time.time()
        self.end_time = self.start_time + duration_sec
        self.speak_callback = speak_callback
        self.canceled = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        while not self.canceled:
            remaining = self.end_time - time.time()
            if remaining <= 0:
                break
            time.sleep(min(1.0, remaining))

        if not self.canceled:
            logger.info(f"[Таймер] Таймер на {self.label} сработал.")
            if self.speak_callback:
                self.speak_callback(f"Время вышло! Ваш таймер на {self.label} завершён.")

    def cancel(self):
        self.canceled = True

    @property
    def remaining_seconds(self) -> int:
        return max(0, int(self.end_time - time.time()))


class TimerSkill(BaseSkill):
    """Фирменный навык управления таймерами в стиле Алисы."""

    def __init__(self):
        self.active_timers: List[ActiveTimer] = []
        self._lock = threading.Lock()

    def can_handle(self, context: RequestContext) -> bool:
        text = context.raw_text.lower().strip()
        triggers = [
            "таймер", "таймеры", "таймера", "таймеру",
            "засеки", "засечь", "поставь будильник", "будильник"
        ]
        return any(trigger in text for trigger in triggers)

    def execute(self, context: RequestContext) -> None:
        text = context.raw_text.lower().strip()

        # 1. Отмена / сброс таймера
        if any(w in text for w in ["отмени", "сбрось", "выключи", "удали", "стоп", "останови", "закрой"]):
            with self._lock:
                if not self.active_timers:
                    context.speak("У вас нет активных таймеров.")
                    return
                count = len(self.active_timers)
                for t in self.active_timers:
                    t.cancel()
                self.active_timers.clear()
            context.speak("Таймер отменён." if count == 1 else "Все таймеры отменены.")
            return

        # 2. Проверка статуса / сколько осталось
        if any(w in text for w in ["сколько", "статус", "проверь", "осталось", "что с", "какой"]):
            with self._lock:
                self.active_timers = [t for t in self.active_timers if not t.canceled and t.remaining_seconds > 0]
                if not self.active_timers:
                    context.speak("Сейчас нет активных таймеров.")
                    return
                timer = self.active_timers[0]
                rem_str = format_remaining_time(timer.remaining_seconds)
                context.speak(f"До конца таймера на {timer.label} осталось {rem_str}.")
                return

        # 3. Установка нового таймера
        duration_sec, label = parse_duration_seconds(text)
        if duration_sec <= 0:
            context.speak("На какое время поставить таймер?")
            return

        with self._lock:
            # Очищаем устаревшие таймеры
            self.active_timers = [t for t in self.active_timers if not t.canceled and t.remaining_seconds > 0]
            new_timer = ActiveTimer(duration_sec, label, context.speak)
            self.active_timers.append(new_timer)

        context.speak(f"Поставил таймер на {label}.")
