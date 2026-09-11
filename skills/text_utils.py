# skills/text_utils.py
# Быстрый стеммер русского языка (алгоритм Snowball / Портера) и нечёткое сопоставление (Fuzzy matching)
# Без внешних тяжёлых зависимостей.

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Iterable, Sequence

# Гласные русского языка
_VOWELS = set("аеёиоуыэюя")

# Регулярные выражения для алгоритма стемминга Snowball для русского языка
_PERFECTIVEGROUND = re.compile(
    r"((ив|ивши|ившись|ыв|ывши|ывшись)|((?<=[ая])(в|вши|вшись)))$"
)
_REFLEXIVE = re.compile(r"(ся|сь)$")
_ADJECTIVE = re.compile(
    r"(ее|ие|ые|ое|ими|ыми|ей|ий|ый|ой|ем|им|ым|ом|его|ого|ему|ому|их|ых|ую|юю|ая|яя|ою|ею)$"
)
_PARTICIPLE = re.compile(r"(((?<=[ая])(ем|нн|вш|ющ|щ))|(ивш|ывш|ующ))$")
_VERB = re.compile(
    r"((?<=[ая])(ла|на|ете|йте|ли|й|л|ем|н|ло|но|ет|ют|ны|ть|ешь|нно))|"
    r"(ила|ыла|ена|ейте|уйте|ите|или|ыли|ей|уй|ил|ыл|им|ым|ен|ило|ыло|ено|ят|ует|уют|ит|ыт|ены|ить|ыть|ишь|ую|ю)$"
)
_NOUN = re.compile(
    r"(а|ев|ов|ие|ье|е|иях|ия|ях|ах|ию|ью|ю|иям|ьям|ям|ием|ем|ам|ом|о|у|ах|и|ей|ий|ой|ь|ы|у|й|ю|я|он|ок)$"
)
_SUPERLATIVE = re.compile(r"(ейш|ейше)$")
_DERIVATIONAL = re.compile(r"(ост|ость)$")
_I = re.compile(r"и$")
_NN = re.compile(r"нн$")


def stem_russian_word(word: str) -> str:
    """
    Возвращает основу русского слова по алгоритму Snowball (Porter Stemmer).
    Работает в нижнем регистре, ё заменяется на е.
    """
    word = word.lower().replace("ё", "е").strip()
    if not word or len(word) <= 2:
        return word

    # Находим RV (область после первой гласной)
    vowel_indices = [i for i, ch in enumerate(word) if ch in _VOWELS]
    if not vowel_indices:
        return word

    rv_start = vowel_indices[0] + 1
    rv = word[rv_start:]
    head = word[:rv_start]

    if not rv:
        return word

    # Шаг 1: Perfective gerund или Reflexive + Adjective/Participle/Verb/Noun
    m = _PERFECTIVEGROUND.search(rv)
    if m:
        rv = _PERFECTIVEGROUND.sub("", rv, count=1)
    else:
        rv = _REFLEXIVE.sub("", rv, count=1)
        m_adj = _ADJECTIVE.search(rv)
        if m_adj:
            rv = _ADJECTIVE.sub("", rv, count=1)
            rv = _PARTICIPLE.sub("", rv, count=1)
        else:
            m_verb = _VERB.search(rv)
            if m_verb:
                rv = _VERB.sub("", rv, count=1)
            else:
                rv = _NOUN.sub("", rv, count=1)

    # Шаг 2: Удаление 'и'
    rv = _I.sub("", rv, count=1)

    # Шаг 3: Derivational
    m_der = _DERIVATIONAL.search(rv)
    if m_der:
        rv = _DERIVATIONAL.sub("", rv, count=1)

    # Шаг 4: Superlative / 'нн' / 'ь'
    m_sup = _SUPERLATIVE.search(rv)
    if m_sup:
        rv = _SUPERLATIVE.sub("", rv, count=1)
        rv = _NN.sub("н", rv, count=1)
    else:
        rv = _SUPERLATIVE.sub("", rv, count=1)
        rv = _NN.sub("н", rv, count=1)
        if rv.endswith("ь"):
            rv = rv[:-1]

    return head + rv


