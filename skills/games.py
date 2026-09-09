# skills/games.py

import random
import re
import logging
from skills.base import BaseSkill, RequestContext
from context_manager import set_active_context, clear_active_context

logger = logging.getLogger(__name__)

NUM_WORDS = {
    "ноль": 0, "нуль": 0, "один": 1, "одна": 1, "одно": 1, "первый": 1,
    "два": 2, "две": 2, "второй": 2, "три": 3, "третий": 3,
    "четыре": 4, "четвертый": 4, "пять": 5, "пятый": 5,
    "шесть": 6, "шестой": 6, "семь": 7, "седьмой": 7,
    "восемь": 8, "восьмой": 8, "девять": 9, "девятый": 9,
    "десять": 10, "десятый": 10, "одиннадцать": 11, "двенадцать": 12,
    "тринадцать": 13, "четырнадцать": 14, "пятнадцать": 15,
    "шестнадцать": 16, "семнадцать": 17, "восемнадцать": 18,
    "девятнадцать": 19, "двадцать": 20, "тридцать": 30,
    "сорок": 40, "пятьдесят": 50, "шестьдесят": 60,
    "семьдесят": 70, "восемьдесят": 80, "девяносто": 90, "сто": 100
}


def _extract_int_from_text(text: str) -> int | None:
    """Извлекает число из цифр или словесного описания."""
    match = re.search(r"\b\d+\b", text)
    if match:
        return int(match.group(0))
    
    words = text.lower().split()
    total = 0
    found = False
    for w in words:
        clean_w = w.strip(".,!?")
        if clean_w in NUM_WORDS:
            total += NUM_WORDS[clean_w]
            found = True
    return total if found else None


def _attempts_str(n: int) -> str:
    abs_n = abs(n)
    last_two = abs_n % 100
    last_one = abs_n % 10
    if 11 <= last_two <= 14:
        return f"{n} попыток"
    if last_one == 1:
        return f"{n} попытку"
    if 2 <= last_one <= 4:
        return f"{n} попытки"
    return f"{n} попыток"


class GuessNumberGame:
    """Сессия игры 'Больше — Меньше'."""

    def __init__(self, secret: int = 0):
        self.secret = secret or random.randint(1, 100)
        self.attempts = 0

    def handle_turn(self, user_text: str, speak_callback) -> bool:
        """
        Обрабатывает ход пользователя.
        Возвращает True, если игра окончена, False если продолжается.
        """
        clean = user_text.lower().strip()
        if any(w in clean for w in ["сдаюсь", "хватит", "стоп", "выход", "закончить"]):
            speak_callback(f"Игра окончена. Я загадал число {self.secret}.")
            return True

        num = _extract_int_from_text(clean)
        if num is None:
            speak_callback("Назовите число от 1 до 100 или скажите 'сдаюсь'.")
            return False

        self.attempts += 1
        if num < self.secret:
            speak_callback("Моё число больше.")
            return False
        elif num > self.secret:
            speak_callback("Моё число меньше.")
            return False
        else:
            att_text = _attempts_str(self.attempts)
            speak_callback(f"В точку! Вы угадали число {self.secret} за {att_text}! Отличная игра.")
            return True


class GamesAndRandomSkill(BaseSkill):
    """Навык интерактивных игр (Больше-Меньше) и генераторов случайных событий (кубики, рандом, выбор)."""

    def can_handle(self, context: RequestContext) -> bool:
        text = context.raw_text.lower().strip()

        # 1. Игра Больше-Меньше
        game_triggers = [
            "больше меньше", "игра больше", "сыграем в больше", "сыграем в игру",
            "угадай число", "загадай число", "поиграем в число", "давай сыграем"
        ]
        if any(t in text for t in game_triggers):
            return True

        # 2. Игральные кости и кубики
        dice_triggers = [
            "брось кубик", "кинь кубик", "бросить кубик", "кинуть кубик",
            "брось кости", "кинь кости", "брось два кубика", "кинь два кубика",
            "брось кость", "кинь кость", "d20", "двадцатигранник", "d6",
            "брось кубики", "кинь кубики"
        ]
        if any(t in text for t in dice_triggers):
            return True

        # 3. Случайное число
        random_triggers = [
            "случайное число", "рандомное число", "назови число от",
            "число от", "сгенерируй число"
        ]
        if any(t in text for t in random_triggers):
            return True

        # 4. Случайный выбор (выбери X или Y)
        if ("выбери" in text or "что выбрать" in text or "что лучше" in text) and "или" in text:
            return True

        return False

    def execute(self, context: RequestContext) -> None:
        text = context.raw_text.lower().strip()

        # 1. Запуск игры «Больше — Меньше»
        if any(w in text for w in ["больше меньше", "угадай число", "загадай число", "сыграем", "поиграем"]):
            game = GuessNumberGame()
            set_active_context(
                name="game_more_less",
                handler=game.handle_turn,
                timeout_sec=60.0,
                on_exit=lambda speak: speak(f"Игра окончена. Было загадано число {game.secret}.")
            )
            context.speak("Я загадал число от 1 до 100. Попробуйте угадать! Называйте число.")
            return

        # 2. Бросок игральных кубиков
        if any(w in text for w in ["кубик", "кости", "d20", "двадцатигранник"]):
            if "d20" in text or "двадцатигранник" in text:
                val = random.randint(1, 20)
                context.speak(f"Бросил двадцатигранник. Выпало {val}.")
                return

            if "два" in text or "2" in text or "пару" in text:
                d1 = random.randint(1, 6)
                d2 = random.randint(1, 6)
                context.speak(f"Бросил два кубика. На первом {d1}, на втором {d2}. В сумме {d1 + d2}.")
                return

            val = random.randint(1, 6)
            context.speak(f"Бросил кубик. Выпало {val}.")
            return

        # 3. Генератор случайных чисел
        if any(w in text for w in ["случайное число", "рандомное число", "число от"]):
            match = re.search(r"от\s+(\d+)\s+до\s+(\d+)", text)
            if match:
                min_v = int(match.group(1))
                max_v = int(match.group(2))
                if min_v > max_v:
                    min_v, max_v = max_v, min_v
                val = random.randint(min_v, max_v)
                context.speak(f"Случайное число от {min_v} до {max_v}: {val}.")
                return

            match_single = re.search(r"до\s+(\d+)", text)
            if match_single:
                max_v = int(match_single.group(1))
                val = random.randint(1, max(1, max_v))
                context.speak(f"Случайное число до {max_v}: {val}.")
                return

            val = random.randint(1, 100)
            context.speak(f"Случайное число: {val}.")
            return

        # 4. Случайный выбор (выбери X или Y)
        if "или" in text:
            # Выделяем варианты
            parts = text
            for remove_prefix in ["выбери", "что выбрать", "что лучше", "посоветуй"]:
                parts = re.sub(rf"^{remove_prefix}\s+", "", parts).strip()
            
            options = [p.strip() for p in parts.split("или") if p.strip()]
            if len(options) >= 2:
                chosen = random.choice(options).rstrip(".,!?")
                templates = [
                    f"Я выбираю {chosen}.",
                    f"Определённо {chosen}.",
                    f"Мой выбор — {chosen}.",
                    f"Думаю, лучше {chosen}."
                ]
                context.speak(random.choice(templates))
                return

        context.speak("Не удалось определить параметры игры. Скажите, например: 'брось кубик' или 'сыграем в больше меньше'.")
