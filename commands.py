# commands.py
# Сравнивать skill is ai_chat_skill, не isinstance. Неизвестное → Groq, не «не понял».
# Мелкий разговор не в NLU. Follow-up ~90 с, только не-чат навык.
# «что?» / пауза «мм» — dialogue_repair, каша STT — уточнение, не поиск.

import logging
import threading
import time
from skills import ALL_SKILLS, local_nlu_skill, ai_chat_skill
from skills.base import RequestContext
from skill_settings import is_skill_enabled
from triggers import (
    is_emergency_stop,
    is_filler,
    is_garbled_utterance,
    is_weak_stt_for_chat,
    is_hold_interrupt,
    is_sleep_command,
    split_quick_compound,
)
from context_manager import clear_active_context, handle_context_input, is_in_context
from dialogue_repair import (
    ask_bare_action,
    drop_pending,
    early_dialogue_turn,
    has_pending,
    is_bare_action,
    next_no_match_line,
    remember_spoken,
    set_pending_prefix,
    slot_clarify,
    take_pending_rewrite,
)
from runtime_state import bump_speak_epoch, stop_extra_tts


def _noop_speak(_text: str) -> None:
    return None

_ROUTER_LOCK = threading.Lock()
_EXEC_LOCK = threading.Lock()

# Последний навык (не диалог), чтобы «а завтра?» и «ещё» не улетали в Groq.
_FOLLOWUP_TTL_SEC = 90.0
_last_skill = None
_last_skill_time = 0.0


def _match_narrow_skill(context: RequestContext):
    """Первый узкий навык (не чат/NLU) и выключенные, уже просмотренные."""
    disabled: list = []
    for skill in ALL_SKILLS:
        if skill is ai_chat_skill or skill is local_nlu_skill:
            continue
        if not is_skill_enabled(skill):
            disabled.append(skill)
            continue
        try:
            accepts = skill.can_handle(context)
        except Exception as e:
            logging.error(f"[Маршрутизатор] Ошибка can_handle у {skill.__class__.__name__}: {e}")
            continue
        if accepts:
            return skill, disabled
    return None, disabled


def _channel_session_command(text: str, speak_callback, channel: str) -> bool | None:
    """Стоп / замолчи / спать для CLI и Telegram. Голос это делает в цикле Vosk."""
    if channel == "voice":
        return None
    if is_emergency_stop(text):
        bump_speak_epoch()
        stop_extra_tts()
        try:
            from player_control import emergency_silence
            emergency_silence()
        except Exception as exc:
            logging.error("[Маршрутизатор] emergency_silence: %s", exc)
        return True
    if is_hold_interrupt(text):
        bump_speak_epoch()
        stop_extra_tts()
        return False
    if is_sleep_command(text):
        bump_speak_epoch()
        stop_extra_tts()
        clear_active_context(call_on_exit=True, speak_callback=speak_callback)
        return True
    return None


def execute(
    text: str,
    speak_callback,
    channel: str = "voice",
    alert_speak=None,
    *,
    skip_early: bool = False,
) -> bool:
    """
    Маршрутизатор: Контекст → NLU → узкие навыки → follow-up → Groq.
    Возвращает True, если сессию нужно усыпить (прощание).
    skip_early: голос уже разобрал паузу / «что?» / кашу в цикле Vosk.
    """
    text = text.lower().strip()
    if not skip_early:
        early = early_dialogue_turn(text)
        if early is not None:
            kind, reply = early
            if kind == "speak" and reply:
                speak_callback(reply)
            return False
        if not text or is_filler(text):
            return False

    pending = has_pending()
    bare = is_bare_action(text)
    if pending and not bare:
        probe = RequestContext(raw_text=text, speak=_noop_speak)
        chosen, _disabled = _match_narrow_skill(probe)
        if chosen is not None or _followup_skill(probe) is not None:
            drop_pending()
        else:
            rewritten = take_pending_rewrite(text)
            if rewritten == "":
                speak_callback(slot_clarify("object"))
                return False
            if rewritten:
                logging.info("[Диалог] Дособрал команду: '%s' → '%s'", text, rewritten)
                text = rewritten
                bare = is_bare_action(text)
    elif pending and bare:
        take_pending_rewrite(text)
        speak_callback(ask_bare_action(text))
        return False

    if bare:
        set_pending_prefix(text)
        speak_callback(ask_bare_action(text))
        return False

    with _EXEC_LOCK:
        return _execute_unlocked(
            text,
            speak_callback,
            channel=channel,
            alert_speak=alert_speak,
        )


def _execute_unlocked(
    text: str,
    speak_callback,
    channel: str = "voice",
    alert_speak=None,
) -> bool:
    # 1. Если активен интерактивный контекст (игра, опрос, подтверждение)
    if is_in_context():
        handled, should_sleep = handle_context_input(text, speak_callback)
        if handled:
            return should_sleep

    session = _channel_session_command(text, speak_callback, channel)
    if session is not None:
        return session

    should_sleep = False
    for part in split_quick_compound(text):
        should_sleep = _dispatch_single(
            part,
            speak_callback,
            channel=channel,
            alert_speak=alert_speak,
        ) or should_sleep
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


def _dispatch_single(
    text: str,
    speak_callback,
    channel: str = "voice",
    alert_speak=None,
) -> bool:
    spoken: list[str] = []

    def capturing_speak(reply_text: str) -> None:
        if reply_text:
            spoken.append(str(reply_text))
            if channel != "voice":
                remember_spoken(str(reply_text))
        speak_callback(reply_text)

    context = RequestContext(
        raw_text=text,
        speak=capturing_speak,
        alert_speak=alert_speak or speak_callback,
        channel=channel,
    )

    chosen, disabled = _match_narrow_skill(context)

    if chosen is None:
        follow = _followup_skill(context)
        if follow is not None:
            logging.info(f"[Маршрутизатор] Follow-up: {follow.__class__.__name__}")
            chosen = follow

    if chosen is None and disabled:
        for skill in disabled:
            try:
                accepts = skill.can_handle(context)
            except Exception as e:
                logging.error(f"[Маршрутизатор] Ошибка can_handle у {skill.__class__.__name__}: {e}")
                continue
            if accepts:
                speak_callback("Этот навык сейчас выключен.")
                return False

    if chosen is None:
        if is_garbled_utterance(text) or (
            channel == "voice" and is_weak_stt_for_chat(text)
        ):
            speak_callback(next_no_match_line())
            return False
        try:
            predicted, predicted_conf = local_nlu_skill.nlu_engine.predict(text)
            if predicted:
                context.intent = predicted
                context.confidence = predicted_conf
                if local_nlu_skill.can_handle(context) and is_skill_enabled(local_nlu_skill):
                    chosen = local_nlu_skill
        except Exception as e:
            logging.error(f"[NLU] Не удалось классифицировать текст: {e}")
        if chosen is None:
            chosen = ai_chat_skill

    _run_skill(chosen, context)
    _record_skill_exchange(chosen, text, spoken)
    _remember_skill(chosen)

    if not spoken and chosen is not ai_chat_skill:
        logging.info(f"[Маршрутизатор] Навык {chosen.__class__.__name__} не ответил вслух.")

    return bool(context.should_sleep)
