# commands.py

import logging
import threading
import time
from skills import ALL_SKILLS, local_nlu_skill, ai_chat_skill
from skills.base import RequestContext
from skills.ai_chat import AIChatSkill
from triggers import is_filler, is_garbled_utterance, split_quick_compound

_EXECUTE_LOCK = threading.Lock()

# Последний навык (не диалог), чтобы «а завтра?» и «ещё» не улетали в Groq.
_FOLLOWUP_TTL_SEC = 90.0
_last_skill = None
_last_skill_time = 0.0


def execute(text: str, speak_callback) -> bool:
    """
    Основной маршрутизатор команд.
    Принимает текст от ассистента, обогащает его данными NLU (если возможно)
    и передает по цепочке приоритетов в зарегистрированные навыки.
    Возвращает True, если сессию нужно усыпить (прощание).
    """
    text = text.lower().strip()
    if not text or is_filler(text):
        return False

    with _EXECUTE_LOCK:
        parts = split_quick_compound(text)
        if len(parts) > 1:
            logging.info(f"[Маршрутизатор] Составная команда: {parts}")
            should_sleep = False
            any_handled = False
            for part in parts:
                handled, sleep = _execute_locked(part, speak_callback)
                any_handled = any_handled or handled
                should_sleep = should_sleep or sleep
            if not any_handled:
                speak_callback("Извините, я не понял эту команду.")
            return should_sleep
        handled, should_sleep = _execute_locked(text, speak_callback)
        if not handled:
            speak_callback("Извините, я не понял эту команду.")
        return should_sleep


def _remember_skill(skill) -> None:
    global _last_skill, _last_skill_time
    if skill is None or isinstance(skill, AIChatSkill):
        return
    if _last_skill is not None and _last_skill is not skill:
        try:
            _last_skill.on_context_lost()
        except Exception as e:
            logging.error(f"[Маршрутизатор] Ошибка on_context_lost у {_last_skill.__class__.__name__}: {e}")
    _last_skill = skill
    _last_skill_time = time.time()


def _followup_skill(context: RequestContext):
    if _last_skill is None or isinstance(_last_skill, AIChatSkill):
        return None
    if time.time() - _last_skill_time > _FOLLOWUP_TTL_SEC:
        return None
    try:
        if _last_skill.accepts_followup(context):
            return _last_skill
    except Exception as e:
        logging.error(f"[Маршрутизатор] Ошибка accepts_followup у {_last_skill.__class__.__name__}: {e}")
    return None


def _run_skill(skill, context: RequestContext) -> None:
    logging.info(f"[Маршрутизатор] Навык {skill.__class__.__name__} взял команду в обработку.")
    try:
        skill.execute(context)
    except Exception as e:
        logging.error(f"[Маршрутизатор] Ошибка при выполнении навыка {skill.__class__.__name__}: {e}")


def _record_skill_exchange(skill, user_text: str, spoken: list[str]) -> None:
    if isinstance(skill, AIChatSkill) or not spoken:
        return
    reply = " ".join(part.strip() for part in spoken if part and str(part).strip())
    if not reply:
        return
    try:
        ai_chat_skill.record_exchange(user_text, reply)
    except Exception as e:
        logging.error(f"[Маршрутизатор] Не удалось записать реплику в память диалога: {e}")


def _execute_locked(text: str, speak_callback) -> tuple[bool, bool]:
    intent = ""
    confidence = 0.0
    slots = {}

    try:
        res = local_nlu_skill.nlu_engine.predict(text)
        if isinstance(res, tuple) and len(res) >= 2 and res[0]:
            intent, confidence = res[0], res[1]
        elif isinstance(res, dict):
            intent = res.get("intent", "")
            confidence = res.get("confidence", 0.0)
            slots = res.get("slots", {})
    except Exception as e:
        logging.error(f"[NLU] Не удалось классифицировать текст: {e}")

    spoken: list[str] = []

    def capturing_speak(reply_text: str) -> None:
        if reply_text:
            spoken.append(str(reply_text))
        speak_callback(reply_text)

    context = RequestContext(
        raw_text=text,
        intent=intent,
        confidence=confidence,
        slots=slots,
        speak=capturing_speak,
    )

    chosen = None
    for skill in ALL_SKILLS:
        if isinstance(skill, AIChatSkill):
            continue
        try:
            accepts = skill.can_handle(context)
        except Exception as e:
            logging.error(f"[Маршрутизатор] Ошибка can_handle у {skill.__class__.__name__}: {e}")
            continue
        if not accepts:
            continue
        chosen = skill
        break

    if chosen is None:
        follow = _followup_skill(context)
        if follow is not None:
            logging.info(f"[Маршрутизатор] Follow-up: {follow.__class__.__name__}")
            chosen = follow

    if chosen is None:
        if is_garbled_utterance(text):
            speak_callback("Не расслышал, повторите, пожалуйста.")
            return True, False
        chosen = ai_chat_skill

    _run_skill(chosen, context)
    _record_skill_exchange(chosen, text, spoken)
    _remember_skill(chosen)

    if not spoken and chosen is not ai_chat_skill:
        logging.info(f"[Маршрутизатор] Навык {chosen.__class__.__name__} не ответил вслух.")

    return True, bool(context.should_sleep)