def get_stems(text: str) -> list[str]:
    """Разбивает текст на слова и возвращает список основ (стем)."""
    words = re.findall(r"[А-Яа-яA-Za-z0-9]+", text.lower().replace("ё", "е"))
    return [stem_russian_word(w) for w in words if w]


def token_similarity(a: str, b: str) -> float:
    """Сходство двух токенов от 0.0 до 1.0 (с учётом основ и опечаток)."""
    a = a.lower().replace("ё", "е").strip()
    b = b.lower().replace("ё", "е").strip()
    if a == b:
        return 1.0
    if not a or not b:
        return 0.0

    stem_a = stem_russian_word(a)
    stem_b = stem_russian_word(b)
    if stem_a == stem_b:
        return 1.0
    if (len(stem_a) >= 3 and len(stem_b) >= 3) and (stem_a.startswith(stem_b) or stem_b.startswith(stem_a)):
        return 0.95

    return SequenceMatcher(None, a, b).ratio()


def fuzzy_phrase_match(text: str, target: str, min_ratio: float = 0.75) -> bool:
    """
    Проверяет, содержится ли целевая фраза/слово `target` в `text`
    с учётом склонений, окончаний и возможных опечаток STT.
    """
    text_stems = get_stems(text)
    target_stems = get_stems(target)

    if not text_stems or not target_stems:
        return False

    # Если target состоит из одного слова
    if len(target_stems) == 1:
        t_stem = target_stems[0]
        for w_stem in text_stems:
            if w_stem == t_stem:
                return True
            if SequenceMatcher(None, w_stem, t_stem).ratio() >= min_ratio:
                return True
        return False

    # Если target состоит из нескольких слов (окно по размеру фразы)
    k = len(target_stems)
    for i in range(len(text_stems) - k + 1):
        window = text_stems[i : i + k]
        matches = 0
        for w_stem, t_stem in zip(window, target_stems):
            if w_stem == t_stem or SequenceMatcher(None, w_stem, t_stem).ratio() >= min_ratio:
                matches += 1
        if matches == k:
            return True

    # Fallback на глобальное подобие
    return SequenceMatcher(None, " ".join(text_stems), " ".join(target_stems)).ratio() >= min_ratio


def extract_fuzzy_match(text: str, candidates: Iterable[str], min_ratio: float = 0.72) -> str | None:
    """
    Ищет лучший вариант из списка `candidates` в тексте `text`.
    Возвращает исходную строку кандидата или None.
    """
    best_candidate = None
    best_score = 0.0
    text_stems = get_stems(text)

    for cand in candidates:
        cand_stems = get_stems(cand)
        if not cand_stems:
            continue

        # Проверка прямого совпадения стем
        cand_len = len(cand_stems)
        cand_score = 0.0

        if cand_len == 1:
            t_stem = cand_stems[0]
            for w_stem in text_stems:
                if w_stem == t_stem:
                    cand_score = max(cand_score, 1.0)
                else:
                    ratio = SequenceMatcher(None, w_stem, t_stem).ratio()
                    if ratio >= min_ratio:
                        cand_score = max(cand_score, ratio)
        else:
            for i in range(len(text_stems) - cand_len + 1):
                window = text_stems[i : i + cand_len]
                ratios = [
                    1.0 if w == t else SequenceMatcher(None, w, t).ratio()
                    for w, t in zip(window, cand_stems)
                ]
                avg_ratio = sum(ratios) / len(ratios)
                if min(ratios) >= (min_ratio - 0.1) and avg_ratio >= min_ratio:
                    cand_score = max(cand_score, avg_ratio)

        if cand_score > best_score:
            best_score = cand_score
            best_candidate = cand

    return best_candidate if best_score >= min_ratio else None
