import os
import json
import time
import datetime
import logging
import asyncio
import re
import threading
from typing import Callable, List, Dict, Any

from dotenv import load_dotenv

load_dotenv()

from skills.base import BaseSkill, RequestContext
from groq import AsyncGroq
from triggers import is_garbled_utterance, is_self_echo

SHARED_EVENTS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "shared_events.json")
_EVENTS_LOCK = threading.Lock()
_HISTORY_LOCK = threading.Lock()

# Окно живого диалога: 45 минут тишины или до явной очистки памяти.
HISTORY_TTL_SEC = 2700
# Сколько последних реплик (user+assistant) держать целиком, плюс system.
MAX_LIVE_MESSAGES = 12


def log_system_action(action_text: str) -> None:
    """Глобальная функция записи системных событий (атомарная запись)."""
    with _EVENTS_LOCK:
        events: List[Dict[str, Any]] = []
        if os.path.exists(SHARED_EVENTS_PATH):
            try:
                with open(SHARED_EVENTS_PATH, "r", encoding="utf-8") as f:
                    events = json.load(f)
            except Exception:
                events = []

        events.append({"time": time.time(), "action": action_text})
        events = events[-5:]

        temp_path = SHARED_EVENTS_PATH + ".tmp"
        try:
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(events, f, ensure_ascii=False, indent=2)
            os.replace(temp_path, SHARED_EVENTS_PATH)
        except Exception as e:
            logging.error(f"[Events] Ошибка атомарной записи события: {e}")


