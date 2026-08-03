"""Юнит-тесты смысловой кластеризации вопросов (§3 ТЗ 03.08.2026).

Чистые функции, БД не нужна. Запуск:
    python -m pytest tests/test_question_cluster.py    # если установлен pytest
    python tests/test_question_cluster.py              # standalone (без зависимостей)
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services import question_cluster as qc


def _cluster_of(clusters, needle: str):
    """Кластер, в который попала формулировка `needle`."""
    for cluster in clusters:
        if any(needle.lower() in form.lower() for form in cluster.forms):
            return cluster
    raise AssertionError(f"формулировка не найдена ни в одном кластере: {needle}")


def test_normalize():
    assert qc.normalize("  Сколько СТОИТ яйцо?! ") == "сколько стоит яйцо"
    assert qc.normalize("Ёлка, ёж") == "елка еж"


def test_stem_collapses_cases():
    assert qc.stem("яйцо") == qc.stem("яйца") == qc.stem("яйце")
    # Короткие слова не режем до огрызка.
    assert qc.stem("зал") == "зал"


def test_synonyms_map_to_one_lemma():
    assert "цена" in qc.keywords("сколько стоит")
    assert "цена" in qc.keywords("какая стоимость")
    assert "мастер" in qc.keywords("кто автор")
    assert "мастер" in qc.keywords("кто мастер")


def test_stopwords_dropped():
    # Служебные слова в ключ не попадают, значимые остаются.
    assert qc.keywords("расскажи мне про это") == frozenset()


def test_paraphrases_land_in_one_cluster():
    """DoD §3: три разные формулировки одного вопроса — один кластер."""
    rows = [("Сколько стоит яйцо?", 1), ("какая цена яйца", 1), ("Сколько это стоит", 1)]
    clusters = qc.cluster_questions(rows)
    assert len(clusters) == 1
    assert clusters[0].count == 3
    assert len(clusters[0].variants) == 2


def test_transitive_merge_does_not_depend_on_order():
    """«какая цена яйца» и «Сколько это стоит» напрямую не похожи…

    …но обе похожи на «сколько стоит яйцо» и должны оказаться в одной группе
    независимо от того, какая формулировка обрабатывается первой. Жадное
    подклеивание к первому подходящему кластеру этот случай теряло.
    """
    rows = [("Сколько это стоит", 1), ("какая цена яйца", 1), ("сколько стоит яйцо", 1)]
    for variant in (rows, list(reversed(rows)), [rows[1], rows[0], rows[2]]):
        clusters = qc.cluster_questions(variant)
        assert len(clusters) == 1, [c.question for c in clusters]
        assert clusters[0].count == 3


def test_different_aspects_stay_apart():
    """Вопросы про цену, автора и местоположение одного предмета не склеиваются."""
    rows = [
        ("сколько стоит яйцо", 3),
        ("кто автор яйца", 2),
        ("где находится яйцо", 2),
    ]
    clusters = qc.cluster_questions(rows)
    assert len(clusters) == 3


def test_representative_is_most_frequent_form():
    rows = [("какая цена яйца", 10), ("Сколько стоит яйцо?", 2)]
    cluster = qc.cluster_questions(rows)[0]
    assert cluster.question == "какая цена яйца"
    assert cluster.variants == ["Сколько стоит яйцо?"]
    assert cluster.count == 12


def test_case_variants_merge():
    rows = [("Кто это?", 3), ("кто это", 2), ("КТО ЭТО?", 1)]
    clusters = qc.cluster_questions(rows)
    assert len(clusters) == 1
    assert clusters[0].count == 6


def test_order_is_deterministic():
    rows = [("сколько стоит яйцо", 1), ("кто автор яйца", 1), ("где находится яйцо", 1)]
    first = [c.question for c in qc.cluster_questions(rows)]
    second = [c.question for c in qc.cluster_questions(list(reversed(rows)))]
    assert first == second


def test_payload_is_summed_per_cluster():
    """Привязки (экспонат, причина отказа) складываются по кластеру."""
    rows = [("сколько стоит яйцо", 2), ("какая цена яйца", 1)]
    payloads = [[("exhibit", 7), ("exhibit", 7)], [("exhibit", 7)]]
    cluster = qc.cluster_questions(rows, payloads)[0]
    assert cluster.payload[("exhibit", 7)] == 3


def test_variants_are_capped():
    rows = [(f"сколько стоит яйцо вариант {i}", 1) for i in range(12)]
    cluster = qc.cluster_questions(rows)[0]
    assert len(cluster.variants) == qc.MAX_VARIANTS


def test_empty_input():
    assert qc.cluster_questions([]) == []


if __name__ == "__main__":
    failures = 0
    for name, func in sorted(globals().items()):
        if name.startswith("test_") and callable(func):
            try:
                func()
                print(f"ok   {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
    print("—" * 40)
    print("все тесты пройдены" if not failures else f"провалено: {failures}")
    sys.exit(1 if failures else 0)
