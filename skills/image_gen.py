# skills/image_gen.py
# «нарисуй …» → Pollinations → Telegram sendPhoto. Ключ не нужен.

from __future__ import annotations

import logging
import os
import random
import re
import time
from typing import Callable
from urllib.parse import quote

import requests

from skills.base import BaseSkill, RequestContext
from skills.utils import send_telegram_notification, telegram_configured

logger = logging.getLogger(__name__)

POLLINATIONS_URL = "https://image.pollinations.ai/prompt/{prompt}"
POLLINATIONS_MODELS = ("sana", "flux")
KEEP_FILES = 20
TELEGRAM_CAPTION_LIMIT = 900
_EXPAND_SYSTEM = (
    "You write prompts for an image model. The user describes a picture, often in Russian. "
    "Reply with one English prompt only: 35-70 words, subject first, then pose, setting, "
    "lighting and style. Photorealistic unless they asked another style. "
    "No quotes, markdown, preamble or refusal. No text, logos or watermarks in the scene. "
    "Keep their subject exactly. Ordinary animals and objects are fine."
)

_WAKE = r"(?:джарвис|умник|гаврила|гаврюша)\s+"
_POLITE = r"(?:пожалуйста\s+)*"
_NOUN = r"(?:картинку|изображение|рисунок|фото)"
_ABOUT = r"(?:\s+(?:с|про|на тему))?"
# Императив + частые ошибки Vosk: «нарисуй» → «нарисует» / «нарисуешь»
_DRAW_VERB = (
    r"(?:нарисуй(?:-ка|те)?|нарисует|нарисуешь|нарисуем|нарисуя|рисуй|порисуй)"
)

# «нарисуй [мне] [картинку с] рыжего кота»
_COMMAND_RE = re.compile(
    rf"^(?:{_WAKE})?{_POLITE}"
    rf"(?:"
    rf"{_DRAW_VERB}(?:\s+пожалуйста)*(?:\s+мне)?(?:\s+пожалуйста)*"
    rf"(?:\s+{_NOUN}{_ABOUT})?\s+"
    rf"|"
    rf"(?:сгенерируй|создай|сделай)(?:\s+пожалуйста)*(?:\s+мне)?\s+"
    rf"{_NOUN}{_ABOUT}\s+"
    rf")"
    rf"(?P<prompt>.+)$",
    re.IGNORECASE | re.DOTALL,
)

_HANDLE_RE = re.compile(
    rf"(?:{_WAKE})?{_POLITE}"
    rf"(?:"
    rf"{_DRAW_VERB}(?:\s+пожалуйста)*(?:\s+мне)?"
    rf"|"
    rf"(?:сгенерируй|создай|сделай)(?:\s+пожалуйста)*(?:\s+мне)?\s+{_NOUN}"
    rf")",
    re.IGNORECASE,
)

_REJECT_RE = re.compile(
    r"(?:\bкак\s+нарису|\bчто\s+нарису|\bкто\s+нарису|"
    r"\bумеешь\s+рисов|\bможешь\s+(?:ли\s+)?рисов|"
    r"\bнаучи|\bнарисовать\b)",
    re.IGNORECASE,
)

FOLLOWUP_EXACT = frozenset({
    "еще", "ещё", "еще раз", "ещё раз",
    "давай еще", "давай ещё",
    "еще одну", "ещё одну", "еще один", "ещё один",
    "другую", "другой", "другую картинку", "другой вариант",
    "повтори", "ещё такую", "еще такую",
})

# Vosk часто съедает «нарисуй» → «все рыжего кота на крыльце»
_STT_JUNK = re.compile(
    r"^(?:(?:джарвис|умник|гаврила|гаврюша)\s+)?(?:все|всё|ну|давай|там|этот|это)\s+",
    re.IGNORECASE,
)
_TALK_VERBS = re.compile(
    r"\b(расскажи|скажи|видел|видела|был|была|иду|пойду|включи|выключи|"
    r"поставь|найди|открой|какая|какой|какое|сколько|почему|зачем)\b",
    re.IGNORECASE,
)
_SUBJECT = re.compile(
    r"\b("
    r"кот|кота|коту|кошка|кошку|кошк|"
    r"собак|п[её]с|щен|"
    r"дракон|робот|машин|"
    r"дом|замок|корабл|"
    r"закат|рассвет|космос|лун|"
    r"гор[аыеу]|лес|море|океан|"
    r"человек|девушк|парень|рыцар|"
    r"цвет|шляп|крыльц"
    r")\w*\b",
    re.IGNORECASE,
)
_SETTING = re.compile(
    r"\b(?:на|в|во|под|у|возле|около|среди|про|с)\s+[а-яё]{3,}",
    re.IGNORECASE,
)


