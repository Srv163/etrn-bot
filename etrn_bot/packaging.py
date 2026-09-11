"""
Коды вида тары (реквизит @ВидТар формата ЭТрН).

Реквизит обязательный и двухсимвольный, а бухгалтер таких кодов не помнит.
Поэтому: бот ищет код по человеческому описанию упаковки («паллеты», «мешки»),
показывает варианты и ждёт выбора — но никогда не подставляет код молча.
Классификатор лежит в `data/packaging_codes.json`, источник — в `data/README.md`.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

DATA = Path(__file__).parent / "data" / "packaging_codes.json"

# Как бухгалтерия называет упаковку → слова, по которым искать в классификаторе.
# Нужно потому, что в классификаторе «поддон», а в заявках пишут «паллеты».
_SYNONYMS = {
    "паллет": "поддон",
    "палет": "поддон",
    "европаллет": "поддон",
    "евро": "поддон",
    "биг-бэг": "мешок большой",
    "бигбэг": "мешок большой",
    "big-bag": "мешок большой",
    "навал": "навалом",
    "насып": "навалом",
    "налив": "наливом",
    "цистерн": "наливом",
    "без упаковки": "неупакованный",
    "картон": "коробка, картонная",
}


@lru_cache(maxsize=1)
def codes() -> dict[str, str]:
    """код → официальное наименование."""
    return json.loads(DATA.read_text(encoding="utf-8"))


def is_valid(code: object) -> bool:
    return isinstance(code, str) and code.upper() in codes()


def title(code: object) -> str | None:
    return codes().get(str(code).upper())


def _same_word(a: str, b: str) -> bool:
    """
    Похожи ли слова с точностью до окончания: «мешки»/«мешок», «ящики»/«ящик».
    Русская морфология с беглой гласной («мешок» → «мешки») не даёт обрезать
    слово по фиксированной длине, поэтому сравниваем общий префикс с порогом,
    зависящим от длины слов. Без порога «непонятное» находило «из прочего».
    """
    common = 0
    for ca, cb in zip(a, b):
        if ca != cb:
            break
        common += 1
    return common >= max(3, min(len(a), len(b)) - 2)


def search(text: str, limit: int = 5) -> list[tuple[str, str]]:
    """
    Подобрать коды по описанию упаковки. Возвращает [(код, наименование)],
    самое точное — первым. Пустой список означает «не нашёл», а не «подставь
    что-нибудь».
    """
    query = (text or "").strip().lower()
    if not query:
        return []
    for word, replacement in _SYNONYMS.items():
        if word in query:
            query = replacement
            break

    words = [w for w in re.findall(r"[а-яёa-z]+", query) if len(w) > 2]
    if not words:
        return []

    scored: list[tuple[int, str, str]] = []
    for code, name in codes().items():
        low = name.lower()
        if low == query:
            score = 100
        elif low.startswith(query):
            score = 60
        else:
            name_words = re.findall(r"[а-яёa-z]+", low)
            hits = sum(1 for w in words
                       if any(_same_word(w, nw) for nw in name_words))
            score = 10 * hits
            # первое слово названия — родовое («Мешок», «Ящик»); совпадение
            # с ним значимее, чем с уточнением в конце
            if hits and name_words and _same_word(words[0], name_words[0]):
                score += 8
            if hits and len(low) < 30:
                score += 3
        if score:
            scored.append((score, code, name))
    scored.sort(key=lambda x: (-x[0], len(x[2]), x[1]))
    return [(code, name) for _score, code, name in scored[:limit]]


def hint(text: str) -> str:
    """Подсказка для вопроса человеку: пара вариантов кода по описанию."""
    found = search(text)
    if not found:
        return ("Код из классификатора видов упаковки (решение КТС № 378), "
                "ровно два символа. Например: PX — поддон, BG — мешок, "
                "CT — коробка картонная, NE — без упаковки.")
    return "Похоже на: " + ", ".join(f"{code} — {name}" for code, name in found)
