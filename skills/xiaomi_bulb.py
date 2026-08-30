# skills/xiaomi_bulb.py

import os
import re
import logging
import random
from skills.base import BaseSkill, RequestContext
from skills.ai_chat import log_system_action 

class XiaomiBulbSkill(BaseSkill):
    def __init__(self):
        self.bulb_ip = os.getenv("XIAOMI_BULB_IP", "").strip()
        self.bulb_token = os.getenv("XIAOMI_BULB_TOKEN", "").strip()
        
        self.yeelight_bulb = None
        self.miio_bulb = None

    @staticmethod
    def _parse_russian_number(text: str) -> int | None:
        words = text.lower().split()
        
        units = {
            "один": 1, "одна": 1, "два": 2, "две": 2, "три": 3, "четыре": 4, "пять": 5,
            "шесть": 6, "семь": 7, "восемь": 8, "девять": 9, "десять": 10,
            "одиннадцать": 11, "двенадцать": 12, "тринадцать": 13, "четырнадцать": 14,
            "пятнадцать": 15, "шестнадцать": 16, "семнадцать": 17, "восемнадцать": 18,
            "девятнадцать": 19
        }
        
        tens = {
            "двадцать": 20, "тридцать": 30, "сорок": 40, "пятьдесят": 50,
            "шестьдесят": 60, "семьдесят": 70, "восемьдесят": 80, "девяносто": 90,
            "сто": 100
        }
        
        val = 0
        found = False
        
        for word in words:
            word_clean = word.strip(",.?!")
            if word_clean in tens:
                val += tens[word_clean]
                found = True
            elif word_clean in units:
                val += units[word_clean]
                found = True
                
        if found and 1 <= val <= 100:
            return val
        return None

    def _init_bulb(self) -> bool:
        if not self.bulb_ip:
            return False

        if self.bulb_token:
            try:
                from miio import Yeelight
                if not self.miio_bulb:
                    self.miio_bulb = Yeelight(ip=self.bulb_ip, token=self.bulb_token)
                return True
            except ImportError:
                logging.error("[XiaomiBulb] Библиотека python-miio не установлена.")
                return False
            except Exception as e:
                logging.error(f"[XiaomiBulb] Ошибка инициализации Miio: {e}")
                return False
        else:
            try:
                from yeelight import Bulb
                if not self.yeelight_bulb:
                    self.yeelight_bulb = Bulb(self.bulb_ip)
                return True
            except ImportError:
                logging.error("[XiaomiBulb] Библиотека yeelight не установлена.")
                return False
            except Exception as e:
                logging.error(f"[XiaomiBulb] Ошибка инициализации Yeelight: {e}")
                return False

    def _turn_on(self):
        if self.bulb_token and self.miio_bulb:
            self.miio_bulb.on()
        elif self.yeelight_bulb:
            self.yeelight_bulb.turn_on()

    def _turn_off(self):
        if self.bulb_token and self.miio_bulb:
            self.miio_bulb.off()
        elif self.yeelight_bulb:
            self.yeelight_bulb.turn_off()

    def _set_brightness(self, value: int):
        if self.bulb_token and self.miio_bulb:
            self.miio_bulb.set_brightness(value)
        elif self.yeelight_bulb:
            self.yeelight_bulb.set_brightness(value)

    def _get_brightness(self) -> int:
        try:
            if self.bulb_token and self.miio_bulb:
                status = self.miio_bulb.status()
                return status.brightness
            elif self.yeelight_bulb:
                props = self.yeelight_bulb.get_properties()
                return int(props.get("bright", 50))
        except Exception as e:
            logging.error(f"[XiaomiBulb] Не удалось получить яркость: {e}")
        return 50

    def can_handle(self, context: RequestContext) -> bool:
        text = context.raw_text.lower().strip()
        
        keywords = ["свет", "лампа", "лампочк", "освещен"]
        has_keyword = any(kw in text for kw in keywords)
        
        actions = [
            "включи", "выключи", "ярче", "тусклее", "яркость", 
            "прибавь", "убавь", "потуши", "зажги", "выруби", "погаси"
        ]
        has_action = any(act in text for act in actions)
        
        return has_keyword and has_action

    def execute(self, context: RequestContext) -> None:
        text = context.raw_text.lower().strip()
        
        if not self.bulb_ip:
            context.speak(
                "Чтобы я могла управлять лампочкой, пожалуйста, пропишите её айпи адрес "
                "в файле .env в переменной XIAOMI_BULB_IP."
            )
            return

        try:
            if self.bulb_token:
                import miio
            else:
                import yeelight
        except ImportError:
            context.speak(
                "Для работы с лампой мне нужны дополнительные библиотеки. "
                "Установите их в терминале командой: пип инсталл yeelight python-miio"
            )
            return

        if not self._init_bulb():
            context.speak("Не удалось подключиться к лампочке. Проверьте правильность настроек в файле .env.")
            return

        try:
            if any(w in text for w in ["выключи", "потуши", "выруби", "погаси"]):
                self._turn_off()
                log_system_action("Пользователь выключил умный свет")
                context.speak(random.choice([
                    "Выключил свет. Теперь можно и отдохнуть в темноте.",
                    "Потушила свет. Как скажете.",
                    "Выключила лампу. Надеюсь, вы не споткнётесь в темноте.",
                    "Свет выключен."
                ]))
                return

            if any(w in text for w in ["включи", "зажги", "вруби", "гори"]):
                self._turn_on()
                log_system_action("Пользователь включил умный свет")
                context.speak(random.choice([
                    "Включила свет. Да будет свет!",
                    "Освещение включено. Так гораздо лучше.",
                    "Зажгла лампу. Теперь всё видно.",
                    "Свет горит."
                ]))
                return

            val = None
            brightness_match = re.search(r'(?:яркость|яркости)\s*(?:на)?\s*(\d+)', text)
            if brightness_match:
                val = int(brightness_match.group(1))
            else:
                val = self._parse_russian_number(text)

            if val is not None:
                if 1 <= val <= 100:
                    self._set_brightness(val)
                    log_system_action(f"Пользователь установил яркость света на {val} процентов")
                    context.speak(f"Установила яркость лампочки на {val} процентов.")
                else:
                    context.speak("Яркость можно установить только в диапазоне от одного до ста процентов.")
                return

            if any(w in text for w in ["ярче", "прибавь", "светлее"]):
                current_bright = self._get_brightness()
                new_bright = min(current_bright + 25, 100)
                self._set_brightness(new_bright)
                log_system_action(f"Пользователь сделал свет ярче, теперь яркость {new_bright} процентов")
                context.speak(f"Сделала светлее. Сейчас яркость {new_bright} процентов.")
                return

            if any(w in text for w in ["тусклее", "убавь", "темнее", "потише"]):
                current_bright = self._get_brightness()
                new_bright = max(current_bright - 25, 1)
                self._set_brightness(new_bright)
                log_system_action(f"Пользователь сделал свет тусклее, теперь яркость {new_bright} процентов")
                context.speak(f"Сделала свет тусклее. Установила яркость на {new_bright} процентов.")
                return

            context.speak(
                "Я поняла, что вы хотите настроить лампочку, но не поняла точную команду. "
                "Попробуйте сказать 'выключи свет' или 'яркость пятьдесят'."
            )

        except Exception as e:
            logging.error(f"[XiaomiBulb] Сбой управления лампочкой: {e}")
            context.speak(
                "Не могу связаться с лампочкой. "
                "Проверьте, включена ли она в розетку и подключена ли к вашему вайфаю."
            )
