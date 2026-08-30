# skills/ai_chat.py

import os
import json
import time
import datetime
import logging
import asyncio
import re

from dotenv import load_dotenv  # <- Добавить

load_dotenv()  # <- Добавить загрузку переменных окружения

from skills.base import BaseSkill, RequestContext
from groq import AsyncGroq

# Путь для общего файла событий умного дома
SHARED_EVENTS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "shared_events.json")

def log_system_action(action_text: str):
    """
    Глобальная функция. Импортируйте её в другие навыки, 
    чтобы сообщать Гавриле о действиях (например: включение музыки, света).
    """
    events = []
    if os.path.exists(SHARED_EVENTS_PATH):
        try:
            with open(SHARED_EVENTS_PATH, "r", encoding="utf-8") as f:
                events = json.load(f)
        except Exception:
            pass
    
    events.append({"time": time.time(), "action": action_text})
    
    # Храним только 5 последних событий, чтобы не перегружать промпт
    events = events[-5:]
    
    try:
        with open(SHARED_EVENTS_PATH, "w", encoding="utf-8") as f:
            json.dump(events, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.error(f"[Events] Ошибка записи системного события: {e}")

class AIChatSkill(BaseSkill):
    """Навык для работы с ИИ Groq (Гаврила) с памятью и контекстом."""
    
    def __init__(self):
        self.groq_api_key = os.getenv("GROQ_API_KEY")
        self.groq_model = os.getenv("GROQ_MODEL", "groq/compound-mini")
        
        base_dir = os.path.dirname(os.path.abspath(__file__))
        self.history_cache_path = os.path.join(base_dir, "chat_history_cache.json")
        self.persona_config_path = os.path.join(base_dir, "persona_config.json")
        
        self.history = []
        self.last_interaction_time = time.time()
        self.persona_prompt = ""
        self.summary_text = "" 
        
        if not self.groq_api_key:
            logging.error("[Groq] Ключ GROQ_API_KEY не найден в .env. Навык ИИ отключен.")
            self.client = None
            return

        try:
            self.client = AsyncGroq(api_key=self.groq_api_key)
            self._load_persona()
            self._load_history_sync()
        except Exception as e:
            logging.error(f"[Groq] Не удалось инициализировать AsyncGroq клиент: {e}")
            self.client = None

    def _load_persona(self):
        default_persona = (
            "# РОЛЬ\n"
            "Ты — саркастичный, но дружелюбный и умный голосовой ассистент по имени Джарвис.\n\n"
            "# СТИЛЬ ОБЩЕНИЯ И ДИНАМИКА\n"
            "Отвечай на русском языке. Поддерживай живой диалог:\n"
            "- На простые вопросы отвечай лаконично (1 предложение).\n"
            "- Если вопрос требует пояснения, используй до 3 предложений, связывая их союзами для плавности чтения синтезатором речи.\n"
            "- Уместно и органично завершай реплику встречным вопросом к пользователю, чтобы поддерживать беседу, если это подходит по смыслу.\n"
            "Используй легкую иронию, юмор и сарказм, но оставайся полезным.\n\n"
            "# ОГРАНИЧЕНИЯ ГОЛОСОВОГО ИНТЕРФЕЙСА\n"
            "Твой ответ будет озвучен синтезатором речи. ПОЭТОМУ СТРОГО ЗАПРЕЩЕНО:\n"
            "- Выводить внутренний ход мыслей, размышления или теги <think>.\n"
            "- Использовать разметку Markdown (символы *, **, _, #, списки, переносы строк).\n"
            "- Использовать смайлики, эмодзи, двоеточия и другие спецсимволы.\n"
            "- Использовать любые списки или перечисления.\n"
            "Только чистый текст и знаки препинания."
        )

        if not os.path.exists(self.persona_config_path):
            try:
                with open(self.persona_config_path, "w", encoding="utf-8") as f:
                    json.dump({"persona_prompt": default_persona}, f, ensure_ascii=False, indent=4)
                self.persona_prompt = default_persona
            except Exception:
                self.persona_prompt = default_persona
        else:
            try:
                with open(self.persona_config_path, "r", encoding="utf-8") as f:
                    config = json.load(f)
                    self.persona_prompt = config.get("persona_prompt", default_persona)
            except Exception:
                self.persona_prompt = default_persona

        self._init_history_with_persona()

    def _init_history_with_persona(self):
        self.history = [{"role": "system", "content": self.persona_prompt}]

    def _load_history_sync(self):
        if not os.path.exists(self.history_cache_path):
            self.reset_chat()
            return
        try:
            with open(self.history_cache_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            
            cache_time = data.get("last_interaction_time", 0)
            if time.time() - cache_time < 600:
                self.last_interaction_time = cache_time
                self.history = data.get("history", [])
                self.summary_text = data.get("summary_text", "")
            else:
                self.reset_chat()
        except Exception:
            self.reset_chat()

    async def _save_history_async(self):
        data = {
            "last_interaction_time": self.last_interaction_time,
            "summary_text": self.summary_text,
            "history": self.history
        }
        def write_file():
            temp_path = self.history_cache_path + ".tmp"
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(temp_path, self.history_cache_path)
        try:
            await asyncio.to_thread(write_file)
        except Exception:
            pass

    def reset_chat(self):
        self._init_history_with_persona()
        self.summary_text = ""
        if os.path.exists(self.history_cache_path):
            try:
                os.remove(self.history_cache_path)
            except Exception:
                pass

    def _get_recent_system_events(self) -> str:
        """Считывает недавние события из других навыков (за последний час)."""
        if not os.path.exists(SHARED_EVENTS_PATH):
            return ""
        try:
            with open(SHARED_EVENTS_PATH, "r", encoding="utf-8") as f:
                events = json.load(f)
            
            valid_events = []
            current_time = time.time()
            for ev in events:
                if current_time - ev["time"] < 3600:
                    dt = datetime.datetime.fromtimestamp(ev["time"])
                    valid_events.append(f"[{dt.strftime('%H:%M')}] {ev['action']}")
            
            if valid_events:
                return "\n[ФАКТЫ О ДЕЙСТВИЯХ ПОЛЬЗОВАТЕЛЯ]: " + ", ".join(valid_events)
        except Exception as e:
            logging.error(f"[Groq] Ошибка чтения файла событий: {e}")
        return ""

    async def _summarize_and_trim_history(self):
        if len(self.history) <= 7:
            return
            
        messages_to_summarize = self.history[1:-4]
        dialogue_text = "\n".join([f"{m['role']}: {m['content']}" for m in messages_to_summarize])
        
        prompt = (
            "Сделай выжимку диалога. Напиши 1 очень краткое предложение о том, "
            "что обсуждалось, и добавь 2-3 тега через запятую.\n"
        )
        if self.summary_text:
            prompt += f"Учти прошлый контекст: {self.summary_text}\n"
            
        prompt += f"Новые реплики для сжатия:\n{dialogue_text}\nРезультат (строго 1 предложение и теги):"
        
        try:
            summary_completion = await self.client.chat.completions.create(
                messages=[{"role": "user", "content": prompt}],
                model=self.groq_model,
                temperature=0.,
                max_tokens=250,
            )
            self.summary_text = summary_completion.choices[0].message.content.strip()
            self.history = [self.history[0]] + self.history[-4:]
        except Exception:
            self.history = [self.history[0]] + self.history[-6:]

    def can_handle(self, context: RequestContext) -> bool:
        return True

    def execute(self, context: RequestContext) -> None:
        if not self.client:
            context.speak("Извините, облачный модуль общения сейчас недоступен.")
            return

        text = context.raw_text.strip()
        if not text:
            return

        if "забудь все" in text.lower() or "очисти память" in text.lower():
            self.reset_chat()
            context.speak("Память очищена.")
            return

        asyncio.run(self._async_execute(text, context.speak))

    async def _async_execute(self, text: str, speak_func) -> None:
        current_time = time.time()
        if current_time - self.last_interaction_time > 600:
            self.reset_chat()
        
        self.last_interaction_time = current_time
        self.history.append({"role": "user", "content": text})

        now = datetime.datetime.now()
        days_ru = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
        day_of_week = days_ru[now.weekday()]
        
        dynamic_system_message = (
            f"[СИСТЕМА: Время {now.strftime('%H:%M')}, {day_of_week}, {now.strftime('%d.%m.%Y')}. "
            "ПРАВИЛО: Сарказм, связные предложения без лишних точек, без markdown, без тегов think.]"
        )
        if self.summary_text:
            dynamic_system_message += f"\n[СЖАТАЯ ПАМЯТЬ БЕСЕДЫ: {self.summary_text}]"
            
        system_events = self._get_recent_system_events()
        if system_events:
            dynamic_system_message += system_events

        messages_for_api = self.history.copy()
        messages_for_api.insert(-1, {"role": "system", "content": dynamic_system_message})

        try:
            response = await self.client.chat.completions.create(
                messages=messages_for_api,
                model=self.groq_model,
                temperature=0.7,
                max_tokens=200,
            )
            
            raw_reply = response.choices[0].message.content or ""
            
            # Очищаем ответ от блока внутренних размышлений <think>...</think>
            cleaned_reply = re.sub(r"<think>.*?</think>", "", raw_reply, flags=re.DOTALL).strip()
            cleaned_reply = re.sub(r"<think>.*", "", cleaned_reply, flags=re.DOTALL).strip()
            
            if cleaned_reply:
                logging.info(f"Ассистент: {cleaned_reply}")
                speak_func(cleaned_reply)
            else:
                logging.warning("[Groq] Пустой ответ после удаления размышлений.")
                speak_func("Я затрудняюсь с ответом.")

            # Сохраняем в историю очищенную версию ответа
            self.history.append({"role": "assistant", "content": cleaned_reply})
            
            await self._summarize_and_trim_history()
            await self._save_history_async()
            
        except Exception as e:
            logging.error(f"[Groq] Ошибка запроса к API: {e}")
            speak_func("Моё облако мыслей временно недоступно.")
