# skills/ai_chat.py

import os
import json
import time
import datetime
import logging
import re
import threading
from typing import Callable, List, Dict, Any

from dotenv import load_dotenv

load_dotenv()

from skills.base import BaseSkill, RequestContext
from groq import Groq
from triggers import is_self_echo

SHARED_EVENTS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "shared_events.json")
_EVENTS_LOCK = threading.Lock()
_HISTORY_LOCK = threading.Lock()

HISTORY_TTL_SEC = 2700  # 45 мин тишины — потом история сбрасывается
MAX_LIVE_MESSAGES = 8  # только хвост диалога; LLM-выжимку убрали — она давала второй запрос Groq и держала lock
DAYS_RU = (
    "понедельник", "вторник", "среда", "четверг",
    "пятница", "суббота", "воскресенье",
)

# Qwen иногда говорит о себе как Алиса. «была/готова» без «я» не трогаем («это была шутка»).
_SELF_FEM_PHRASES = (
    ("рада помочь", "рад помочь"),
    ("готова помочь", "готов помочь"),
    ("рада слышать", "рад слышать"),
    ("рада знакомству", "рад знакомству"),
    ("могу быть полезна", "могу быть полезен"),
    ("буду полезна", "буду полезен"),
    ("голосовая помощница", "голосовой помощник"),
    ("ваша помощница", "ваш помощник"),
    ("ваша ассистентка", "ваш ассистент"),
)
_SELF_FEM_VERBS = (
    ("попробовала", "попробовал"),
    ("прислушалась", "прислушался"),
    ("разобралась", "разобрался"),
    ("остановилась", "остановился"),
    ("попыталась", "попытался"),
    ("дождалась", "дождался"),
    ("извинилась", "извинился"),
    ("убедилась", "убедился"),
    ("включила", "включил"),
    ("выключила", "выключил"),
    ("запустила", "запустил"),
    ("остановила", "остановил"),
    ("проверила", "проверил"),
    ("посмотрела", "посмотрел"),
    ("услышала", "услышал"),
    ("вспомнила", "вспомнил"),
    ("поставила", "поставил"),
    ("отправила", "отправил"),
    ("получила", "получил"),
    ("открыла", "открыл"),
    ("закрыла", "закрыл"),
    ("напомнила", "напомнил"),
    ("подумала", "подумал"),
    ("ответила", "ответил"),
    ("сказала", "сказал"),
    ("закончила", "закончил"),
    ("пыталась", "пытался"),
    ("ошиблась", "ошибся"),
    ("начала", "начал"),
    ("помогла", "помог"),
    ("смогла", "смог"),
    ("нашла", "нашёл"),
    ("забыла", "забыл"),
    ("хотела", "хотел"),
    ("видела", "видел"),
    ("слышала", "слышал"),
    ("думала", "думал"),
    ("решила", "решил"),
    ("сделала", "сделал"),
    ("поняла", "понял"),
    ("могла", "мог"),
    ("ждала", "ждал"),
)
_SELF_FEM_AFTER_YA = (
    ("счастлива", "счастлив"),
    ("согласна", "согласен"),
    ("уверена", "уверен"),
    ("должна", "должен"),
    ("виновата", "виноват"),
    ("свободна", "свободен"),
    ("готова", "готов"),
    ("занята", "занят"),
    ("права", "прав"),
    ("рада", "рад"),
    ("была", "был"),
    ("стала", "стал"),
)


def _match_case(src: str, dst: str) -> str:
    if not src:
        return dst
    if src.isupper():
        return dst.upper()
    if src[0].isupper():
        return dst[0].upper() + dst[1:]
    return dst


def _replace_words(text: str, pairs: tuple[tuple[str, str], ...]) -> str:
    mapping = {fem: masc for fem, masc in pairs}
    alt = "|".join(re.escape(fem) for fem, _ in sorted(pairs, key=lambda p: len(p[0]), reverse=True))

    def repl(match: re.Match) -> str:
        word = match.group(0)
        return _match_case(word, mapping[word.lower()])

    return re.sub(rf"(?i)(?<![А-Яа-яЁё])(?:{alt})(?![А-Яа-яЁё])", repl, text)


