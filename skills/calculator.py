# skills/calculator.py

import re
import math
import logging
from skills.base import BaseSkill, RequestContext

logger = logging.getLogger(__name__)

RUSSIAN_NUMBERS = {
    "ноль": 0, "нуль": 0, "нуля": 0, "нулю": 0,
    "один": 1, "одна": 1, "одно": 1, "одного": 1, "одной": 1, "раз": 1, "первой": 1, "первую": 1,
    "два": 2, "две": 2, "двух": 2, "второй": 2, "вторую": 2,
    "три": 3, "трех": 3, "трёх": 3, "третьей": 3, "третью": 3,
    "четыре": 4, "четырех": 4, "четырёх": 4, "четвертой": 4, "четвертую": 4,
    "пять": 5, "пяти": 5, "пятой": 5, "пятую": 5,
    "шесть": 6, "шести": 6, "шестой": 6, "шестую": 6,
    "семь": 7, "семи": 7, "седьмой": 7, "седьмую": 7,
    "восемь": 8, "восьми": 8, "восьмой": 8, "восьмую": 8,
    "девять": 9, "девяти": 9, "девятой": 9, "девятую": 9,
    "десять": 10, "десяти": 10, "десятой": 10, "десятую": 10,
    "одиннадцать": 11, "одиннадцати": 11, "одиннадцатой": 11,
    "двенадцать": 12, "двенадцати": 12, "двенадцатой": 12,
    "тринадцать": 13, "тринадцати": 13,
    "четырнадцать": 14, "четырнадцати": 14,
    "пятнадцать": 15, "пятнадцати": 15,
    "шестнадцать": 16, "шестнадцати": 16,
    "семнадцать": 17, "семнадцати": 17,
    "восемнадцать": 18, "восемнадцати": 18,
    "девятнадцать": 19, "девятнадцати": 19,
    "двадцать": 20, "двадцати": 20, "двадцатой": 20,
    "тридцать": 30, "тридцати": 30, "тридцатой": 30,
    "сорок": 40, "сорока": 40,
    "пятьдесят": 50, "пятидесяти": 50,
    "шестьдесят": 60, "шестидесяти": 60,
    "семьдесят": 70, "семидесяти": 70,
    "восемьдесят": 80, "восьмидесяти": 80,
    "девяносто": 90, "девяноста": 90,
    "сто": 100, "ста": 100, "сотни": 100,
    "двести": 200, "двухсот": 200,
    "триста": 300, "трехсот": 300, "трёхсот": 300,
    "четыреста": 400, "четырехсот": 400, "четырёхсот": 400,
    "пятьсот": 500, "пятисот": 500,
    "шестьсот": 600, "шестисот": 600,
    "семьсот": 700, "семисот": 700,
    "восемьсот": 800, "восьмисот": 800,
    "девятьсот": 900, "девятисот": 900,
    "тысяча": 1000, "тысячи": 1000, "тысяч": 1000, "тысячу": 1000,
    "миллион": 1000000, "миллиона": 1000000, "миллионов": 1000000
}


def words_to_number_phrase(text: str) -> str:
    """Заменяет словесные числительные в строке на арабские цифры."""
    # Заменяем точку/запятую в десятичных дробях
    text = re.sub(r"(\d+)[,\.](\d+)", r"\1.\2", text)
    words = text.split()
    new_words = []
    accum = 0
    in_number = False

    for w in words:
        clean_w = w.strip(".,!?")
        if clean_w in RUSSIAN_NUMBERS:
            val = RUSSIAN_NUMBERS[clean_w]
            if val in [1000, 1000000]:
                accum = (accum if accum > 0 else 1) * val
            else:
                accum += val
            in_number = True
        else:
            if in_number:
                new_words.append(str(accum))
                accum = 0
                in_number = False
            new_words.append(w)

    if in_number:
        new_words.append(str(accum))

    return " ".join(new_words)


def format_calc_result(value: float) -> str:
    """Форматирует числовой результат для естественного звучания."""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, int):
        return str(value)
    return f"{value:.2f}".rstrip("0").rstrip(".")


