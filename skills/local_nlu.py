# skills/local_nlu.py

import random
import logging
from difflib import get_close_matches

import nlu
from skills.base import BaseSkill, RequestContext


class LocalNLUSkill(BaseSkill):
    """Навык для обработки статических запросов через локальную модель NLU."""

    def __init__(self):
        self.nlu_engine = nlu.NLUClassifier()
        self.nlu_engine.train()

    def can_handle(self, context: RequestContext) -> bool:
        if not context.intent or context.intent not in self.nlu_engine.intents:
            return False
        examples = [
            example.lower().strip()
            for example in self.nlu_engine.intents[context.intent].get("examples", [])
            if example.strip()
        ]
        if not examples:
            return False
        text = context.raw_text.lower().strip()
        if text in examples:
            return True
        return context.confidence >= 0.65 and bool(get_close_matches(text, examples, n=1, cutoff=0.55))

    def execute(self, context: RequestContext) -> None:
        intent = context.intent
        logging.info(f"[NLU Интент]: '{intent}' ({context.confidence:.2f})")

        responses = self.nlu_engine.intents.get(intent, {}).get("responses", [])
        if not responses:
            context.speak("Извините, не знаю, как на это ответить.")
            return

        context.speak(random.choice(responses))
        if intent == "farewell":
            context.should_sleep = True
