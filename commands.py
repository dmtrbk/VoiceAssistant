# commands.py

import logging
from skills import ALL_SKILLS, local_nlu_skill
from skills.base import RequestContext

def execute(text: str, speak_callback) -> None:
    """
    Основной маршрутизатор команд.
    Принимает текст от ассистента, обогащает его данными NLU (если возможно)
    и передает по цепочке приоритетов в зарегистрированные навыки.
    """
    text = text.lower().strip()
    if not text:
        return

    intent = ""
    confidence = 0.0
    slots = {}

    # 1. Пытаемся классифицировать текст через локальный NLU
    try:
        nlu_engine = local_nlu_skill.nlu_engine
        
        # Динамически проверяем, какой метод классификации реализован в вашем nlu.py
        if hasattr(nlu_engine, "classify"):
            res = nlu_engine.classify(text)
        elif hasattr(nlu_engine, "predict"):
            res = nlu_engine.predict(text)
        else:
            res = None
            logging.warning("[NLU] В классе NLUClassifier не найден метод classify или predict. Проверьте ваш nlu.py.")

        # Разбираем результат классификации
        if res:
            if isinstance(res, tuple) and len(res) >= 2:
                intent, confidence = res[0], res[1]
            elif isinstance(res, dict):
                intent = res.get("intent", "")
                confidence = res.get("confidence", 0.0)
                slots = res.get("slots", {})
    except Exception as e:
        logging.error(f"[NLU] Не удалось классифицировать текст: {e}")

    # 2. Создаем контекст запроса для передачи в навыки
    context = RequestContext(
        raw_text=text,
        intent=intent,
        confidence=confidence,
        slots=slots,
        speak=speak_callback
    )

    # 3. Перебираем навыки в порядке их приоритета (определенного в skills/__init__.py)
    handled = False
    for skill in ALL_SKILLS:
        try:
            if skill.can_handle(context):
                logging.info(f"[Маршрутизатор] Навык {skill.__class__.__name__} взял команду в обработку.")
                skill.execute(context)
                handled = True
                break
        except Exception as e:
            logging.error(f"[Маршрутизатор] Ошибка при выполнении навыка {skill.__class__.__name__}: {e}")

    # 4. Фоллбек (запасной вариант), если ни один навык не перехватил команду
    if not handled:
        logging.info("[Маршрутизатор] Ни один навык не смог обработать команду.")
        speak_callback("Извините, я не понял эту команду.")