class CalculatorSkill(BaseSkill):
    """Фирменный быстрый калькулятор в стиле Алисы."""

    def can_handle(self, context: RequestContext) -> bool:
        text = context.raw_text.lower().strip()
        calc_triggers = [
            "сколько будет", "посчитай", "вычисли", "сложи",
            "умножь", "раздели", "подели", "вычти", "отними", "прибавь",
            "плюс", "минус", "умножить", "разделить", "поделить",
            "корень из", "квадратный корень", "степени", "степень", "в квадрате", "в кубе",
            "процентов от", "процента от", "процент от", "%"
        ]
        has_math_word = any(trig in text for trig in calc_triggers)
        has_numbers = bool(re.search(r"\d+", text)) or any(w in text for w in RUSSIAN_NUMBERS)
        return has_math_word and has_numbers

    def execute(self, context: RequestContext) -> None:
        text = context.raw_text.lower().strip()
        normalized = words_to_number_phrase(text)

        # 1. Проценты: "20 процентов от 500"
        pct_match = re.search(r"(\d+(?:\.\d+)?)\s*(?:процент(?:а|ов|у)?|%)\s*от\s*(\d+(?:\.\d+)?)", normalized)
        if pct_match:
            pct_val = float(pct_match.group(1))
            total_val = float(pct_match.group(2))
            res = (pct_val / 100.0) * total_val
            context.speak(f"{format_calc_result(pct_val)} процентов от {format_calc_result(total_val)} — это {format_calc_result(res)}.")
            return

        # 2. Квадратный корень: "корень из 144"
        sqrt_match = re.search(r"(?:корень|квадратный корень)\s*из\s*(\d+(?:\.\d+)?)", normalized)
        if sqrt_match:
            num = float(sqrt_match.group(1))
            if num < 0:
                context.speak("Из отрицательных чисел квадратный корень не извлекается.")
                return
            res = math.sqrt(num)
            context.speak(f"Квадратный корень из {format_calc_result(num)} равен {format_calc_result(res)}.")
            return

        # 3. Степени: "5 в квадрате", "2 в кубе", "2 в 10 степени", "2 в степени 8"
        sq_match = re.search(r"(\d+(?:\.\d+)?)\s*в\s*квадрате", normalized)
        if sq_match:
            num = float(sq_match.group(1))
            res = num ** 2
            context.speak(f"{format_calc_result(num)} в квадрате будет {format_calc_result(res)}.")
            return

        cube_match = re.search(r"(\d+(?:\.\d+)?)\s*в\s*кубе", normalized)
        if cube_match:
            num = float(cube_match.group(1))
            res = num ** 3
            context.speak(f"{format_calc_result(num)} в кубе будет {format_calc_result(res)}.")
            return

        pow_match = re.search(r"(\d+(?:\.\d+)?)\s*в\s*(?:(\d+(?:\.\d+)?)\s*(?:-й|-ой)?\s*)?(?:степени|степень)\s*(\d+(?:\.\d+)?)?", normalized)
        if pow_match:
            base_num = float(pow_match.group(1))
            exp_val = pow_match.group(2) or pow_match.group(3)
            if exp_val:
                exp_num = float(exp_val)
                try:
                    res = base_num ** exp_num
                    context.speak(f"{format_calc_result(base_num)} в степени {format_calc_result(exp_num)} равно {format_calc_result(res)}.")
                    return
                except OverflowError:
                    context.speak("Получилось слишком большое число!")
                    return

        # 4. Префиксные команды деления: "раздели 100 на 4", "подели 250 на 5"
        prefix_div = re.search(r"(?:раздели|подели)\s*(\d+(?:\.\d+)?)\s*на\s*(\d+(?:\.\d+)?)", normalized)
        if prefix_div:
            n1 = float(prefix_div.group(1))
            n2 = float(prefix_div.group(2))
            if n2 == 0:
                context.speak("На ноль делить нельзя!")
                return
            res = n1 / n2
            context.speak(f"Получается {format_calc_result(res)}.")
            return

        # 5. Префиксные команды умножения: "умножь 25 на 4"
        prefix_mult = re.search(r"(?:умножь)\s*(\d+(?:\.\d+)?)\s*на\s*(\d+(?:\.\d+)?)", normalized)
        if prefix_mult:
            n1 = float(prefix_mult.group(1))
            n2 = float(prefix_mult.group(2))
            res = n1 * n2
            context.speak(f"Будет {format_calc_result(res)}.")
            return

        # 6. Префиксные сложение/вычитание: "прибавь к 10 5", "отними от 20 7"
        prefix_add = re.search(r"(?:прибавь|сложи)\s*(?:к\s*)?(\d+(?:\.\d+)?)\s*(?:и|\+|,)?\s*(\d+(?:\.\d+)?)", normalized)
        if prefix_add:
            n1 = float(prefix_add.group(1))
            n2 = float(prefix_add.group(2))
            res = n1 + n2
            context.speak(f"Будет {format_calc_result(res)}.")
            return

        prefix_sub = re.search(r"(?:отними|вычти)\s*(?:из|от\s*)?(\d+(?:\.\d+)?)\s*(\d+(?:\.\d+)?)", normalized)
        if prefix_sub:
            n1 = float(prefix_sub.group(1))
            n2 = float(prefix_sub.group(2))
            res = n1 - n2
            context.speak(f"Будет {format_calc_result(res)}.")
            return

        # 7. Инфиксная арифметика (+, -, *, /)
        mult_match = re.search(r"(\d+(?:\.\d+)?)\s*(?:умножить на|умножь на|\*|x)\s*(\d+(?:\.\d+)?)", normalized)
        if mult_match:
            n1 = float(mult_match.group(1))
            n2 = float(mult_match.group(2))
            res = n1 * n2
            context.speak(f"Будет {format_calc_result(res)}.")
            return

        div_match = re.search(r"(\d+(?:\.\d+)?)\s*(?:разделить на|поделить на|раздели на|подели на|/|:)\s*(\d+(?:\.\d+)?)", normalized)
        if div_match:
            n1 = float(div_match.group(1))
            n2 = float(div_match.group(2))
            if n2 == 0:
                context.speak("На ноль делить нельзя!")
                return
            res = n1 / n2
            context.speak(f"Получается {format_calc_result(res)}.")
            return

        plus_match = re.search(r"(\d+(?:\.\d+)?)\s*(?:плюс|прибавить|\+)\s*(\d+(?:\.\d+)?)", normalized)
        if plus_match:
            n1 = float(plus_match.group(1))
            n2 = float(plus_match.group(2))
            res = n1 + n2
            context.speak(f"Будет {format_calc_result(res)}.")
            return

        minus_match = re.search(r"(\d+(?:\.\d+)?)\s*(?:минус|отнять|-)\s*(\d+(?:\.\d+)?)", normalized)
        if minus_match:
            n1 = float(minus_match.group(1))
            n2 = float(minus_match.group(2))
            res = n1 - n2
            context.speak(f"Будет {format_calc_result(res)}.")
            return

        context.speak("Извините, не смог посчитать это выражение.")
