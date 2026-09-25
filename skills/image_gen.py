# skills/image_gen.py
# «нарисуй …» → Cloudflare Workers AI (FLUX.1 schnell) → файл и Telegram.

from __future__ import annotations

import base64
import logging
import os
import re
import time
from typing import Callable

import requests

from skills.base import BaseSkill, RequestContext
from skills.utils import send_telegram_notification, telegram_configured

logger = logging.getLogger(__name__)

CF_RUN_URL = "https://api.cloudflare.com/client/v4/accounts/{account}/ai/run/{model}"
CF_FLUX_SCHNELL = "@cf/black-forest-labs/flux-1-schnell"
PROMPT_MAX = 2048
DEFAULT_STEPS = 4
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
# Диалог / биржа — не кадр. «песочница» иначе ловится как «пёс».
_NOT_SCENE = re.compile(
    r"(?:"
    r"\bсч[её]т\b|\bторг\w*|\bбирж\w*|\bпесочниц\w*|\bзарабатыв\w*|"
    r"\bакци\w*|\bкрипт\w*|\bпортфел\w*|\bброкер\w*|"
    r"\bпереключ\w*|\bреальн\w*\s+торг"
    r")",
    re.IGNORECASE,
)
# Короткие основы без жадного \w*: «пёс» ≠ «песочница», «лес» ≠ «переключимся».
_SUBJECT = re.compile(
    r"\b("
    r"кот(?:а|у|ом|е|ы|ов)?|кошк\w*|"
    r"собак\w*|п[её]с(?:а|у|ом|е|ы|ов|ам|ами|ах)?|щен(?:ок|ка|ку|ком|ке|ки|ков)?|"
    r"дракон\w*|робот\w*|машин\w*|"
    r"дом(?:а|у|ом|е)?|замок\w*|корабл\w*|"
    r"закат\w*|рассвет\w*|космос\w*|лун\w*|"
    r"гор[аыеу]|лес(?:а|у|ом|е|а)?|море|океан\w*|"
    r"человек\w*|девушк\w*|парень|парня|рыцар\w*|"
    r"цвет\w*|шляп\w*|крыльц\w*"
    r")\b",
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
    if _NOT_SCENE.search(cleaned):
        return False
    words = cleaned.split()
    if len(words) < 3 or len(words) > 14:
        return False
    return bool(_SUBJECT.search(cleaned) and _SETTING.search(cleaned))


def is_explicit_draw_command(text: str) -> bool:
    """Явно «нарисуй / сделай картинку …», без угадывания сцены."""
    lowered = (text or "").lower().strip()
    if not lowered or _REJECT_RE.search(lowered):
        return False
    return bool(_HANDLE_RE.search(lowered))


def is_draw_command(text: str, *, allow_bare_scene: bool = True) -> bool:
    lowered = (text or "").lower().strip()
    if not lowered or _REJECT_RE.search(lowered):
        return False
    if _HANDLE_RE.search(lowered):
        return True
    return bool(allow_bare_scene and is_scene_prompt(lowered))


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
    """Короткий русский запрос → детальный английский промпт. Без LLM — шаблон."""
    fallback = fallback_prompt(user_text)
    try:
        if complete is None:
            from skills.openai_client import FAST_MODEL, complete as llm_complete

            complete = llm_complete
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
        logger.warning("[Картинки] Не развернул промпт через LLM: %s", exc)
        return fallback
    cleaned = _clean_expanded(raw)
    if len(cleaned) < 12:
        return fallback
    return cleaned


class ImageBackendNotConfigured(RuntimeError):
    """Нет или отклонён CLOUDFLARE_ACCOUNT_ID / CLOUDFLARE_API_TOKEN."""


class ImageQuotaError(RuntimeError):
    """Дневной лимит нейронов Cloudflare."""


def cloudflare_credentials() -> tuple[str, str]:
    account = (
        os.getenv("CLOUDFLARE_ACCOUNT_ID") or os.getenv("CF_ACCOUNT_ID") or ""
    ).strip().strip("\"'")
    token = (
        os.getenv("CLOUDFLARE_API_TOKEN")
        or os.getenv("CLOUDFLARE_AI_TOKEN")
        or os.getenv("CF_API_TOKEN")
        or ""
    ).strip().strip("\"'")
    return account, token


def cloudflare_configured() -> bool:
    account, token = cloudflare_credentials()
    return bool(account and token)


def _image_model() -> str:
    raw = (os.getenv("CLOUDFLARE_IMAGE_MODEL") or CF_FLUX_SCHNELL).strip()
    return raw or CF_FLUX_SCHNELL


def _image_steps(override: int | None = None) -> int:
    if override is not None:
        value = override
    else:
        raw = (os.getenv("CLOUDFLARE_IMAGE_STEPS") or "").strip()
        try:
            value = int(raw) if raw else DEFAULT_STEPS
        except ValueError:
            value = DEFAULT_STEPS
    return max(1, min(8, int(value)))


def _model_short_name(model: str) -> str:
    return model.rsplit("/", 1)[-1].lstrip("@") or model


def _decode_image_b64(raw: str) -> bytes:
    text = (raw or "").strip()
    if text.lower().startswith("data:") and "," in text:
        text = text.split(",", 1)[1]
    text = re.sub(r"\s+", "", text)
    pad = (-len(text)) % 4
    if pad:
        text += "=" * pad
    return base64.b64decode(text)


def _cf_error_text(payload: object, status: int) -> str:
    if isinstance(payload, dict):
        errors = payload.get("errors") or []
        messages = []
        for item in errors:
            if isinstance(item, dict) and item.get("message"):
                messages.append(str(item["message"]))
            elif item:
                messages.append(str(item))
        if messages:
            return "; ".join(messages)
        for key in ("error", "message"):
            if payload.get(key):
                return str(payload[key])
    return f"HTTP {status}"


def generate_image(
    prompt: str,
    *,
    post: Callable[..., requests.Response] | None = None,
    steps: int | None = None,
    account_id: str | None = None,
    api_token: str | None = None,
    model: str | None = None,
) -> tuple[bytes, str]:
    """Workers AI REST: POST /ai/run/@cf/black-forest-labs/flux-1-schnell → JPEG."""
    creds_account, creds_token = cloudflare_credentials()
    account = (account_id if account_id is not None else creds_account).strip()
    token = (api_token if api_token is not None else creds_token).strip()
    if not account or not token:
        raise ImageBackendNotConfigured(
            "Нужны CLOUDFLARE_ACCOUNT_ID и CLOUDFLARE_API_TOKEN"
        )

    model_name = (model or _image_model()).strip() or CF_FLUX_SCHNELL
    poster = post or requests.post
    url = CF_RUN_URL.format(account=account, model=model_name)
    # Живой REST-схемы flux-1-schnell: только prompt и steps. Поле seed отклоняется.
    try:
        response = poster(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json={
                "prompt": (prompt or "")[:PROMPT_MAX],
                "steps": _image_steps(steps),
            },
            timeout=90,
        )
    except requests.RequestException as exc:
        logger.warning("[Картинки] Cloudflare не ответил: %s", exc)
        raise RuntimeError(str(exc)) from exc

    if response.status_code == 429:
        raise ImageQuotaError("Лимит нейронов Cloudflare")

    content_type = (response.headers.get("content-type") or "").split(";", 1)[0].strip()
    data = response.content or b""
    if _looks_like_image(data, content_type):
        return data, _model_short_name(model_name)

    try:
        payload: object = response.json()
    except ValueError:
        payload = None

    if response.status_code >= 400:
        err = _cf_error_text(payload, response.status_code)
        logger.warning("[Картинки] Cloudflare: %s", err)
        if response.status_code in (401, 403):
            raise ImageBackendNotConfigured(err)
        raise RuntimeError(err)

    image_b64 = ""
    if isinstance(payload, dict):
        if payload.get("success") is False:
            raise RuntimeError(_cf_error_text(payload, response.status_code))
        result = payload.get("result")
        if isinstance(result, dict):
            image_b64 = str(result.get("image") or result.get("image_b64") or "")
        elif isinstance(result, str):
            image_b64 = result
        if not image_b64:
            image_b64 = str(payload.get("image") or "")

    if not image_b64:
        raise RuntimeError("Cloudflare не вернул картинку")
    decoded = _decode_image_b64(image_b64)
    if not _looks_like_image(decoded):
        raise RuntimeError("Cloudflare вернул не картинку")
    return decoded, _model_short_name(model_name)


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
        # В Telegram/CLI «голая» сцена без «нарисуй» — слишком часто чужой диалог.
        # Bare scene оставляем для голоса: Vosk часто съедает глагол.
        channel = getattr(context, "channel", "voice") or "voice"
        allow_bare = channel == "voice"
        return is_draw_command(context.raw_text, allow_bare_scene=allow_bare)

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
        if not cloudflare_configured():
            logger.error(
                "[Картинки] Нужны CLOUDFLARE_ACCOUNT_ID и CLOUDFLARE_API_TOKEN в .env."
            )
            speak("Нет ключа Cloudflare.")
            return

        speak("Рисую.")
        drawn = expand_prompt(prompt)
        logger.info("[Картинки] Промпт: %s", drawn)
        try:
            image_bytes, model_name = generate_image(drawn)
        except ImageBackendNotConfigured:
            speak("Нет ключа Cloudflare.")
            return
        except ImageQuotaError:
            logger.warning("[Картинки] Дневной лимит нейронов Cloudflare.")
            speak("Лимит на сегодня.")
            return
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