def _bare_scene(text: str) -> str:
    cleaned = _STT_JUNK.sub("", (text or "").lower().strip())
    return re.sub(r"\s+", " ", cleaned).strip(" .,!?…")


def is_scene_prompt(text: str) -> bool:
    """Описание кадра без глагола «нарисуй»: «рыжего кота на деревянном крыльце»."""
    cleaned = _bare_scene(text)
    if not cleaned or _TALK_VERBS.search(cleaned) or _REJECT_RE.search(cleaned):
        return False
    words = cleaned.split()
    if len(words) < 3 or len(words) > 14:
        return False
    return bool(_SUBJECT.search(cleaned) and _SETTING.search(cleaned))


def is_draw_command(text: str) -> bool:
    lowered = (text or "").lower().strip()
    if not lowered or _REJECT_RE.search(lowered):
        return False
    return bool(_HANDLE_RE.search(lowered) or is_scene_prompt(lowered))


def extract_prompt(text: str) -> str:
    raw = (text or "").strip()
    if not raw:
        return ""
    match = _COMMAND_RE.match(raw)
    if match:
        prompt = match.group("prompt")
    elif is_scene_prompt(raw):
        prompt = _bare_scene(raw)
    else:
        prompt = ""
    return re.sub(r"\s+", " ", prompt).strip(" \t.,!?…")


def is_followup_redraw(text: str) -> bool:
    lowered = (text or "").lower().strip()
    if lowered in FOLLOWUP_EXACT:
        return True
    words = lowered.split()
    if not words or len(words) > 4:
        return False
    return any(phrase in lowered for phrase in ("еще", "ещё", "другую", "другой", "повтори"))


def _looks_like_image(data: bytes, content_type: str = "") -> bool:
    if data.startswith(b"\xff\xd8\xff") or data.startswith(b"\x89PNG"):
        return True
    return content_type.startswith("image/")


def fallback_prompt(user_text: str) -> str:
    subject = re.sub(r"\s+", " ", (user_text or "").strip()).strip(".,!?")
    if not subject:
        subject = "an unexpected beautiful scene"
    return (
        f"High quality photorealistic image of {subject}, detailed textures, "
        "natural lighting, sharp focus, no text, no watermark, no logo"
    )


def _clean_expanded(raw: str) -> str:
    text = (raw or "").strip()
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    text = re.sub(r"^```(?:\w+)?\s*|\s*```$", "", text).strip()
    text = text.strip(" \"'`")
    text = re.sub(r"\s+", " ", text)
    if len(text) > 600:
        text = text[:600].rsplit(" ", 1)[0]
    return text


def expand_prompt(user_text: str, complete: Callable[..., str] | None = None) -> str:
    """Короткий русский запрос → детальный английский промпт. Без Groq — шаблон."""
    fallback = fallback_prompt(user_text)
    try:
        if complete is None:
            from skills.groq_client import FAST_MODEL, complete as groq_complete

            complete = groq_complete
            preferred = FAST_MODEL
        else:
            preferred = None
        raw = complete(
            [
                {"role": "system", "content": _EXPAND_SYSTEM},
                {"role": "user", "content": user_text},
            ],
            preferred=preferred,
            temperature=0.4,
            max_tokens=180,
        )
    except Exception as exc:
        logger.warning("[Картинки] Не развернул промпт через Groq: %s", exc)
        return fallback
    cleaned = _clean_expanded(raw)
    if len(cleaned) < 12:
        return fallback
    return cleaned