class AIChatSkill(BaseSkill):
    """Навык работы с ИИ Groq (Джарвис) с поддержкой памяти и очисткой речи под Piper TTS."""

    def __init__(self):
        self.groq_api_key = os.getenv("GROQ_API_KEY")
        self.groq_model = os.getenv("GROQ_MODEL", "groq/compound-mini")

        base_dir = os.path.dirname(os.path.abspath(__file__))
        self.history_cache_path = os.path.join(base_dir, "chat_history_cache.json")
        self.persona_config_path = os.path.join(base_dir, "persona_config.json")

        self.history: List[Dict[str, str]] = []
        self.last_interaction_time = time.time()
        self.persona_prompt = ""
        self.summary_text = ""
        self.client = None

        self._load_persona()

        if not self.groq_api_key:
            logging.error("[Groq] Ключ GROQ_API_KEY не найден в .env.")
            return

        try:
            self.client = AsyncGroq(api_key=self.groq_api_key)
            self._load_history_sync()
        except Exception as e:
            logging.error(f"[Groq] Ошибка инициализации AsyncGroq: {e}")
            self.client = None

    def _load_persona(self) -> None:
        default_persona = (
            "Ты — Джарвис, голосовой ассистент. Говори о себе только в мужском роде: "
            "понял, рад, сделал, готов, согласен.\n\n"
            "Отвечай как в живом разговоре: 1–3 коротких предложения, простой русский, без канцелярита. "
            "Не используй штампы вроде «чем могу помочь», «я языковая модель», «как искусственный интеллект». "
            "Не остроумничай в каждой реплике. Не заканчивай реплику вопросом к пользователю — дождись, пока он сам скажет.\n\n"
            "Ответ будет озвучен синтезатором речи. Запрещены markdown, списки, эмодзи, смайлики, ссылки, "
            "теги think и любые непроизносимые символы. Только связный текст и обычные знаки препинания.\n\n"
            "Ты не включаешь музыку, свет, таймеры и программы из этого чата. "
            "Если фраза похожа на оговорку или обрывок — коротко переспроси. "
            "Не говори, что уже что-то включил, выключил или запустил, если этого нет в фактах о действиях."
        )

        if not os.path.exists(self.persona_config_path):
            try:
                temp_path = self.persona_config_path + ".tmp"
                with open(temp_path, "w", encoding="utf-8") as f:
                    json.dump({"persona_prompt": default_persona}, f, ensure_ascii=False, indent=4)
                os.replace(temp_path, self.persona_config_path)
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

    def _init_history_with_persona(self) -> None:
        self.history = [{"role": "system", "content": self.persona_prompt}]

    def _load_history_sync(self) -> None:
        if not os.path.exists(self.history_cache_path):
            self.reset_chat()
            return
        try:
            with open(self.history_cache_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            cache_time = data.get("last_interaction_time", 0)
            if time.time() - cache_time < HISTORY_TTL_SEC:
                self.last_interaction_time = cache_time
                loaded = data.get("history", [])
                self.summary_text = data.get("summary_text", "")
                if loaded and loaded[0].get("role") == "system":
                    loaded[0] = {"role": "system", "content": self.persona_prompt}
                    self.history = loaded
                else:
                    self.history = [{"role": "system", "content": self.persona_prompt}] + [
                        m for m in loaded if m.get("role") != "system"
                    ]
            else:
                self.reset_chat()
        except Exception:
            self.reset_chat()

    def _write_history_file(self) -> None:
        data = {
            "last_interaction_time": self.last_interaction_time,
            "summary_text": self.summary_text,
            "history": self.history
        }
        temp_path = self.history_cache_path + ".tmp"
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(temp_path, self.history_cache_path)

    def _save_history_sync(self) -> None:
        try:
            self._write_history_file()
        except Exception as e:
            logging.error(f"[Groq] Ошибка сохранения истории: {e}")

    async def _save_history_async(self) -> None:
        try:
            await asyncio.to_thread(self._write_history_file)
        except Exception as e:
            logging.error(f"[Groq] Ошибка сохранения истории: {e}")

    def record_exchange(self, user_text: str, assistant_text: str) -> None:
        """Пишет в память диалога реплику навыка, чтобы Groq видел, что только что произошло."""
        user_text = (user_text or "").strip()
        assistant_text = (assistant_text or "").strip()
        if not user_text or not assistant_text or not self.history:
            return

        with _HISTORY_LOCK:
            current_time = time.time()
            if current_time - self.last_interaction_time > HISTORY_TTL_SEC:
                self.reset_chat()
            self.last_interaction_time = current_time
            self.history.append({"role": "user", "content": user_text})
            self.history.append({"role": "assistant", "content": assistant_text})
            overflow = len(self.history) - 1 - MAX_LIVE_MESSAGES
            if overflow > 0:
                dropped = self.history[1:1 + overflow]
                self.history = [self.history[0]] + self.history[1 + overflow:]
                dropped_text = " ".join(m.get("content", "") for m in dropped if m.get("content"))
                if dropped_text:
                    snippet = dropped_text[:240]
                    if self.summary_text:
                        self.summary_text = f"{self.summary_text} {snippet}".strip()
                    else:
                        self.summary_text = snippet
            self._save_history_sync()

    def reset_chat(self) -> None:
        self._init_history_with_persona()
        self.summary_text = ""
        if os.path.exists(self.history_cache_path):
            try:
                os.remove(self.history_cache_path)
            except Exception:
                pass

    def _get_recent_system_events(self) -> str:
        with _EVENTS_LOCK:
            if not os.path.exists(SHARED_EVENTS_PATH):
                return ""
            try:
                with open(SHARED_EVENTS_PATH, "r", encoding="utf-8") as f:
                    events = json.load(f)
            except Exception as e:
                logging.error(f"[Groq] Ошибка чтения событий: {e}")
                return ""

        valid_events = []
        current_time = time.time()
        for ev in events:
            if current_time - ev.get("time", 0) < 3600:
                dt = datetime.datetime.fromtimestamp(ev["time"])
                valid_events.append(f"[{dt.strftime('%H:%M')}] {ev['action']}")

        if valid_events:
            return "\n[ФАКТЫ О ДЕЙСТВИЯХ ПОЛЬЗОВАТЕЛЯ]: " + ", ".join(valid_events)
        return ""

    def _clean_tts_text(self, text: str) -> str:
        """Очистка ответа от Markdown, тегов think и спецсимволов для TTS."""
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
        text = re.sub(r"<think>.*", "", text, flags=re.DOTALL)
        text = re.sub(r"[\*\_`\#]", "", text)
        text = re.sub(r"\[.*?\]\(.*?\)", "", text)
        return text.strip()

    def _clip_spoken_reply(self, text: str) -> str:
        """Короткая озвучка без хвостового вопроса — длинный монолог кормит эхо в микрофон."""
        text = (text or "").strip()
        if not text:
            return text
        parts = [p.strip() for p in re.split(r"(?<=[.!?…])\s+", text) if p.strip()]
        if not parts:
            return text
        kept = list(parts)
        while len(kept) > 1 and kept[-1].endswith("?"):
            kept.pop()
        kept = kept[:2]
        clipped = " ".join(kept)
        if len(clipped) > 280:
            acc = []
            total = 0
            for part in kept:
                extra = len(part) + (1 if acc else 0)
                if total + extra > 280 and acc:
                    break
                acc.append(part)
                total += extra
            clipped = " ".join(acc) if acc else clipped[:280].rsplit(" ", 1)[0]
        return clipped.strip()

    async def _summarize_and_trim_history(self) -> None:
        if len(self.history) <= 1 + MAX_LIVE_MESSAGES:
            return

        keep = 10
        messages_to_summarize = self.history[1:-keep]
        if not messages_to_summarize:
            return
        dialogue_text = "\n".join([f"{m['role']}: {m['content']}" for m in messages_to_summarize])

        prompt = (
            "Собери факты из диалога для памяти голосового ассистента. "
            "Кратко, по делу, на русском: тема, имена и предпочтения пользователя, "
            "о чём договорились, важные детали. Без стиля и эмоций, без markdown. "
            "Два-четыре коротких предложения.\n"
        )
        if self.summary_text:
            prompt += f"Уже известные факты: {self.summary_text}\n"
        prompt += f"Новые реплики:\n{dialogue_text}"

        try:
            summary_completion = await self.client.chat.completions.create(
                messages=[{"role": "user", "content": prompt}],
                model=self.groq_model,
                temperature=0.0,
                max_tokens=250,
            )
            self.summary_text = summary_completion.choices[0].message.content.strip()
            self.history = [self.history[0]] + self.history[-keep:]
        except Exception:
            self.history = [self.history[0]] + self.history[-MAX_LIVE_MESSAGES:]

    def can_handle(self, context: RequestContext) -> bool:
        return True

    def execute(self, context: RequestContext) -> None:
        if not self.client:
            context.speak("Извините, облачный модуль общения сейчас недоступен.")
            return

        raw_text = context.raw_text
        text = str(raw_text).strip()

        if not text:
            return

        lowered = text.lower()
        if "забудь все" in lowered or "очисти память" in lowered:
            self.reset_chat()
            context.speak("Память очищена.")
            return

        if is_garbled_utterance(text):
            context.speak("Не расслышал, повторите, пожалуйста.")
            return

        last_assistant = ""
        with _HISTORY_LOCK:
            for message in reversed(self.history):
                if message.get("role") == "assistant" and message.get("content"):
                    last_assistant = message["content"]
                    break
        if last_assistant and is_self_echo(text, last_assistant):
            logging.info(f"[Groq] Похоже на эхо своей речи, пропускаю: '{text}'")
            return

        # Безопасный неблокирующий запуск асинхронной логики
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._async_execute(text, context.speak))
        except RuntimeError:
            asyncio.run(self._async_execute(text, context.speak))

    async def _async_execute(self, text: str, speak_func: Callable[[str], None]) -> None:
        current_time = time.time()
        with _HISTORY_LOCK:
            if current_time - self.last_interaction_time > HISTORY_TTL_SEC:
                self.reset_chat()
            self.last_interaction_time = current_time
            self.history.append({"role": "user", "content": text})
            history_snapshot = list(self.history)
            summary_snapshot = self.summary_text

        now = datetime.datetime.now()
        days_ru = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
        day_of_week = days_ru[now.weekday()]

        dynamic_system_message = (
            f"[Сейчас {now.strftime('%H:%M')}, {day_of_week}, {now.strftime('%d.%m.%Y')}.]"
        )
        if summary_snapshot:
            dynamic_system_message += f"\n[Факты из беседы: {summary_snapshot}]"

        system_events = self._get_recent_system_events()
        if system_events:
            dynamic_system_message += system_events

        messages_for_api = history_snapshot
        messages_for_api.insert(-1, {"role": "system", "content": dynamic_system_message})

        try:
            response = await self.client.chat.completions.create(
                messages=messages_for_api,
                model=self.groq_model,
                temperature=0.7,
                max_tokens=120,
            )

            raw_reply = response.choices[0].message.content or ""
            cleaned_reply = self._clip_spoken_reply(self._clean_tts_text(raw_reply))

            if cleaned_reply:
                speak_func(cleaned_reply)
            else:
                logging.warning("[Groq] Пустой ответ после очистки.")
                speak_func("Я затрудняюсь с ответом.")

            with _HISTORY_LOCK:
                self.history.append({"role": "assistant", "content": cleaned_reply})
            await self._summarize_and_trim_history()
            await self._save_history_async()

        except Exception as e:
            logging.error(f"[Groq] Ошибка запроса к API: {e}")
            speak_func("Моё облако мыслей временно недоступно.")