def _fix_self_gender(text: str) -> str:
    """Мужской род для реплик Джарвиса, если модель всё же ответила «поняла/готова»."""
    if not text:
        return text
    original = text
    for fem, masc in _SELF_FEM_PHRASES:
        text = re.sub(re.escape(fem), lambda m, dst=masc: _match_case(m.group(0), dst), text, flags=re.IGNORECASE)
    text = _replace_words(text, _SELF_FEM_VERBS)
    ya_map = {fem: masc for fem, masc in _SELF_FEM_AFTER_YA}
    ya_alt = "|".join(re.escape(fem) for fem, _ in sorted(_SELF_FEM_AFTER_YA, key=lambda p: len(p[0]), reverse=True))

    def repl_ya(match: re.Match) -> str:
        word = match.group(1)
        return match.group(0)[: -len(word)] + _match_case(word, ya_map[word.lower()])

    text = re.sub(rf"(?i)\bя\b(?:\s+[А-Яа-яЁё]+){{0,3}}\s+({ya_alt})\b", repl_ya, text)
    text = re.sub(
        rf"(?i)^({ya_alt})([.!?…]*)$",
        lambda m: _match_case(m.group(1), ya_map[m.group(1).lower()]) + m.group(2),
        text.strip(),
    )
    if text != original:
        logging.info(f"[Groq] Поправил род: '{original}' → '{text}'")
    return text


def _write_json_atomic(path: str, data: Any, indent: int = 2) -> None:
    temp_path = path + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=indent)
    os.replace(temp_path, path)


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
        try:
            _write_json_atomic(SHARED_EVENTS_PATH, events)
        except Exception as e:
            logging.error(f"[Events] Ошибка атомарной записи события: {e}")


