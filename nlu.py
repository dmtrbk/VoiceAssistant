import json
import logging
import os
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INTENTS_PATH = os.path.join(BASE_DIR, "intents.json")

class NLUClassifier:
    def __init__(self, intents_path: str = INTENTS_PATH):
        self.intents_path = intents_path
        self.intents = self._load_intents()
        self.vectorizer = None
        self.classifier = None

    def _load_intents(self) -> dict:
        if not os.path.exists(self.intents_path):
            logging.error(f"Файл не найден: {self.intents_path}")
            return {}
        try:
            with open(self.intents_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logging.error(f"Ошибка чтения {self.intents_path}: {e}")
            return {}

    def train(self) -> bool:
        if not self.intents:
            return False

        X, y = [], []
        for intent_name, intent_data in self.intents.items():
            for example in intent_data.get("examples", []):
                cleaned = example.lower().strip()
                if cleaned:
                    X.append(cleaned)
                    y.append(intent_name)

        if not X:
            return False

        # Используем символ-буквенный анализ (лучшее решение для русского языка)
        self.vectorizer = TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 4))
        X_vectorized = self.vectorizer.fit_transform(X)

        self.classifier = LogisticRegression(C=10.0, max_iter=500, random_state=42)
        self.classifier.fit(X_vectorized, y)
        logging.info("[NLU] Модель распознавания успешно обучена.")
        return True

    def predict(self, text: str):
        if not self.vectorizer or not self.classifier:
            return None, 0.0

        cleaned = text.lower().strip()
        vec = self.vectorizer.transform([cleaned])
        probs = self.classifier.predict_proba(vec)
        max_idx = probs.argmax()
        confidence = probs[0][max_idx]
        predicted_intent = self.classifier.classes_[max_idx]

        return predicted_intent, confidence

# Функция обратной совместимости
def train_nlu_model():
    nlu = NLUClassifier()
    if nlu.train():
        return nlu.vectorizer, nlu.classifier
    return None, None

INTENTS = NLUClassifier().intents