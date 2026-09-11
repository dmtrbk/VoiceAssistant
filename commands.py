# commands.py
# Сравнивать skill is ai_chat_skill, не isinstance. Неизвестное → Groq, не «не понял».
# Мелкий разговор не в NLU. Follow-up ~90 с, только не-чат навык.
# Тихий брокерский счёт (skills/stocks.py) не документировать и не светить в справке.

import logging
import threading
import time
from skills import ALL_SKILLS, local_nlu_skill, ai_chat_skill
from skills.base import RequestContext
from skill_settings import is_skill_enabled
from triggers import is_filler, is_garbled_utterance, split_quick_compound
from context_manager import is_in_context, handle_context_input

_ROUTER_LOCK = threading.Lock()

# Последний навык (не диалог), чтобы «а завтра?» и «ещё» не улетали в Groq.
_FOLLOWUP_TTL_SEC = 90.0
_last_skill = None
_last_skill_time = 0.0


def execute(text: str, speak_callback, channel: str = "voice") -> bool:
    """
    Маршрутизатор: Контекст → NLU → узкие навыки → follow-up → Groq.
    Возвращает True, если сессию нужно усыпить (прощание).
    """
    text = text.lower().strip()
    if not text or is_filler(text):
        return False

    # 1. Если активен интерактивный контекст (игра, опрос, подтверждение)
    if is_in_context():
        handled, should_sleep = handle_context_input(text, speak_callback)
        if handled:
            return should_sleep

    should_sleep = False
    for part in split_quick_compound(text):
        should_sleep = _dispatch_single(part, speak_callback, channel=channel) or should_sleep
    return should_sleep


def forget_skill(skill) -> None:
    """Сбрасывает follow-up, если выключили навык, который только что отвечал."""
    global _last_skill, _last_skill_time
    with _ROUTER_LOCK:
        if _last_skill is skill:
            try:
                _last_skill.on_context_lost()
            except Exception as exc:
                logging.error("[Маршрутизатор] on_context_lost при выключении: %s", exc)
            _last_skill = None
            _last_skill_time = 0.0


def _remember_skill(skill) -> None:
    global _last_skill, _last_skill_time
    with _ROUTER_LOCK:
        if skill is None:
            return
        # Смена темы на разговор: «а завтра?» после зайцев уже не прогноз.
        if skill is ai_chat_skill:
            _last_skill = None
            _last_skill_time = 0.0
            return
        if _last_skill is not None and _last_skill is not skill:
            try:
                _last_skill.on_context_lost()
            except Exception as e:
                logging.error(f"[Маршрутизатор] Ошибка on_context_lost у {_last_skill.__class__.__name__}: {e}")
        _last_skill = skill
        _last_skill_time = time.time()


def _followup_skill(context: RequestContext):
    with _ROUTER_LOCK:
        if _last_skill is None or _last_skill is ai_chat_skill:
            return None
        if not is_skill_enabled(_last_skill):
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
    logging.info(f"[Маршрутизатор] Навык {skill.__class__.__name__} взял команду в обработку (канал: {context.channel}).")
    try:
        skill.execute(context)
    except Exception as e:
        logging.error(f"[Маршрутизатор] Ошибка при выполнении навыка {skill.__class__.__name__}: {e}")


def _record_skill_exchange(skill, user_text: str, spoken: list[str]) -> None:
    if skill is ai_chat_skill or not spoken:
        return
    reply = " ".join(part.strip() for part in spoken if part and str(part).strip())
    if not reply:
        return
    try:
        ai_chat_skill.record_exchange(user_text, reply)
    except Exception as e:
        logging.error(f"[Маршрутизатор] Не удалось записать реплику в память диалога: {e}")


def _dispatch_single(text: str, speak_callback, channel: str = "voice") -> bool:
    intent = ""
    confidence = 0.0

    try:
        # NLUClassifier.predict → tuple, не dict.
        predicted, predicted_conf = local_nlu_skill.nlu_engine.predict(text)
        if predicted:
            intent, confidence = predicted, predicted_conf
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
        speak=capturing_speak,
        channel=channel,
    )

    chosen = None
    blocked = None
    for skill in ALL_SKILLS:
        if skill is ai_chat_skill:
            continue
        try:
            accepts = skill.can_handle(context)
        except Exception as e:
            logging.error(f"[Маршрутизатор] Ошибка can_handle у {skill.__class__.__name__}: {e}")
            continue
        if not accepts:
            continue
        if not is_skill_enabled(skill):
            blocked = skill
            continue
        chosen = skill
        break

    if chosen is None:
        follow = _followup_skill(context)
        if follow is not None:
            logging.info(f"[Маршрутизатор] Follow-up: {follow.__class__.__name__}")
            chosen = follow

    if chosen is None and blocked is not None:
        speak_callback("Этот навык сейчас выключен.")
        return False

    if chosen is None:
        if is_garbled_utterance(text):
            speak_callback("Не расслышал, повторите, пожалуйста.")
            return False
        chosen = ai_chat_skill

    _run_skill(chosen, context)
    _record_skill_exchange(chosen, text, spoken)
    _remember_skill(chosen)

    if not spoken and chosen is not ai_chat_skill:
        logging.info(f"[Маршрутизатор] Навык {chosen.__class__.__name__} не ответил вслух.")

    return bool(context.should_sleep)
