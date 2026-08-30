# skills/local_nlu.py

import os
import random
import logging
import nlu
from skills.base import BaseSkill, RequestContext

class LocalNLUSkill(BaseSkill):
    """Навык для обработки статических запросов через локальную модель NLU."""
    
    def __init__(self):
        self.nlu_engine = nlu.NLUClassifier()
        self.nlu_engine.train()

    def can_handle(self, context: RequestContext) -> bool:
        # Навык берется за обработку, только если точность распознавания интента выше 85%
        return context.confidence > 0.85

    def execute(self, context: RequestContext) -> None:
        intent = context.intent
        logging.info(f"[NLU Интент]: '{intent}' ({context.confidence:.2f})")
        
        responses = self.nlu_engine.intents.get(intent, {}).get("responses", [])
        if not responses:
            context.speak("Извините, не знаю, как на это ответить.")
            return

        if intent == "farewell":
            if any(w in context.raw_text for w in ["пока", "выход", "прощай", "отключись", "до свидания"]):
                context.speak(random.choice(responses))
                os._exit(0)

        context.speak(random.choice(responses))
