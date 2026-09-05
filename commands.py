# commands.py

import logging
import threading
from skills import ALL_SKILLS, local_nlu_skill
from skills.base import RequestContext
from triggers import is_filler

_EXECUTE_LOCK = threading.Lock()


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
        return _execute_locked(text, speak_callback)


def _execute_locked(text: str, speak_callback) -> bool:
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

    context = RequestContext(
        raw_text=text,
        intent=intent,
        confidence=confidence,
        slots=slots,
        speak=speak_callback,
    )

    handled = False
    for skill in ALL_SKILLS:
        try:
            accepts = skill.can_handle(context)
        except Exception as e:
            logging.error(f"[Маршрутизатор] Ошибка can_handle у {skill.__class__.__name__}: {e}")
            continue
        if not accepts:
            continue

        logging.info(f"[Маршрутизатор] Навык {skill.__class__.__name__} взял команду в обработку.")
        try:
            skill.execute(context)
        except Exception as e:
            logging.error(f"[Маршрутизатор] Ошибка при выполнении навыка {skill.__class__.__name__}: {e}")
        handled = True
        break

    if not handled:
        logging.info("[Маршрутизатор] Ни один навык не смог обработать команду.")
        speak_callback("Извините, я не понял эту команду.")

    return bool(context.should_sleep)