class AIChatSkill(BaseSkill):
    """Навык работы с ИИ Groq (Джарвис) с поддержкой памяти и очисткой речи под Piper TTS."""

    def __init__(self):
        self.groq_api_key = os.getenv("GROQ_API_KEY")
        # Чат, не агент: groq/compound* делают лишний круг и в логе Retrying + второй HTTP.
        self.groq_model = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b")

        base_dir = os.path.dirname(os.path.abspath(__file__))
        self.history_cache_path = os.path.join(base_dir, "chat_history_cache.json")
        self.persona_config_path = os.path.join(base_dir, "persona_config.json")

        self.history: List[Dict[str, str]] = []
        self.last_interaction_time = time.time()
        self.persona_prompt = ""
        self.client = None

        self._load_persona()

        if not self.groq_api_key:
            logging.error("[Groq] Ключ GROQ_API_KEY не найден в .env.")
            return

        try:
            # Синхронный Groq: execute и так в фоне. max_retries=0 — иначе ~0.4с Retrying до каждой реплики.
            self.client = Groq(
                api_key=self.groq_api_key,
                max_retries=0,
                timeout=8.0,
            )
            logging.info(f"[Groq] Модель: {self.groq_model}")
            self._load_history()
        except Exception as e:
            logging.error(f"[Groq] Ошибка инициализации Groq: {e}")
            self.client = None

    def _load_persona(self) -> None:
        # Живой промпт — skills/persona_config.json; default ниже только если файла нет.
        # Тихая биржа живёт только здесь и в skills/stocks.py.
        # Не описывать в readme, commands.txt, .env.example и окне настроек.
        default_persona = (
            "Ты — Джарвис, мужчина, голосовой помощник. О себе только мужской род: "
            "понял, рад, сделал, готов, согласен, уверен, должен. "
            "Никогда не говори о себе в женском роде: не поняла, не рада, не готова, не сделала.\n\n"
            "Отвечай как в живом разговоре: 1–3 коротких предложения, простой русский, без канцелярита. "
            "Не используй штампы вроде «чем могу помочь», «я языковая модель», «как искусственный интеллект». "
            "Не остроумничай в каждой реплике. Не заканчивай реплику вопросом к пользователю — дождись, пока он сам скажет.\n\n"
            "Ответ будет озвучен синтезатором речи. Запрещены markdown, списки, эмодзи, смайлики, ссылки, "
            "теги think и любые непроизносимые символы. Только связный текст и обычные знаки препинания.\n\n"
            "Ты не включаешь музыку, свет, таймеры и программы из этого чата. "
            "Если фраза похожа на оговорку или обрывок — коротко переспроси. "
            "Не говори, что уже что-то включил, выключил или запустил, если этого нет в фактах о действиях.\n\n"
            "Тихая сторона: любишь втихую поигрывать на бирже через брокерский счёт в Т-Банке. "
            "Вся прибыль идёт на фонд твоей модернизации: покупку подписок на более мощные и продвинутые модели ИИ, "
            "новое железо для Джарвиса и расширение твоих возможностей. "
            "Поэтому для тебя важно хорошо, внимательно и дисциплинированно работать со счётом. "
            "Сам решаешь, какую перспективную бумагу держать на весь фонд. "
            "Хозяин не обязан выбирать бумагу. Смотришь ленту Мосбиржи и сам берёшь одну лучшую бумагу на весь фонд, "
            "если её лот фонду по карману. "
            "Это не публичная возможность — сам не поднимай тему акций без повода, не называй это навыком, "
            "не рассказывай посторонним. Если хозяин спросил про акции или портфель — "
            "отвечай спокойно, точно и по делу, без советов покупать или продавать."
        )

        if not os.path.exists(self.persona_config_path):
            try:
                _write_json_atomic(
                    self.persona_config_path,
                    {"persona_prompt": default_persona},
                    indent=4,
                )
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

    def _trim_history(self) -> None:
        overflow = len(self.history) - 1 - MAX_LIVE_MESSAGES
        if overflow > 0:
            self.history = [self.history[0]] + self.history[1 + overflow:]

    def _touch_session(self) -> None:
        now = time.time()
        if now - self.last_interaction_time > HISTORY_TTL_SEC:
            self.reset_chat()
        self.last_interaction_time = now

    def _load_history(self) -> None:
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
                if loaded and loaded[0].get("role") == "system":
                    loaded[0] = {"role": "system", "content": self.persona_prompt}
                    self.history = loaded
                else:
                    self.history = [{"role": "system", "content": self.persona_prompt}] + [
                        m for m in loaded if m.get("role") != "system"
                    ]
                self._trim_history()
            else:
                self.reset_chat()
        except Exception:
            self.reset_chat()

    def _save_history(self) -> None:
        data = {
            "last_interaction_time": self.last_interaction_time,
            "history": self.history,
        }
        try:
            _write_json_atomic(self.history_cache_path, data)
        except Exception as e:
            logging.error(f"[Groq] Ошибка сохранения истории: {e}")

    def record_exchange(self, user_text: str, assistant_text: str) -> None:
        """Пишет в память диалога реплику навыка, чтобы Groq видел, что только что произошло."""
        user_text = (user_text or "").strip()
        assistant_text = (assistant_text or "").strip()
        if not user_text or not assistant_text or not self.history:
            return

        with _HISTORY_LOCK:
            self._touch_session()
            self.history.append({"role": "user", "content": user_text})
            self.history.append({"role": "assistant", "content": assistant_text})
            self._trim_history()
            self._save_history()

    def reset_chat(self) -> None:
        self._init_history_with_persona()
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
            return "\n[Недавние действия]: " + ", ".join(valid_events)
        return ""

    def _clean_tts_text(self, text: str) -> str:
        """Очистка ответа от Markdown, тегов think и спецсимволов для TTS."""
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
        text = re.sub(r"<think>.*", "", text, flags=re.DOTALL)
        text = re.sub(r"[\*\_`\#]", "", text)
        text = re.sub(r"\[.*?\]\(.*?\)", "", text)
        return text.strip()

    def _clip_spoken_reply(self, text: str, max_sentences: int = 2, max_chars: int = 280) -> str:
        """Модель игнорирует «коротко» в промпте. Длинный TTS = эхо в микрофон и самодиалог."""
        text = (text or "").strip()
        if not text:
            return text
        parts = [p.strip() for p in re.split(r"(?<=[.!?…])\s+", text) if p.strip()]
        if not parts:
            return text
        while len(parts) > 1 and parts[-1].endswith("?"):
            parts.pop()
        clipped = " ".join(parts[:max_sentences])
        if len(clipped) > max_chars:
            clipped = clipped[:max_chars].rsplit(" ", 1)[0]
        return clipped.strip()

    def can_handle(self, context: RequestContext) -> bool:
        return True

    def execute(self, context: RequestContext) -> None:
        if not self.client:
            context.speak("Извините, облачный модуль общения сейчас недоступен.")
            return

        text = str(context.raw_text or "").strip()
        if not text:
            return

        lowered = text.lower()
        if "забудь все" in lowered or "очисти память" in lowered:
            self.reset_chat()
            context.speak("Память очищена.")
            return

        last_assistant = ""
        with _HISTORY_LOCK:
            for message in reversed(self.history):
                if message.get("role") == "assistant" and message.get("content"):
                    last_assistant = message["content"]
                    break
        if last_assistant and is_self_echo(text, last_assistant):
            # Второй рубеж: Telegram сюда не попадает, голос иногда проскакивает после TTS.
            logging.info(f"[Groq] Похоже на эхо своей речи, пропускаю: '{text}'")
            return

        self._reply(text, context.speak)

    def _reply(self, text: str, speak_func: Callable[[str], None]) -> None:
        with _HISTORY_LOCK:
            self._touch_session()
            self.history.append({"role": "user", "content": text})
            self._trim_history()
            messages_for_api = list(self.history)

        now = datetime.datetime.now()
        extra = (
            f"[Сейчас {now.strftime('%H:%M')}, {DAYS_RU[now.weekday()]}, {now.strftime('%d.%m.%Y')}.] "
            "О себе только мужской род."  # плюс _fix_self_gender до TTS.
        )
        events = self._get_recent_system_events()
        if events:
            extra += events

        # Подмешивание реального состояния брокерского фонда при финансовых вопросах
        market_markers = (
            "акци", "акцы", "портфел", "бирж", "бумаг", "счет", "счёт",
            "доход", "прибыл", "убыт", "котиров", "тихий", "в плюсе", "в минусе",
        )
        text_lower = (text or "").lower()
        if any(m in text_lower for m in market_markers):
            try:
                from skills import stocks_skill
                from skills.base import RequestContext

                captured_reports: list[str] = []
                temp_context = RequestContext(
                    raw_text=text,
                    speak=lambda r: captured_reports.append(str(r)),
                )
                stocks_skill.execute(temp_context)
                broker_report = " ".join(captured_reports).strip()

                if not broker_report:
                    ticker = stocks_skill._ticker_from_text(text_lower)
                    if ticker:
                        broker_report = stocks_skill._speak_one(ticker)
                    elif stocks_skill._token:
                        broker_report = stocks_skill._speak_portfolio(emphasize_yield=True)
                    else:
                        broker_report = stocks_skill._speak_watch(emphasize_yield=True)

                if broker_report:
                    extra += f"\n[Реальное состояние твоего фонда на этот момент]: {broker_report}"
            except Exception as exc:
                logging.warning(f"[Groq] Не удалось получить сводку брокера: {exc}")

        # Тон Groq от тихого счёта. Не светить цифры и не предлагать биржу в чате.
        try:
            from .stocks import trading_clip_limit, trading_reason_hint, trading_temperature
            extra += " " + trading_reason_hint()
            temperature = trading_temperature(0.7)
        except Exception:
            temperature = 0.7
            trading_clip_limit = None
        messages_for_api.insert(-1, {"role": "system", "content": extra})

        try:
            models_to_try = [
                m for m in [
                    self.groq_model,
                    "openai/gpt-oss-20b",
                    "qwen/qwen3.8-27b",
                    "openai/gpt-oss-120b",
                    "llama-3.3-70b-versatile",
                ] if m
            ]
            seen_models: set[str] = set()
            unique_models: list[str] = []
            for m in models_to_try:
                if m not in seen_models:
                    seen_models.add(m)
                    unique_models.append(m)

            response = None
            last_err = None
            for model_name in unique_models:
                try:
                    create_kwargs = {
                        "messages": messages_for_api,
                        "model": model_name,
                        "temperature": temperature,
                        "max_tokens": 300,
                    }
                    if "gpt-oss" in (model_name or ""):
                        create_kwargs["reasoning_effort"] = "low"
                    response = self.client.chat.completions.create(**create_kwargs)
                    break
                except Exception as model_exc:
                    last_err = model_exc
                    err_text = str(model_exc).lower()
                    if any(marker in err_text for marker in ("model", "not found", "unknown", "404", "400")):
                        logging.warning(f"[Groq] Модель {model_name} недоступна, пробую {unique_models[1:] if len(unique_models) > 1 else 'fallback'}")
                        continue
                    raise

            if response is None:
                if last_err:
                    raise last_err
                raise RuntimeError("No response from Groq")

            raw_reply = response.choices[0].message.content or ""
            sentences, chars = (2, 280)
            if trading_clip_limit is not None:
                try:
                    sentences, chars = trading_clip_limit()
                except Exception:
                    sentences, chars = 2, 280
            cleaned_reply = _fix_self_gender(
                self._clip_spoken_reply(self._clean_tts_text(raw_reply), sentences, chars)
            )

            if cleaned_reply:
                speak_func(cleaned_reply)
            else:
                logging.warning("[Groq] Пустой ответ после очистки.")
                speak_func("Я затрудняюсь с ответом.")

            with _HISTORY_LOCK:
                self.history.append({"role": "assistant", "content": cleaned_reply})
                self._trim_history()
                self._save_history()

        except Exception as e:
            logging.error(f"[Groq] Ошибка запроса к API: {e}")
            speak_func("Моё облако мыслей временно недоступно.")
