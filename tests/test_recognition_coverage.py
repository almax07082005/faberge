"""Юнит-тесты замера покрытия каталога классами распознавания (вопрос музея 31.08.2026, п. III).

Проверяется чистое ядро ``scripts/recognition_coverage.py`` — то самое, которым посчитаны
цифры в ``docs/task-2026-08-31-recognition-coverage.md``. Раз цифры уезжают в переписку с
музеем, арифметика под ними обязана быть закреплена тестом, а не «я запускал, было верно».

Сети и БД тесты не требуют: снимок каталога — обычные словари той же формы, что отдаёт
публичный ``GET /exhibits``, а разбор сида идёт по строке в памяти.

    python -m pytest tests/test_recognition_coverage.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import recognition_coverage as rc  # noqa: E402

from app.services import recognizer  # noqa: E402


# ── Сборка снимка в форме публичного API ────────────────────────────────────────────────────
def hall(hall_id: int, number, name: str) -> dict:
    return {"id": hall_id, "hall_number": number, "name": name}


def exhibit(ex_id: int, name: str, hall_id, slug=None, showcase=None) -> dict:
    """Карточка в «плоской» форме ExhibitSummary (hall_id/showcase_number рядом с полями)."""
    return {
        "id": ex_id, "name": name, "label_slug": slug,
        "hall_id": hall_id, "showcase_number": showcase,
    }


SNAPSHOT = {
    "source": "test",
    "halls": [hall(1, 4, "Синяя гостиная"), hall(2, 12, "Бежевый зал"), hall(3, 2, "Рыцарский зал")],
    "exhibits": [
        exhibit(10, "Яйцо «Курочка»", 1, slug="kurochka", showcase=12),
        exhibit(11, "Яйцо «Курочка»", 1, slug=None, showcase=1),
        exhibit(12, "Подвески", 1, slug="podveski", showcase=11),
        exhibit(20, "Флакон", 2, slug=None, showcase=1),
        exhibit(21, "Ваза", None, slug=None),          # карточка вне витрин → вне залов
    ],
}


# ── Агрегация ───────────────────────────────────────────────────────────────────────────────
def test_totals_equal_sum_of_halls():
    """Итог считается по тем же строкам, что показаны музею: иначе таблица не сходится сама с собой."""
    cov = rc.build_coverage(SNAPSHOT)
    assert cov.exhibits == sum(r.exhibits for r in cov.rows) == 5
    assert cov.with_label_slug == sum(r.with_label_slug for r in cov.rows) == 2
    assert cov.missing == 3
    assert cov.coverage_rate == 0.4


def test_hall_row_math():
    by_number = {r.hall_number: r for r in rc.build_coverage(SNAPSHOT).rows}
    blue = by_number[4]
    assert (blue.exhibits, blue.with_label_slug, blue.missing) == (3, 2, 1)
    assert blue.coverage_rate == round(2 / 3, 4)


def test_empty_hall_is_zero_not_full():
    """Зал без экспонатов — 0 %, а не «100 %, всё покрыто»: делить не на что, и хвалиться нечем."""
    knights = {r.hall_number: r for r in rc.build_coverage(SNAPSHOT).rows}[2]
    assert (knights.exhibits, knights.with_label_slug, knights.coverage_rate) == (0, 0, 0.0)


def test_exhibit_without_hall_is_kept_and_goes_last():
    """Карточка без витрины не выпадает из знаменателя: распознавание её тоже возвращает."""
    rows = rc.build_coverage(SNAPSHOT).rows
    assert rows[-1].hall_id is None
    assert rows[-1].exhibits == 1
    assert sum(r.exhibits for r in rows) == len(SNAPSHOT["exhibits"])


def test_rows_ordered_by_hall_number():
    """Порядок — музейный (номер зала), а не по id: на проде id 14 — это зал № 8."""
    rows = rc.build_coverage(SNAPSHOT).rows
    assert [r.hall_number for r in rows] == [2, 4, 12, None]


def test_location_object_is_read_too():
    """С 31.08.2026 расположение приходит объектом `location` — снимки «до» и «после» релиза
    должны считаться одинаково, иначе сравнение замеров во времени бессмысленно."""
    nested = {
        "halls": [hall(1, 4, "Синяя гостиная")],
        "exhibits": [{
            "id": 1, "name": "Яйцо", "label_slug": "egg",
            "location": {"hall_id": 1, "hall_number": 4, "hall_name": "Синяя гостиная",
                         "showcase_number": 3},
        }],
    }
    cov = rc.build_coverage(nested)
    assert (cov.exhibits, cov.with_label_slug) == (1, 1)
    assert cov.rows[0].hall_number == 4


# ── Списки для музея ────────────────────────────────────────────────────────────────────────
def test_exhibits_without_slug_filtered_by_hall():
    """Поимённый список — это план съёмки по залу; карточки со слагом в него попадать не должны."""
    rows = rc.exhibits_without_slug(SNAPSHOT, hall_id=1)
    assert [e["id"] for e in rows] == [11]
    assert all(not e["label_slug"] for e in rc.exhibits_without_slug(SNAPSHOT, hall_id=None))


def test_find_exhibits_ignores_case_and_yo():
    assert [e["id"] for e in rc.find_exhibits(SNAPSHOT, "курочка")] == [10, 11]


def test_slug_index_takes_first_by_id():
    """Повторяет `crud.slug_by_name`: среди одноимённых берётся МЕНЬШИЙ id. Считать иначе —
    показывать музею не ту карточку, на которую его уводит прод."""
    index = rc.slug_index(SNAPSHOT)
    assert index[recognizer.normalize_name("Яйцо «Курочка»")]["id"] == 10


def test_name_collisions_point_to_the_slugged_card():
    """Снимок бесслаговой карточки уводит на одноимённую со слагом — это и показывает --collisions."""
    pairs = rc.name_collisions(SNAPSHOT)
    assert [(victim["id"], target["id"]) for victim, target in pairs] == [(11, 10)]


# ── Разбор сид-файлов ───────────────────────────────────────────────────────────────────────
SEED_ONE_LINE_HEADER = """
INSERT INTO halls (id, hall_number, name, description, level) VALUES
 (4, 4, 'Синяя гостиная', 'Императорские пасхальные яйца', 2)