def generate_image(
    prompt: str,
    *,
    get: Callable[..., requests.Response] | None = None,
    models: tuple[str, ...] | None = None,
    seed: int | None = None,
) -> tuple[bytes, str]:
    getter = get or requests.get
    seed = random.randint(1, 2_000_000_000) if seed is None else seed
    last_err = "Сервис картинок не ответил"
    for model_name in models or POLLINATIONS_MODELS:
        url = POLLINATIONS_URL.format(prompt=quote(prompt, safe=""))
        try:
            response = getter(
                url,
                params={
                    "model": model_name,
                    "width": 1024,
                    "height": 1024,
                    "nologo": "true",
                    "private": "true",
                    "enhance": "true",
                    "seed": seed,
                },
                headers={"User-Agent": "VoiceAssistant/1.0"},
                timeout=90,
            )
        except requests.RequestException as exc:
            last_err = str(exc)
            logger.warning("[Картинки] %s не ответил: %s", model_name, exc)
            continue
        content_type = (response.headers.get("content-type") or "").split(";", 1)[0].strip()
        data = response.content or b""
        if response.status_code >= 400 or not _looks_like_image(data, content_type):
            last_err = f"HTTP {response.status_code}"
            logger.warning("[Картинки] %s: %s", model_name, last_err)
            continue
        return data, model_name
    raise RuntimeError(last_err)


def _default_save_dir() -> str:
    pictures = os.path.expanduser("~/Изображения")
    if not os.path.isdir(pictures):
        pictures = os.path.expanduser("~/Pictures")
    return os.path.join(pictures, "Jarvis")


def _image_suffix(data: bytes) -> str:
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    return ".png"


class ImageGenSkill(BaseSkill):
    """Рисует картинку и отправляет её в Telegram."""

    def __init__(self, save_dir: str | None = None):
        self.save_dir = save_dir or _default_save_dir()
        self._last_prompt = ""

    def can_handle(self, context: RequestContext) -> bool:
        return is_draw_command(context.raw_text)

    def accepts_followup(self, context: RequestContext) -> bool:
        return bool(self._last_prompt) and is_followup_redraw(context.raw_text)

    def execute(self, context: RequestContext) -> None:
        speak = context.speak
        if speak is None:
            return

        text = str(context.raw_text or "").strip()
        prompt = extract_prompt(text)
        if not prompt and is_followup_redraw(text):
            prompt = self._last_prompt
        if not prompt:
            speak("Что нарисовать?")
            return

        self._last_prompt = prompt
        speak("Рисую.")
        drawn = expand_prompt(prompt)
        logger.info("[Картинки] Промпт: %s", drawn)
        try:
            image_bytes, model_name = generate_image(drawn)
        except Exception as exc:
            logger.error("[Картинки] Ошибка генерации: %s", exc)
            speak("Не рисует.")
            return

        path = self._save_image(image_bytes)
        if not path:
            speak("Не сохранил.")
            return

        logger.info("[Картинки] Файл %s модель %s", path, model_name)
        caption = prompt[:TELEGRAM_CAPTION_LIMIT]
        sent = False
        if telegram_configured():
            sent = bool(send_telegram_notification(caption, photo_path=path, background=False))
        else:
            logger.warning("[Картинки] Telegram не настроен, файл только на диске: %s", path)

        try:
            from skills.ai_chat import log_system_action

            log_system_action(f"Навык картинок сохранил файл: {prompt}")
        except Exception:
            pass

        channel = getattr(context, "channel", "voice")
        if sent:
            if channel != "telegram":
                speak("Отправил.")
            return
        if telegram_configured():
            speak("Не ушло.")
            return
        speak("Сохранил.")

    def _save_image(self, image_bytes: bytes) -> str:
        try:
            os.makedirs(self.save_dir, exist_ok=True)
            name = time.strftime("img_%Y%m%d_%H%M%S") + _image_suffix(image_bytes)
            path = os.path.join(self.save_dir, name)
            with open(path, "wb") as handle:
                handle.write(image_bytes)
            self._prune_old()
            return path
        except Exception as exc:
            logger.error("[Картинки] Не удалось сохранить файл: %s", exc)
            return ""

    def _prune_old(self) -> None:
        try:
            names = [
                os.path.join(self.save_dir, name)
                for name in os.listdir(self.save_dir)
                if name.startswith("img_") and name.endswith((".png", ".jpg", ".jpeg"))
            ]
            names.sort(key=os.path.getmtime, reverse=True)
            for stale in names[KEEP_FILES:]:
                try:
                    os.remove(stale)
                except OSError:
                    pass
        except OSError:
            return
