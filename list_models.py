#!/usr/bin/env python3
# list_models.py — список моделей OpenRouter (или другого OpenAI-compatible base_url).

from __future__ import annotations

import argparse
import os
import sys

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

DEFAULT_BASE = "https://openrouter.ai/api/v1"


def _env(name: str) -> str:
    return (os.getenv(name) or "").strip().strip("\"'")


def main() -> int:
    parser = argparse.ArgumentParser(description="Список моделей OpenRouter / OpenAI-compatible API")
    parser.add_argument(
        "--all",
        action="store_true",
        help="Показать все модели, не только бесплатные (:free)",
    )
    parser.add_argument(
        "--filter",
        default="",
        help="Подстрока в id модели (например qwen, llama, deepseek)",
    )
    args = parser.parse_args()

    api_key = _env("OPENAI_API_KEY")
    base_url = _env("OPENAI_API_BASE") or DEFAULT_BASE
    current = _env("OPENAI_MODEL") or _env("GROQ_MODEL")

    if not api_key:
        print("OPENAI_API_KEY не найден в .env", file=sys.stderr)
        return 1

    client = OpenAI(api_key=api_key, base_url=base_url)
    try:
        page = client.models.list()
    except Exception as exc:
        print(f"Не удалось получить список моделей: {exc}", file=sys.stderr)
        return 1

    ids = sorted({m.id for m in page.data if getattr(m, "id", None)}, key=str.lower)
    needle = (args.filter or "").strip().lower()
    if needle:
        ids = [mid for mid in ids if needle in mid.lower()]

    free_ids = [mid for mid in ids if mid.endswith(":free") or ":free" in mid]
    show = ids if args.all else free_ids

    print(f"base_url: {base_url}")
    if current:
        print(f"сейчас в .env: {current}")
    print(f"всего с API: {len(ids)}" + (f" (фильтр «{args.filter}»)" if needle else ""))
    if not args.all:
        print(f"бесплатных (:free): {len(free_ids)}  — полный список: python list_models.py --all")
    print()

    if not show:
        print("Ничего не найдено.")
        return 0

    label = "Все модели" if args.all else "Бесплатные модели"
    print(f"=== {label} ===")
    for mid in show:
        mark = "  ← текущая" if current and mid == current else ""
        print(f"- {mid}{mark}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
