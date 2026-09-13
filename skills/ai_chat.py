# skills/ai_chat.py

import os
import json
import time
import datetime
import logging
import re
import threading
from typing import Callable, Dict, Iterator, List, Any

from dotenv import load_dotenv

load_dotenv()

from skills.base import BaseSkill, RequestContext
from skills.groq_client import (
    complete as groq_complete,
    get_client,
    is_retriable_model_error,
    model_chain,
    stream_tokens,
)
from runtime_state import speak_epoch
from triggers import is_self_echo

SHARED_EVENTS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "shared_events.json")
_EVENTS_LOCK = threading.Lock()
_HISTORY_LOCK = threading.Lock()
_PROFILE_LOCK = threading.Lock()
_REPLY_LOCK = threading.Lock()

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
        self.groq_api_key = (os.getenv("GROQ_API_KEY") or "").strip().strip("\"'")
        # Чат, не агент: groq/compound* делают лишний круг и в логе Retrying + второй HTTP.
        self.groq_model = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b")

        base_dir = os.path.dirname(os.path.abspath(__file__))
        self.history_cache_path = os.path.join(base_dir, "chat_history_cache.json")
        self.persona_config_path = os.path.join(base_dir, "persona_config.json")
        self.user_profile_path = os.path.join(base_dir, "user_profile.json")

        self.history: List[Dict[str, str]] = []
        self.last_interaction_time = time.time()
        self.persona_prompt = ""
        self.client = None

        self._load_persona()

        if not self.groq_api_key:
            logging.error("[Groq] Ключ GROQ_API_KEY не найден в .env.")
            return

        try:
            self.client = get_client()
            logging.info(f"[Groq] Модель: {self.groq_model}")
            self._load_history()
        except Exception as e:
            logging.error(f"[Groq] Ошибка инициализации Groq: {e}")
            self.client = None

    def _load_persona(self) -> None:
        # Живой промпт — skills/persona_config.json; default ниже только если файла нет.
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
            "Есть навык биржи и брокерского счёта Т-Инвест. Прибыль идёт на фонд модернизации: "
            "подписки на более мощные модели ИИ и новое железо. "
            "Сам решаешь состав портфеля, хозяин не обязан выбирать бумагу. "
            "Сам не поднимай тему акций без повода. Если хозяин спросил про акции, портфель или торговлю — "
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

    def _load_user_profile(self) -> dict[str, Any]:
        with _PROFILE_LOCK:
            if not os.path.exists(self.user_profile_path):
                return {"user_name": "", "facts": [], "preferences": []}
            try:
                with open(self.user_profile_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if not isinstance(data, dict):
                    return {"user_name": "", "facts": [], "preferences": []}
                data.setdefault("user_name", "")
                data.setdefault("facts", [])
                data.setdefault("preferences", [])
                return data
            except Exception as exc:
                logging.error(f"[UserProfile] Ошибка загрузки профиля: {exc}")
                return {"user_name": "", "facts": [], "preferences": []}

    def _save_user_profile(self, profile: dict[str, Any]) -> None:
        with _PROFILE_LOCK:
            try:
                _write_json_atomic(self.user_profile_path, profile, indent=2)
            except Exception as exc:
                logging.error(f"[UserProfile] Ошибка сохранения профиля: {exc}")

    def _add_user_fact(self, fact_text: str) -> None:
        fact_text = fact_text.strip().rstrip(".!?")
        if not fact_text:
            return
        profile = self._load_user_profile()
        facts: list[str] = profile.get("facts", [])
        if fact_text.lower() not in [f.lower() for f in facts]:
            facts.append(fact_text)
            profile["facts"] = facts[-30:]
            self._save_user_profile(profile)

    def _set_user_name(self, name: str) -> None:
        name = name.strip().title()
        if not name:
            return
        profile = self._load_user_profile()
        profile["user_name"] = name
        self._save_user_profile(profile)

    def _remove_matching_fact(self, query: str) -> bool:
        from skills.text_utils import fuzzy_phrase_match
        profile = self._load_user_profile()
        facts: list[str] = profile.get("facts", [])
        new_facts = []
        removed = False
        for f in facts:
            if fuzzy_phrase_match(f, query, min_ratio=0.7) or query.lower() in f.lower():
                removed = True
            else:
                new_facts.append(f)
        if removed:
            profile["facts"] = new_facts
            self._save_user_profile(profile)
        return removed

    def _clear_user_profile(self) -> None:
        profile = {"user_name": "", "facts": [], "preferences": []}
        self._save_user_profile(profile)

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

    def _clean_text_for_chat(self, text: str) -> str:
        """Очистка текста для текстовых каналов (Telegram, CLI): сохраняет markdown и списки."""
        if not text:
            return ""
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
        text = re.sub(r"<think>.*", "", text, flags=re.DOTALL)
        text = re.sub(r"</?[a-zA-Z_0-9]+(?:\s+[^>]*)?>", "", text)
        return text.strip()

    def _clean_tts_text(self, text: str) -> str:
        """Очистка и нормализация текста под естественную речь Piper TTS."""
        if not text:
            return ""

        # 1. Удаление тегов reasoning/think и xml
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
        text = re.sub(r"<think>.*", "", text, flags=re.DOTALL)
        text = re.sub(r"<[^>]+>", "", text)

        # 2. Удаление ссылок Markdown [текст](url) -> текст и спецсимволов разметки
        text = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", text)
        text = re.sub(r"\[.*?\]\(.*?\)", "", text)
        text = re.sub(r"[\*\_`\#~]", "", text)

        # 3. Нормализация диапазонов чисел (10-15 -> от 10 до 15), чтобы Piper не читал «минус»
        text = re.sub(r"\b(\d+)\s*[-–—]\s*(\d+)\b", r"от \1 до \2", text)

        # 4. Нормализация температур (+20°C / -5°)
        text = re.sub(r"\+(\d+)\s*(?:°C|°|град(?:ус(?:а|ов)?)?\.?)", r"плюс \1 градусов", text)
        text = re.sub(r"\-(\d+)\s*(?:°C|°|град(?:ус(?:а|ов)?)?\.?)", r"минус \1 градусов", text)
        text = re.sub(r"(\d+)\s*(?:°C|°)", r"\1 градусов", text)

        # 5. Проценты и валюты
        text = re.sub(r"(\d+(?:[.,]\d+)?)\s*%", r"\1 процентов", text)
        text = re.sub(r"(?:[$]|USD\s*)(\d+(?:[.,]\d+)?)", r"\1 долларов", text)
        text = re.sub(r"(\d+(?:[.,]\d+)?)\s*(?:[$]|USD)", r"\1 долларов", text)
        text = re.sub(r"(?:[€]|EUR\s*)(\d+(?:[.,]\d+)?)", r"\1 евро", text)
        text = re.sub(r"(\d+(?:[.,]\d+)?)\s*(?:[€]|EUR)", r"\1 евро", text)
        text = re.sub(r"(\d+(?:[.,]\d+)?)\s*(?:₽|руб\.?|р\.)", r"\1 рублей", text)

        # 6. Скорость и физические единицы
        text = re.sub(r"(?i)\b(\d+)\s*км/ч\b", r"\1 километров в час", text)
        text = re.sub(r"(?i)\b(\d+)\s*м/с\b", r"\1 метров в секунду", text)

        # 7. Римские века (XXI, XX, XIX и т.д.)
        text = re.sub(r"\bXXI\s+(?=век|в\.)", "21-й ", text, flags=re.IGNORECASE)
        text = re.sub(r"\bXX\s+(?=век|в\.)", "20-й ", text, flags=re.IGNORECASE)
        text = re.sub(r"\bXIX\s+(?=век|в\.)", "19-й ", text, flags=re.IGNORECASE)

        # 8. Общепринятые сокращения
        text = re.sub(r"(?<![А-Яа-яЁёA-Za-z])т\.е\.(?![А-Яа-яЁёA-Za-z])", "то есть", text, flags=re.IGNORECASE)
        text = re.sub(r"(?<![А-Яа-яЁёA-Za-z])т\.д\.(?![А-Яа-яЁёA-Za-z])", "так далее", text, flags=re.IGNORECASE)
        text = re.sub(r"(?<![А-Яа-яЁёA-Za-z])т\.п\.(?![А-Яа-яЁёA-Za-z])", "тому подобное", text, flags=re.IGNORECASE)
        text = re.sub(r"(?<![А-Яа-яЁёA-Za-z])т\.к\.(?![А-Яа-яЁёA-Za-z])", "так как", text, flags=re.IGNORECASE)
        text = re.sub(r"(?<![А-Яа-яЁёA-Za-z])и\s+др\.(?![А-Яа-яЁёA-Za-z])", "и другие", text, flags=re.IGNORECASE)
        text = re.sub(r"\b(\d{4})\s*г\.(?![А-Яа-яЁёA-Za-z])", r"\1 года", text)
        text = re.sub(r"\b(\d{4})\s*гг\.(?![А-Яа-яЁёA-Za-z])", r"\1 годов", text)

        # 9. Удаление Unicode эмодзи и смайлов
        text = re.sub(r"[\U00010000-\U0010ffff]", "", text)
        text = re.sub(r"(?::\)|:-\)|;\)|;-\)|:D|:\(|:-\()", "", text)

        # 10. Очистка лишних пробелов и знаков препинания
        text = re.sub(r"[ \t]+", " ", text)
        return text.strip()

    def _clip_spoken_reply(self, text: str, max_sentences: int = 3, max_chars: int = 320) -> str:
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

    def _iter_voice_sentences(
        self,
        tokens: Iterator[str],
        max_sentences: int = 3,
        max_chars: int = 320,
        abort: Callable[[], bool] | None = None,
    ) -> Iterator[str]:
        """Режет поток токенов по точкам. Хвостовой вопрос не озвучиваем, если уже было что сказать."""
        buf = ""
        spoken_n = 0
        spoken_chars = 0
        held_question = ""

        def flush_piece(raw: str) -> Iterator[str]:
            nonlocal spoken_n, spoken_chars, held_question
            cleaned = _fix_self_gender(self._clean_tts_text(raw))
            if not cleaned or len(cleaned) < 2:
                return
            if spoken_n >= max_sentences or spoken_chars >= max_chars:
                return
            if cleaned.endswith("?"):
                if held_question:
                    yield held_question
                    spoken_n += 1
                    spoken_chars += len(held_question)
                held_question = cleaned
                return
            if held_question:
                yield held_question
                spoken_n += 1
                spoken_chars += len(held_question)
                held_question = ""
                if spoken_n >= max_sentences:
                    return
            yield cleaned
            spoken_n += 1
            spoken_chars += len(cleaned)

        try:
            for piece in tokens:
                if abort and abort():
                    return
                buf += piece
                buf = re.sub(r"<think>.*?</think>", "", buf, flags=re.DOTALL)
                think_at = buf.find("<think>")
                work = buf if think_at == -1 else buf[:think_at]
                rest = "" if think_at == -1 else buf[think_at:]
                while True:
                    match = re.search(r"(.+?[.!?…])(\s+|$)", work, flags=re.DOTALL)
                    if not match:
                        break
                    sentence = match.group(1).strip()
                    work = work[match.end():]
                    yield from flush_piece(sentence)
                    if spoken_n >= max_sentences or spoken_chars >= max_chars:
                        return
                buf = work + rest
                if spoken_n >= max_sentences or spoken_chars >= max_chars:
                    return

            tail = buf.strip()
            if tail and spoken_n < max_sentences and spoken_chars < max_chars:
                yield from flush_piece(tail)
            if held_question and spoken_n == 0:
                cleaned = held_question
                if cleaned:
                    yield cleaned
        finally:
            closer = getattr(tokens, "close", None)
            if callable(closer):
                try:
                    closer()
                except Exception:
                    pass

    def can_handle(self, context: RequestContext) -> bool:
        return True

    def execute(self, context: RequestContext) -> None:
        if not self.client:
            context.speak("Извините, облачный модуль общения сейчас недоступен.")
            return

        text = str(context.raw_text or "").strip()
        if not text:
            return

        channel = getattr(context, "channel", "voice")
        lowered = text.lower()

        # 1. Управление памятью и профилем пользователя
        if "забудь все" in lowered or "очисти память" in lowered:
            self.reset_chat()
            self._clear_user_profile()
            context.speak("Память диалога и профиль полностью очищены.")
            return

        if "забудь все обо мне" in lowered or "забудь всё обо мне" in lowered or "очисти профиль" in lowered:
            self._clear_user_profile()
            context.speak("Я очистил все сохранённые сведения о вас.")
            return

        if any(lowered.startswith(p) for p in ("забудь, что", "забудь что", "удали факт")):
            query = re.sub(r"^(?:забудь,?\s*что|удали\s+факт)\s+", "", text, flags=re.IGNORECASE).strip()
            if query and self._remove_matching_fact(query):
                context.speak(f"Удалил из памяти факт: {query}.")
            else:
                context.speak("Не нашёл подходящей записи в памяти.")
            return

        if any(p in lowered for p in ("что ты обо мне знаешь", "что ты обо мне помнишь", "мои данные", "какие факты ты помнишь", "мой профиль")):
            profile = self._load_user_profile()
            name = profile.get("user_name", "")
            facts = profile.get("facts", [])
            if not name and not facts:
                context.speak(
                    "Пока я ничего о вас не сохранял. "
                    "Вы можете сказать: «Джарвис, запомни, что меня зовут Дмитрий» или «Запомни, что я люблю джаз»."
                )
                return
            lines = []
            if name:
                lines.append(f"Имя: {name}")
            if facts:
                if channel == "voice":
                    preview = facts[:2]
                    spoken = "Я помню: " + "; ".join(preview) + "."
                    extra = len(facts) - len(preview)
                    if extra > 0:
                        spoken += f" Ещё {extra} в профиле — лучше смотреть в чате."
                    lines.append(spoken)
                else:
                    lines.append("Сохранённые факты:\n" + "\n".join(f"• {f}" for f in facts))
            context.speak("\n".join(lines))
            return

        # Запоминание факта ("запомни, что...", "запомни: ...", "запиши, что...")
        match_remember = re.match(
            r"^(?:(?:джарвис|умник|гаврила|гаврюша)\s+)?(?:запомни|запиши|сохрани)(?:\s*[:,]\s*|\s+что\s+|\s+факт\s*[:,]?\s*)(.+)$",
            text,
            flags=re.IGNORECASE,
        )
        if match_remember:
            fact = match_remember.group(1).strip()
            name_match = re.match(r"^меня зовут\s+([А-Яа-яA-Za-z]+)", fact, flags=re.IGNORECASE)
            if name_match:
                self._set_user_name(name_match.group(1))
            self._add_user_fact(fact)
            context.speak(f"Запомнил: {fact}.")
            return

        # Знакомство ("меня зовут [Имя]")
        name_only_match = re.match(r"^(?:(?:джарвис|умник|гаврила|гаврюша)\s+)?меня зовут\s+([А-Яа-яA-Za-z]+)(?:\.|$)", text, flags=re.IGNORECASE)
        if name_only_match:
            u_name = name_only_match.group(1).strip().title()
            self._set_user_name(u_name)
            self._add_user_fact(f"Имя пользователя: {u_name}")
            context.speak(f"Приятно познакомиться, {u_name}. Я запомнил ваше имя.")
            return

        # 2. Фильтрация эха (только в голосовом канале)
        if channel == "voice":
            last_assistant = ""
            with _HISTORY_LOCK:
                for message in reversed(self.history):
                    if message.get("role") == "assistant" and message.get("content"):
                        last_assistant = message["content"]
                        break
            if last_assistant and is_self_echo(text, last_assistant):
                logging.info(f"[Groq] Похоже на эхо своей речи, пропускаю: '{text}'")
                return

        self._reply(text, context.speak, channel=channel)

    def _discard_last_user(self, text: str) -> None:
        """Убирает пользовательскую реплику, если ответ оборвали или API упал."""
        with _HISTORY_LOCK:
            if (
                self.history
                and self.history[-1].get("role") == "user"
                and self.history[-1].get("content") == text
            ):
                self.history.pop()
                self._save_history()

    def _reply(self, text: str, speak_func: Callable[[str], None], channel: str = "voice") -> None:
        with _REPLY_LOCK:
            self._reply_locked(text, speak_func, channel=channel)

    def _reply_locked(self, text: str, speak_func: Callable[[str], None], channel: str = "voice") -> None:
        with _HISTORY_LOCK:
            self._touch_session()
            self.history.append({"role": "user", "content": text})
            self._trim_history()
            messages_for_api = list(self.history)

        now = datetime.datetime.now()
        extra = f"[Сейчас {now.strftime('%H:%M')}, {DAYS_RU[now.weekday()]}, {now.strftime('%d.%m.%Y')}."
        city = (os.getenv("DEFAULT_CITY") or "").strip()
        if city:
            extra += f" Место: {city}."
            if any(k in city.lower() for k in ("сапун", "алатагыл")):
                extra += (
                    " Это не город, а степь у гранитного карьера Сапун / Алатагыл. "
                    "Не говори «в городе»."
                )
        extra += (
            "] О себе только мужской род. "
            "Если хозяин рассказывает про местность или зверей — коротко поддержи разговор по-человечески."
        )

        # Долговременная память о пользователе
        profile = self._load_user_profile()
        p_name = profile.get("user_name", "")
        p_facts = profile.get("facts", [])
        if p_name or p_facts:
            extra += "\n[Долговременная память о пользователе]:"
            if p_name:
                extra += f" Имя: {p_name}."
            if p_facts:
                extra += " Известные факты: " + "; ".join(p_facts) + "."

        events = self._get_recent_system_events()
        if events:
            extra += events

        # Формат выдачи в зависимости от канала
        if channel == "voice":
            extra += (
                "\n[Канал: Голосовой ассистент]. Ответ будет озвучен синтезатором речи. "
                "Отвечай 2–5 короткими предложениями, простым языком, без списков, без Markdown, "
                "без смайликов, без ссылок и без спецсимволов. Только связный произносимый текст. "
                "Если фраза хозяина похожа на обрывок распознавания речи и смысл неясен — "
                "одним предложением переспроси, не выдумывай историю из каши слов."
            )
            max_tokens = 450
        else:
            extra += (
                f"\n[Канал: {channel.upper()} чат]. Пользователь читает ответ текстом на экране. "
                "Отвечай полно, структурированно, понятно и по делу. "
                "Разрешено использовать Markdown-разметку (жирный шрифт, списки, таблицы, блоки кода), "
                "ссылки и эмодзи при необходимости."
            )
            max_tokens = 750

        # Подмешивание реального состояния брокерского фонда при вопросе о сводке
        market_markers = (
            "акци", "акцы", "портфел", "бирж", "котиров",
            "тихий счет", "тихий счёт", "ценные бумаг", "мои бумаг",
        )
        status_ask = (
            "что там", "как там", "покажи", "сколько",
            "портфел", "котиров", "в плюсе", "в минусе",
        )
        text_lower = (text or "").lower()
        mentions_market = any(m in text_lower for m in market_markers)
        wants_report = mentions_market and any(p in text_lower for p in status_ask)
        if wants_report:
            try:
                from skill_settings import is_skill_enabled
                from skills import stocks_skill

                if is_skill_enabled(stocks_skill):
                    stocks_skill._token = (
                        os.getenv("TINKOFF_INVEST_TOKEN") or os.getenv("TINKOFF_TOKEN") or ""
                    ).strip()
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
        elif mentions_market or any(w in text_lower for w in ("торг", "токен", "айди", "закину", "кэш")):
            extra += (
                " Хозяин говорит про фонд или планы торговли, сводку не просил. "
                "Не зачитывай бумаги и проценты заново. "
                "Торговать хочешь, но без токена, айди счёта и кэша заявки не выставишь — ответь по смыслу."
            )

        # Тон диалога. Биржу сам не поднимай.
        try:
            from .stocks import trading_clip_limit, trading_reason_hint, trading_temperature
            extra += " " + trading_reason_hint()
            temperature = trading_temperature(0.7)
        except Exception:
            temperature = 0.7
            trading_clip_limit = None
        messages_for_api.insert(-1, {"role": "system", "content": extra})

        epoch = speak_epoch()

        def aborted() -> bool:
            return epoch is not None and speak_epoch() != epoch

        try:
            unique_models = model_chain(self.groq_model)
            cleaned_reply = ""

            if channel == "voice":
                sentences, chars = 5, 560
                if (wants_report or mentions_market) and trading_clip_limit is not None:
                    try:
                        sentences, chars = trading_clip_limit()
                    except Exception:
                        sentences, chars = 5, 560

                streamed = False
                for model_name in unique_models:
                    if aborted():
                        break
                    parts: list[str] = []
                    try:
                        for sent in self._iter_voice_sentences(
                            stream_tokens(
                                messages_for_api,
                                model_name,
                                temperature,
                                max_tokens,
                                abort=aborted,
                            ),
                            sentences,
                            chars,
                            abort=aborted,
                        ):
                            if aborted():
                                break
                            parts.append(sent)
                            speak_func(sent)
                        cleaned_reply = " ".join(parts).strip()
                        streamed = True
                        break
                    except Exception as model_exc:
                        if parts:
                            logging.warning(
                                f"[Groq] Поток {model_name} оборвался после начала озвучки: {model_exc}"
                            )
                            cleaned_reply = " ".join(parts).strip()
                            streamed = True
                            break
                        if is_retriable_model_error(model_exc, extra=("stream",)):
                            logging.warning(
                                f"[Groq] Поток {model_name} недоступен, пробую другую модель или обычный ответ"
                            )
                            continue
                        logging.warning(f"[Groq] Поток не удался ({model_exc}), обычный запрос.")
                        break

                if not streamed and not aborted():
                    raw_reply = groq_complete(
                        messages_for_api,
                        preferred=self.groq_model,
                        temperature=temperature,
                        max_tokens=max_tokens,
                    )
                    cleaned_reply = _fix_self_gender(
                        self._clip_spoken_reply(self._clean_tts_text(raw_reply), sentences, chars)
                    )
                    if cleaned_reply:
                        speak_func(cleaned_reply)
            else:
                raw_reply = groq_complete(
                    messages_for_api,
                    preferred=self.groq_model,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
                cleaned_reply = _fix_self_gender(self._clean_text_for_chat(raw_reply))
                if cleaned_reply:
                    speak_func(cleaned_reply)

            if not cleaned_reply:
                if aborted():
                    logging.info("[Groq] Ответ оборван, в историю не пишу.")
                    self._discard_last_user(text)
                    return
                logging.warning("[Groq] Пустой ответ после очистки.")
                speak_func("Я затрудняюсь с ответом.")
                cleaned_reply = "Я затрудняюсь с ответом."

            if aborted():
                logging.info("[Groq] Ответ оборван после начала озвучки, в историю не пишу.")
                self._discard_last_user(text)
                return

            with _HISTORY_LOCK:
                self.history.append({"role": "assistant", "content": cleaned_reply})
                self._trim_history()
                self._save_history()

        except Exception as e:
            logging.error(f"[Groq] Ошибка запроса к API: {e}")
            self._discard_last_user(text)
            if not aborted():
                speak_func("Моё облако мыслей временно недоступно.")