ON CONFLICT (id) DO NOTHING;

INSERT INTO showcases (id, hall_id, showcase_number, name) VALUES
 (1, 4, 1, 'Пасхальные шедевры Фаберже')
ON CONFLICT (id) DO NOTHING;

INSERT INTO exhibits (id, showcase_id, label_slug, name, year_created) VALUES
 (1, 1, 'kurochka', 'Пасхальное яйцо «Курочка»', '1885'),
 (2, 1, NULL, 'Ваза', '1900');
"""

# Тот же смысл, но список колонок перенесён на отдельную строку — так написан db/seed.sql.
# На этом формате разбор раньше падал с `int('id')`, поэтому формат закреплён тестом.
SEED_WRAPPED_HEADER = """
INSERT INTO halls
 (id, hall_number, name, description, level) VALUES
 (4, 4, 'Синяя гостиная',
   'Императорские пасхальные яйца', 2);

INSERT INTO showcases
 (id, hall_id, showcase_number, name) VALUES
 (1, 4, 1, 'Пасхальные шедевры Фаберже');

INSERT INTO exhibits
 (id, showcase_id, label_slug, name, year_created) VALUES
 (1, 1, 'kurochka', 'Пасхальное яйцо «Курочка»',
   '1885'),
 (2, 1, NULL, 'Ваза', '1900');
"""


def _seed_numbers(text: str):
    cov = rc.build_coverage(rc.load_seed(text))
    return cov.exhibits, cov.with_label_slug, [(r.hall_number, r.exhibits) for r in cov.rows]


def test_seed_parses_both_insert_formats():
    """Оба сида репозитория должны читаться одинаково: иначе «замер без сети» врёт молча."""
    assert _seed_numbers(SEED_ONE_LINE_HEADER) == _seed_numbers(SEED_WRAPPED_HEADER)
    assert _seed_numbers(SEED_ONE_LINE_HEADER)[:2] == (2, 1)


def test_seed_column_header_is_not_counted_as_exhibit():
    """Строка со списком колонок выглядит как кортеж — но экспонатом быть не должна."""
    snapshot = rc.load_seed(SEED_WRAPPED_HEADER)
    assert [e["id"] for e in snapshot["exhibits"]] == [1, 2]
    assert all(isinstance(h["id"], int) for h in snapshot["halls"])


def test_repo_seeds_are_readable():
    """Обе фикстуры репозитория разбираются без исключения — команда из документа исполнима.

    Цифры здесь СПЕЦИАЛЬНО не закрепляются: сид — фикстура наливки, её правят вместе с
    контентом, и падающий на этом тест ничего не защищал бы.
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for name in ("db/seed.sql", "db/seed_fabergemuseum.sql"):
        with open(os.path.join(root, name), encoding="utf-8") as fh:
            cov = rc.build_coverage(rc.load_seed(fh.read()))
        assert cov.exhibits > 0, name


# ── Инвариант, на котором держится весь ответ музею ──────────────────────────────────────────
def test_stub_returns_only_known_classes():
    """Распознавание отдаёт ТОЛЬКО классы из белого списка каталога.

    Белый список собирает `crud.all_label_slugs` с `where(label_slug is not null)`, то есть
    карточка без `label_slug` в него не попадает — а значит не может быть возвращена
    посетителю ни при какой съёмке. Проверяем на стабе, потому что это единственная ветка
    распознавания, работающая без сети; SQL-фильтр проверить без БД нельзя, он закреплён
    комментарием в `scripts/fix_showcase_orphans.py` («label_slug не трогаем»).
    """
    known = ["a", "b", "c"]
    for payload in (b"one", b"two", b"three", b"four"):
        outcome = recognizer._recognize_stub(payload, known, top_k=3)
        assert outcome.label_slug in (None, *known)
        assert all(slug in known for slug, _ in outcome.candidates)


def test_no_candidates_when_catalog_has_no_classes():
    """Каталог без единого класса — распознавать нечего: пустой ответ, а не выдумка."""
    outcome = recognizer._recognize_stub(b"photo", [], top_k=3)
    assert outcome.recognized is False
    assert outcome.candidates == []
