import json
import logging
import os
from difflib import SequenceMatcher

# Только farewell/coin в intents.json. Приветствия и «как дела» — Groq.
# predict всегда (intent|None, confidence). Совпадение фраз, не линейная модель.

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INTENTS_PATH = os.path.join(BASE_DIR, "intents.json")


class NLUClassifier:
    def __init__(self, intents_path: str = INTENTS_PATH):
        self.intents_path = intents_path
        self.intents = self._load_intents()
        self._examples: list[tuple[str, str]] = []

    def _load_intents(self) -> dict:
        if not os.path.exists(self.intents_path):
            logger.error(f"Файл не найден: {self.intents_path}")
            return {}
        try:
            with open(self.intents_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Ошибка чтения {self.intents_path}: {e}")
            return {}

    def train(self) -> bool:
        self._examples = []
        if not self.intents:
            return False
        for intent_name, intent_data in self.intents.items():
            for example in intent_data.get("examples", []):
                cleaned = example.lower().strip()
                if cleaned:
                    self._examples.append((cleaned, intent_name))
        if not self._examples:
            return False
        logger.info("[NLU] Фразы прощания и монетки загружены.")
        return True

    def predict(self, text: str):
        cleaned = (text or "").lower().strip()
        if not cleaned or not self._examples:
            return None, 0.0

        best_intent = None
        best = 0.0
        for example, intent_name in self._examples:
            if cleaned == example:
                return intent_name, 1.0
            ratio = SequenceMatcher(None, cleaned, example).ratio()
            if ratio > best:
                best = ratio
                best_intent = intent_name
        if best < 0.5:
            return None, float(best)
        return best_intent, float(best)
