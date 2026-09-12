# skills/timer.py

import json
import os
import re
import time
import logging
import threading
from skills.base import BaseSkill, RequestContext
from skills.text_utils import plural as _plural, words_to_number

logger = logging.getLogger(__name__)

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TIMERS_FILE = os.path.join(PROJECT_DIR, "timers_cache.json")


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
            hours = words_to_number(val_str)

    # Поиск минут
    min_match = re.search(r"(\d+|[а-яё\s]+?)\s*(?:минут(?:у|ы|ам)?)", text)
    if min_match:
        val_str = min_match.group(1).strip()
        if val_str.isdigit():
            minutes = int(val_str)
        else:
            minutes = words_to_number(val_str)

    # Поиск секунд
    sec_match = re.search(r"(\d+|[а-яё\s]+?)\s*(?:секунд(?:у|ы|ам)?)", text)
    if sec_match:
        val_str = sec_match.group(1).strip()
        if val_str.isdigit():
            seconds = int(val_str)
        else:
            seconds = words_to_number(val_str)

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


def _parse_bare_minutes(text: str) -> tuple[int, str]:
    """«пять» / «10» после вопроса «на какое время?» считаем минутами."""
    match = re.search(r"\d+", text)
    if match:
        minutes = int(match.group(0))
        if minutes > 0:
            return minutes * 60, f"{minutes} {_plural(minutes, 'минуту', 'минуты', 'минут')}"
    words_val = words_to_number(text)
    if words_val > 0:
        return words_val * 60, f"{words_val} {_plural(words_val, 'минуту', 'минуты', 'минут')}"
    return 0, ""


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
    def __init__(
        self,
        duration_sec: int,
        label: str,
        speak_callback,
        end_time: float | None = None,
        on_finished=None,
    ):
        self.duration_sec = duration_sec
        self.label = label
        self.start_time = time.time()
        self.end_time = end_time if end_time is not None else (self.start_time + duration_sec)
        self.speak_callback = speak_callback
        self.on_finished = on_finished
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
                try:
                    self.speak_callback(f"Время вышло! Ваш таймер на {self.label} завершён.")
                except Exception as exc:
                    logger.debug("[Таймер] Ошибка в speak_callback: %s", exc)
        if self.on_finished:
            try:
                self.on_finished(self)
            except Exception:
                pass

    def cancel(self):
        self.canceled = True

    @property
    def remaining_seconds(self) -> int:
        return max(0, int(self.end_time - time.time()))


class TimerSkill(BaseSkill):
    """Фирменный навык управления таймерами в стиле Алисы."""

    def __init__(self):
        self.active_timers: list[ActiveTimer] = []
        self._lock = threading.Lock()
        self._awaiting_duration = False
        self._restored = False

    def can_handle(self, context: RequestContext) -> bool:
        text = context.raw_text.lower().strip()
        triggers = [
            "таймер", "таймеры", "таймера", "таймеру",
            "засеки", "засечь", "поставь будильник", "будильник"
        ]
        return any(trigger in text for trigger in triggers)

    def accepts_followup(self, context: RequestContext) -> bool:
        return self._awaiting_duration

    def on_context_lost(self) -> None:
        self._awaiting_duration = False

    def _on_timer_finished(self, timer: ActiveTimer) -> None:
        with self._lock:
            self._prune_timers()
            self._save_timers_locked()

    def _save_timers_locked(self) -> None:
        data = []
        for t in self.active_timers:
            if not t.canceled and t.remaining_seconds > 0:
                data.append({
                    "end_time": t.end_time,
                    "duration_sec": t.duration_sec,
                    "label": t.label,
                })
        try:
            temp_file = TIMERS_FILE + ".tmp"
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(temp_file, TIMERS_FILE)
        except Exception as e:
            logger.debug("[Таймер] Не удалось сохранить %s: %s", TIMERS_FILE, e)

    def _ensure_restored_locked(self, default_speak) -> None:
        if self._restored:
            return
        self._restored = True
        if not os.path.exists(TIMERS_FILE):
            return
        try:
            with open(TIMERS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, list):
                return
            now = time.time()
            for item in data:
                if not isinstance(item, dict):
                    continue
                end_time = float(item.get("end_time", 0))
                label = str(item.get("label", "таймер"))
                duration_sec = int(item.get("duration_sec", 60))
                if end_time > now:
                    new_timer = ActiveTimer(
                        duration_sec=duration_sec,
                        label=label,
                        speak_callback=default_speak,
                        end_time=end_time,
                        on_finished=self._on_timer_finished,
                    )
                    self.active_timers.append(new_timer)
                    logger.info("[Таймер] Восстановлен активный таймер на %s (осталось %d с).", label, int(end_time - now))
        except Exception as e:
            logger.debug("[Таймер] Не удалось восстановить таймеры: %s", e)

    def _prune_timers(self) -> None:
        self.active_timers = [
            timer for timer in self.active_timers
            if not timer.canceled and timer.remaining_seconds > 0
        ]

    def execute(self, context: RequestContext) -> None:
        text = context.raw_text.lower().strip()
        alert = context.alert_speak or context.speak

        with self._lock:
            self._ensure_restored_locked(alert)
            self._prune_timers()
            self._save_timers_locked()

        # 1. Отмена / сброс таймера
        if any(w in text for w in ["отмени", "сбрось", "выключи", "удали", "стоп", "останови", "закрой"]):
            self._awaiting_duration = False
            with self._lock:
                if not self.active_timers:
                    context.speak("У вас нет активных таймеров.")
                    return
                count = len(self.active_timers)
                for t in self.active_timers:
                    t.cancel()
                self.active_timers.clear()
                self._save_timers_locked()
            context.speak("Таймер отменён." if count == 1 else "Все таймеры отменены.")
            return

        # 2. Проверка статуса / сколько осталось
        if any(w in text for w in ["сколько", "статус", "проверь", "осталось", "что с", "какой"]):
            with self._lock:
                self._prune_timers()
                self._save_timers_locked()
                if not self.active_timers:
                    context.speak("Сейчас нет активных таймеров.")
                    return
                timer = self.active_timers[0]
                rem_str = format_remaining_time(timer.remaining_seconds)
                context.speak(f"До конца таймера на {timer.label} осталось {rem_str}.")
                return

        # 3. Установка нового таймера
        duration_sec, label = parse_duration_seconds(text)
        if duration_sec <= 0 and self._awaiting_duration:
            duration_sec, label = _parse_bare_minutes(text)
        if duration_sec <= 0:
            self._awaiting_duration = True
            context.speak("На какое время поставить таймер?")
            return

        self._awaiting_duration = False

        with self._lock:
            self._prune_timers()
            new_timer = ActiveTimer(
                duration_sec=duration_sec,
                label=label,
                speak_callback=alert,
                on_finished=self._on_timer_finished,
            )
            self.active_timers.append(new_timer)
            self._save_timers_locked()

        context.speak(f"Поставил таймер на {label}.")
