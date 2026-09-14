# list_models.py
# Печатает каталог Groq с подписями. Рисовать картинки Groq не умеет.

from __future__ import annotations

import os
import sys

from dotenv import load_dotenv

from skills.groq_client import format_groq_models_report


def fetch_model_ids(api_key: str) -> list[str]:
    from groq import Groq

    client = Groq(api_key=api_key)
    models_list = client.models.list()
    return [model.id for model in models_list.data]


def main(argv: list[str] | None = None) -> int:
    del argv
    load_dotenv()
    api_key = os.getenv("GROQ_API_KEY", "").strip().strip("\"'")
    if not api_key:
        print("❌ Ошибка: API-ключ не найден в файле .env.")
        return 1

    try:
        model_ids = fetch_model_ids(api_key)
    except Exception as exc:
        print(f"❌ Не удалось получить список моделей: {exc}")
        return 1

    print(format_groq_models_report(model_ids))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
