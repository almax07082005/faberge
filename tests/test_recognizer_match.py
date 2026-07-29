"""Юнит-тесты сшивки названий ML-индекса с каталогом (баг-репорт 28.07.2026, п.1).

Именно здесь ломалось распознавание: предсказание с названием, отличающимся от
названия в БД регистром, «ё/е», кавычками или двойным пробелом, молча
выбрасывалось — и ответ всегда был «не удалось распознать» при исправной модели.

Нужны зависимости из requirements.txt (pydantic-settings, httpx). Запуск:
    python -m pytest tests/test_recognizer_match.py
    python tests/test_recognizer_match.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.recognizer import (  # noqa: E402
    build_name_index,
    match_title,
    normalize_name,
)

CATALOG = {
    "Яйцо «Ландыши»": "faberge_egg_lilies",
    "Яйцо «Коронационное»": "faberge_egg_coronation",
    "Портсигар": "cigarette_case_1",
    "Часы «Шантеклер»": "clock_chanticleer",
}
INDEX = build_name_index(CATALOG)


def test_normalize_name():
    assert normalize_name("Яйцо «Ландыши»") == normalize_name("яйцо ландыши")
    assert normalize_name("Пётр") == normalize_name("Петр")          # ё → е
    assert normalize_name("Яйцо  Ландыши ") == "яйцо ландыши"        # двойные пробелы, trim
    assert normalize_name('Часы "Шантеклер".') == "часы шантеклер"   # кавычки и точка


def test_match_exact_after_normalization():
    """Расхождения регистра / кавычек / «ё» больше не выбрасывают предсказание."""
    for title in ("Яйцо «Ландыши»", "яйцо ландыши", 'ЯЙЦО "ЛАНДЫШИ"', " Яйцо  Ландыши "):
        slug, how = match_title(title, INDEX)
        assert slug == "faberge_egg_lilies", title
        assert how == "exact", title


def test_match_fuzzy():
    """Мелкая опечатка/расхождение в написании — нечёткое сопоставление."""
    slug, how = match_title("Яйцо «Коронацинное»", INDEX)
    assert slug == "faberge_egg_coronation"
    assert how == "fuzzy"


def test_match_miss():
    """Совсем чужое название не должно «примагничиваться» к случайному экспонату."""
    slug, how = match_title("Автомобиль Руссо-Балт", INDEX)
    assert slug is None
    assert how == "miss"
    assert match_title("", INDEX) == (None, "miss")


def test_match_fuzzy_disabled():
    assert match_title("Яйцо «Коронацинное»", INDEX, cutoff=0)[0] is None


def test_build_name_index_skips_empty():
    index = build_name_index({"": "empty", "Портсигар": "cigarette_case_1"})
    assert "" not in index
    assert index["портсигар"] == "cigarette_case_1"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {fn.__name__}: {exc!r}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
