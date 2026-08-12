"""Юнит-тесты разового бэкфилла структурных полей каталога (баг-репорт 12.08.2026, п.5).

Сам разбор каталожной строки покрыт отдельно (tests/test_catalog_line.py) — здесь проверяется
то, что делает вокруг него скрипт и чем он может испортить прод:

  • дозаполняются ТОЛЬКО пустые поля; непустое не перезаписывается никогда — 12.08 фронт
    применил в эти же поля 38 ручных правок на 34 карточках, и затереть их разбором
    путеводителя 2014 года нельзя;
  • повторный прогон даёт ноль PATCH — это дословный DoD заказчика «повторный прогон импорта
    ничего не ломает»;
  • сухой прогон не пишет вообще ничего;
  • откат возвращает ровно исходные значения, включая NULL;
  • связная музейная проза (20 карточек, напр. id 48 «Царские врата») пропускается целиком;
  • ``--clean-material-techniques`` выносит технику из material, а без ключа не трогает
    непустое поле.

Сети и БД не нужно: сетевой слой скрипта — единственная функция ``api``, её подменяет FakeApi.
Списочная выдача в фейке намеренно НЕ отдаёт short_description/material/techniques — ровно как
ExhibitSummary на проде, иначе тест не поймал бы, что скрипт забыл сходить за карточкой.

Запуск:
    python -m pytest tests/test_backfill_catalog.py
    python tests/test_backfill_catalog.py     # standalone
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import backfill_catalog_fields as backfill  # noqa: E402

# Реальные строки с прода: A1 (74 % каталога), A2 (без мастера) и вековая датировка,
# у которой year_created обязан остаться пустым.
LINE_A1 = ("Санкт-Петербург, 1899–1903. Фирма К. Фаберже, мастер М. Перхин. "
           "Золото, серебро, сталь, сапфир; штамп, чеканка, гравировка, золочение")
LINE_A2 = ("Санкт-Петербург, 1908−1917. Серебро; выемчатая эмаль, живопись по эмали, "
           "токарно-давильные работы")
LINE_CENTURY = "Москва, вторая половина XVI века. Дерево, левкас, темпера"
LINE_PROSE = ("Редким и ценным музейным предметом являются царские врата рубежа XVI–XVII веков. "
              "На них, как обычно, представлено «Благовещение». Поскольку врата были важным "
              "элементом иконостаса, их дополнительно украсили серебряной басмой.")


# ── Фейковый каталог ────────────────────────────────────────────────────────────────────────
class FakeApi:
    """Каталог в памяти: залы → экспонаты. Умеет ровно те ручки, что дёргает бэкфилл."""

    # Поля ExhibitSummary: описания, материалов и техник в списочной выдаче НЕТ.
    SUMMARY = ("id", "exhibit_number", "name", "year_created", "dating", "master_name",
               "hall_id", "showcase_id", "showcase_number")

    def __init__(self, halls, exhibits) -> None:
        self.halls = {h["id"]: dict(h) for h in halls}
        self.exhibits = {e["id"]: dict(e) for e in exhibits}
        self.calls: list = []

    @staticmethod
    def _page(items, query):
        params = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
        limit, offset = int(params.get("limit", 100)), int(params.get("offset", 0))
        return 200, {"items": items[offset:offset + limit], "total": len(items)}

    def _detail(self, ex: dict) -> dict:
        hall = self.halls.get(ex.get("hall_id"), {})
        card = dict(ex)
        card["hall"] = {"id": hall.get("id"), "hall_number": hall.get("hall_number"),
                        "name": hall.get("name")}
        return card

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        head, _, query = path.partition("?")
        parts = head.strip("/").split("/")

        if method == "GET" and head == "/halls":
            assert "include_service=true" in query, "служебный зал 1 обязан попасть в объём"
            return self._page(sorted(self.halls.values(), key=lambda h: h["id"]), query)
        if method == "GET" and head.startswith("/halls/") and head.endswith("/exhibits"):
            hall_id = int(parts[1])
            items = [{k: e.get(k) for k in self.SUMMARY}
                     for e in sorted(self.exhibits.values(), key=lambda e: e["id"])
                     if e.get("hall_id") == hall_id]
            return self._page(items, query)
        if method == "GET" and head.startswith("/admin/exhibits/"):
            ex = self.exhibits.get(int(parts[-1]))
            return (200, self._detail(ex)) if ex else (404, {"detail": "Экспонат не найден."})
        if method == "PATCH" and head.startswith("/admin/exhibits/"):
            ex = self.exhibits[int(parts[-1])]
            assert "short_description" not in body, "short_description — источник разбора, его не трогаем"
            ex.update(body)
            return 200, self._detail(ex)
        raise AssertionError(f"фейковый API не знает {method} {path}")

    def patches(self) -> list:
        return [(path, body) for method, path, body in self.calls if method == "PATCH"]

    def snapshot(self) -> dict:
        """Значения полей бэкфилла по всем карточкам — чтобы сравнить «до» и «после»."""
        return {
            ex["id"]: {name: ex.get(name) for name in backfill.FIELD_TITLES}
            for ex in self.exhibits.values()
        }


def card(ex_id: int, name: str, line=None, hall_id: int = 6, **fields) -> dict:
    base = {
        "id": ex_id, "name": name, "hall_id": hall_id, "exhibit_number": None,
        "showcase_id": 20, "showcase_number": 1, "short_description": line,
        "year_created": None, "dating": None, "master_name": None,
        "material": None, "techniques": None,
    }
    base.update(fields)
    return base


def catalog(*cards, halls=None) -> FakeApi:
    halls = halls or [{"id": 6, "hall_number": 6, "name": "Аванзал", "is_service": False}]
    return FakeApi(halls, list(cards))


def _args(**over) -> argparse.Namespace:
    base = dict(apply=False, clean_material_techniques=False, ids=None, limit=None,
                report_file=None, rollback_file=None, rollback=None, max_print=200)
    base.update(over)
    return argparse.Namespace(**base)


def run_backfill(fake: FakeApi, **over):
    """Прогнать бэкфилл по фейковому каталогу; вернуть (код возврата, напечатанное)."""
    if over.get("apply") and not over.get("rollback_file") and not over.get("rollback"):
        over["rollback_file"] = os.path.join(tempfile.mkdtemp(), "rollback.json")
    saved, buffer = backfill.api, io.StringIO()
    backfill.api = fake
    try:
        with contextlib.redirect_stdout(buffer):
            code = backfill.run(_args(**over))
    finally:
        backfill.api = saved
    return code, buffer.getvalue()


# ── Дозаполнение ────────────────────────────────────────────────────────────────────────────
def test_empty_fields_are_filled_from_the_catalog_line():
    """Основной случай: у карточки пусто всё, каталожная строка лежит в short_description."""
    fake = catalog(card(532, "Пасхальное яйцо", LINE_A1))
    run_backfill(fake, apply=True)
    ex = fake.exhibits[532]
    assert ex["year_created"] == 1899                      # нижняя граница диапазона
    assert ex["dating"] == "1899–1903"                     # датировка дословно как в указателе
    assert ex["master_name"] == "Фирма К. Фаберже, мастер М. Перхин"
    assert ex["material"] == "Золото, Серебро, Сталь, Сапфир"
    assert ex["techniques"] == "штамп, чеканка, гравировка, золочение"
    assert ex["short_description"] == LINE_A1              # источник не тронут


def test_century_dating_leaves_year_empty_but_fills_dating():
    """«вторая половина XVI века» в число не превращаем: 1501 сломал бы сортировку сильнее пустого."""
    fake = catalog(card(1222, "Икона", LINE_CENTURY))
    _, printed = run_backfill(fake, apply=True)
    ex = fake.exhibits[1222]
    assert ex["year_created"] is None
    assert ex["dating"] == "вторая половина XVI века"
    assert ex["material"] == "Дерево, Левкас, Темпера"
    assert ex["techniques"] is None                        # техник в строке нет — поле не трогаем
    assert "век — года нет" in printed                     # видно в распределении точности


def test_missing_maker_segment_is_not_invented():
    """Форма A2 — сегмента мастера нет вовсе. Поле обязано остаться пустым, а не собраться из места."""
    fake = catalog(card(459, "Ковш", LINE_A2))
    run_backfill(fake, apply=True)
    ex = fake.exhibits[459]
    assert ex["master_name"] is None
    assert (ex["year_created"], ex["material"]) == (1908, "Серебро")


def test_only_empty_fields_are_patched():
    """Патч несёт ровно недостающие поля: заполненные в запрос не попадают вовсе."""
    fake = catalog(card(532, "Пасхальное яйцо", LINE_A1,
                        year_created=1901, material="Золото, Серебро"))
    run_backfill(fake, apply=True)
    body = fake.patches()[0][1]
    assert set(body) == {"dating", "master_name", "techniques"}


# ── Непустое не перезаписываем ──────────────────────────────────────────────────────────────
def test_manual_values_are_never_overwritten():
    """Ручные правки музея и фронта разбор указателя не трогает — только показывает расхождение."""
    fake = catalog(card(532, "Пасхальное яйцо", LINE_A1,
                        year_created=1904, master_name="Август Хольмстрём"))
    _, printed = run_backfill(fake, apply=True)
    ex = fake.exhibits[532]
    assert ex["year_created"] == 1904                      # год музея на месте
    assert ex["master_name"] == "Август Хольмстрём"        # мастер музея на месте
    assert "Расходится с каталожной строкой" in printed
    assert "Август Хольмстрём" in printed and "Фирма К. Фаберже" in printed


def test_case_only_difference_is_not_a_conflict():
    """«Золото, серебро» против «Золото, Серебро» — не расхождение, а регистр: в отчёт не идёт."""
    fake = catalog(card(532, "Пасхальное яйцо", LINE_A1, material="золото, серебро, сталь, сапфир"))
    _, printed = run_backfill(fake, apply=True)
    assert fake.exhibits[532]["material"] == "золото, серебро, сталь, сапфир"
    assert "— нет: заполненные поля совпадают с разбором" in printed


def test_blank_string_counts_as_empty():
    """Пробельная строка — это пустое поле: заполнением её считать нельзя."""
    fake = catalog(card(532, "Пасхальное яйцо", LINE_A1, master_name="   "))
    run_backfill(fake, apply=True)
    assert fake.exhibits[532]["master_name"] == "Фирма К. Фаберже, мастер М. Перхин"


# ── Идемпотентность и сухой прогон ──────────────────────────────────────────────────────────
def test_second_run_changes_nothing():
    """DoD заказчика: «повторный прогон импорта ничего не ломает» — второй прогон = ноль PATCH."""
    fake = catalog(card(532, "Пасхальное яйцо", LINE_A1),
                   card(1222, "Икона", LINE_CENTURY),
                   card(48, "Царские врата", LINE_PROSE, material="Серебро, Дерево"))
    run_backfill(fake, apply=True)
    after_first = fake.snapshot()
    patched = len(fake.patches())
    assert patched == 2                                    # проза не патчится

    _, printed = run_backfill(fake, apply=True)
    assert len(fake.patches()) == patched                  # ни одного нового PATCH
    assert fake.snapshot() == after_first
    assert "Правок: 0" in printed


def test_dry_run_sends_no_patch():
    """Сухой прогон по умолчанию: показывает план и не пишет ни байта."""
    fake = catalog(card(532, "Пасхальное яйцо", LINE_A1))
    before = fake.snapshot()
    _, printed = run_backfill(fake)
    assert fake.patches() == []
    assert fake.snapshot() == before
    assert "Это сухой прогон" in printed
    assert "Правок: 5" in printed                          # но план ровно тот, что применится


def test_dry_run_plan_equals_what_apply_writes():
    """План сухого прогона и результат --apply — одно и то же: заказчик смотрит именно на план."""
    cards = (card(532, "Пасхальное яйцо", LINE_A1), card(459, "Ковш", LINE_A2))
    _, dry = run_backfill(catalog(*cards))
    fake = catalog(*cards)
    _, wet = run_backfill(fake, apply=True)
    line = next(ln for ln in dry.splitlines() if ln.startswith("Правок:"))
    assert line in wet
    assert sum(len(body) for _, body in fake.patches()) == int(line.split()[1].rstrip(","))


# ── Проза и прочее, чего трогать нельзя ─────────────────────────────────────────────────────
def test_narrative_card_is_skipped_entirely():
    """Связный музейный очерк — не каталожная строка: карточку не трогаем, id уходит в отчёт."""
    fake = catalog(card(48, "Царские врата", LINE_PROSE))
    before = fake.snapshot()
    _, printed = run_backfill(fake, apply=True)
    assert fake.patches() == []
    assert fake.snapshot() == before
    assert "не каталожная строка" in printed
    assert "id=48" in printed


def test_card_without_description_is_not_a_parse_failure():
    """Пустое описание — не «строка не разобралась»: в статистику разбора карточка не попадает."""
    fake = catalog(card(700, "Витрина без описания", None))
    _, printed = run_backfill(fake, apply=True)
    assert fake.patches() == []
    assert "с каталожной строкой: 0" in printed
    assert "Пропущено — карточку не трогаем вовсе (0)" in printed


# ── Откат ───────────────────────────────────────────────────────────────────────────────────
def test_rollback_restores_original_values():
    """Откат возвращает ровно исходные значения, включая NULL у незаполненных полей."""
    fake = catalog(card(532, "Пасхальное яйцо", LINE_A1, year_created=1904))
    before = fake.snapshot()
    path = os.path.join(tempfile.mkdtemp(), "rollback.json")
    run_backfill(fake, apply=True, rollback_file=path)
    assert fake.snapshot() != before

    run_backfill(fake, rollback=path, apply=True)
    assert fake.snapshot() == before                       # включая master_name = None
    with open(path, encoding="utf-8") as fh:
        assert json.load(fh)["items"][0]["before"]["master_name"] is None


def test_rollback_is_repeatable_and_spares_hand_edits():
    """Повторный откат молчит, а поле, поправленное человеком после прогона, не затирает."""
    fake = catalog(card(532, "Пасхальное яйцо", LINE_A1))
    path = os.path.join(tempfile.mkdtemp(), "rollback.json")
    run_backfill(fake, apply=True, rollback_file=path)
    fake.exhibits[532]["master_name"] = "Мастер М. Перхин"  # музей поправил руками
    _, printed = run_backfill(fake, rollback=path, apply=True)
    assert fake.exhibits[532]["master_name"] == "Мастер М. Перхин"
    assert "значение правили после прогона" in printed
    assert fake.exhibits[532]["dating"] is None            # остальное откатилось

    _, printed = run_backfill(fake, rollback=path, apply=True)
    assert "Возвращено карточек: 0" in printed


# ── Смежный баг: техника в материалах ───────────────────────────────────────────────────────
def test_material_technique_is_left_alone_without_the_flag():
    """Без ключа чистка не работает: это правка НЕПУСТОГО поля, а таких по умолчанию не делаем."""
    fake = catalog(card(48, "Царские врата", LINE_PROSE,
                        material="Серебро, Дерево, Левкас, Темпера, Тиснение"))
    _, printed = run_backfill(fake, apply=True)
    assert fake.exhibits[48]["material"] == "Серебро, Дерево, Левкас, Темпера, Тиснение"
    assert fake.patches() == []
    assert "--clean-material-techniques не задан" in printed


def test_clean_material_techniques_moves_the_technique():
    """id 48 «Царские врата»: «Тиснение» уезжает из материалов в техники строчными."""
    fake = catalog(card(48, "Царские врата", LINE_PROSE,
                        material="Серебро, Дерево, Левкас, Темпера, Тиснение"))
    run_backfill(fake, apply=True, clean_material_techniques=True)
    ex = fake.exhibits[48]
    assert ex["material"] == "Серебро, Дерево, Левкас, Темпера"
    assert ex["techniques"] == "тиснение"


def test_cleaning_is_idempotent():
    """Из вычищенного значения технику больше не выделить — второй прогон с ключом молчит."""
    fake = catalog(card(48, "Царские врата", LINE_PROSE,
                        material="Серебро, Дерево, Левкас, Темпера, Тиснение"))
    run_backfill(fake, apply=True, clean_material_techniques=True)
    patched = len(fake.patches())
    _, printed = run_backfill(fake, apply=True, clean_material_techniques=True)
    assert len(fake.patches()) == patched
    assert "Правок: 0" in printed


def test_cleaning_does_not_overwrite_existing_techniques():
    """Поле techniques уже занято — не перезаписываем, а показываем расхождение."""
    fake = catalog(card(48, "Царские врата", LINE_PROSE,
                        material="Серебро, Темпера, Тиснение", techniques="чеканка"))
    _, printed = run_backfill(fake, apply=True, clean_material_techniques=True)
    ex = fake.exhibits[48]
    assert ex["material"] == "Серебро, Темпера"             # материалы всё равно чистим
    assert ex["techniques"] == "чеканка"                    # чужое значение на месте
    assert "поле techniques уже занято" in printed


def test_enamel_is_never_pulled_out_of_material():
    """«эмаль» в указателе бывает и материалом (id 1079), и техникой — решает позиция, а у
    поля material её нет. Такую карточку показываем человеку, а не чистим наугад."""
    fake = catalog(card(1079, "Компас настольный", None,
                        material="Бовенит, Рубин, Золото, Стекло, Эмаль, Бумага"))
    _, printed = run_backfill(fake, apply=True, clean_material_techniques=True)
    assert fake.exhibits[1079]["material"] == "Бовенит, Рубин, Золото, Стекло, Эмаль, Бумага"
    assert fake.patches() == []
    assert "Требует глаз (1)" in printed


def test_cleaning_never_empties_the_material_field():
    """Если в material одни техники — чистка отказывается: пустое поле хуже неверного."""
    fake = catalog(card(900, "Блюдо", None, material="Тиснение, Чеканка"))
    _, printed = run_backfill(fake, apply=True, clean_material_techniques=True)
    assert fake.exhibits[900]["material"] == "Тиснение, Чеканка"
    assert fake.patches() == []
    assert "оставила бы поле пустым" in printed


# ── Обход каталога ──────────────────────────────────────────────────────────────────────────
def test_service_hall_is_in_scope_and_report_is_grouped_by_hall():
    """Зал 1 «Парадная лестница» помечен служебным и в публичном списке залов его нет (п.1),
    но экспонат в нём настоящий — бэкфилл обязан его увидеть."""
    fake = FakeApi(
        [{"id": 1, "hall_number": 1, "name": "Парадная лестница", "is_service": True},
         {"id": 6, "hall_number": 6, "name": "Аванзал", "is_service": False}],
        [card(458, "Бюсты", LINE_A1, hall_id=1), card(532, "Пасхальное яйцо", LINE_A2, hall_id=6)],
    )
    _, printed = run_backfill(fake, apply=True)
    assert fake.exhibits[458]["year_created"] == 1899
    assert "зал 1 — 1" in printed and "зал 6 — 1" in printed


def test_ids_mode_reads_the_card_directly():
    """--ids не ходит в списочную выдачу вовсе: карточка отдаёт все поля разом."""
    fake = catalog(card(532, "Пасхальное яйцо", LINE_A1), card(459, "Ковш", LINE_A2))
    run_backfill(fake, apply=True, ids="532")
    assert not any(path.startswith("/halls") for _, path, _ in fake.calls)
    assert fake.exhibits[532]["dating"] == "1899–1903"
    assert fake.exhibits[459]["dating"] is None


def test_report_file_lists_changes_conflicts_and_skips():
    """Список правок заказчику: и то, что сделали, и то, что сознательно не тронули."""
    fake = catalog(card(532, "Пасхальное яйцо", LINE_A1, master_name="Август Хольмстрём"),
                   card(48, "Царские врата", LINE_PROSE))
    path = os.path.join(tempfile.mkdtemp(), "report.json")
    run_backfill(fake, report_file=path)
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    assert doc["summary"]["changes"] == 4                   # мастера не трогали
    assert [c["exhibit_id"] for c in doc["conflicts"]] == [532]
    assert [s["exhibit_id"] for s in doc["skipped"]] == [48]


# ── Чистое ядро отдельно ────────────────────────────────────────────────────────────────────
def test_build_plan_needs_no_network():
    """build_plan — чистая функция над словарями: ни сети, ни БД."""
    plan = backfill.build_plan([card(532, "Пасхальное яйцо", LINE_A1, hall_id=6)])
    assert {c.field_name for c in plan.changes} == set(backfill.FIELD_TITLES)
    assert plan.parsed == 1 and plan.with_line == 1 and not plan.skips


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
