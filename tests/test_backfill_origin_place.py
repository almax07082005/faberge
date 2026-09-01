"""Юнит-тесты бэкфилла места создания (баг-репорт 31.08.2026, п. I-2, решение Д4).

Сам разбор каталожной строки покрыт отдельно (tests/test_catalog_line.py) — здесь проверяется
то, что делает вокруг него скрипт и чем он может испортить прод:

  • дозаполняется ТОЛЬКО пустое поле; непустое не перезаписывается никогда — карточки правит
    музей руками, и разбор путеводителя 2014 года не основание затирать более свежее решение;
  • повторный прогон даёт ноль PATCH — прод-БД нам недоступна, и «прогнать ещё раз, чтобы
    убедиться» должно быть безопасным по построению;
  • сухой прогон не пишет вообще ничего;
  • откат возвращает исходное значение, включая NULL, и не трогает поле, которое после
    прогона правили руками;
  • строка без места и связная музейная проза карточку не меняют вовсе;
  • ``short_description`` в патч не попадает никогда — это ИСТОЧНИК разбора (и заодно
    предохранитель от перегенерации озвучки в LLM, E15).

Сети и БД не нужно: сетевой слой скрипта — единственная функция ``api``, её подменяет FakeApi.
Списочная выдача в фейке намеренно НЕ отдаёт short_description и origin_place — ровно как
ExhibitSummary на проде, иначе тест не поймал бы, что скрипт забыл сходить за карточкой.

Запуск:
    python -m pytest tests/test_backfill_origin_place.py
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

import backfill_exhibit_origin_place as backfill  # noqa: E402

# Реальные строки с прода: A1 (74 % каталога), форма без места и связная музейная проза.
LINE_A1 = ("Санкт-Петербург, 1899–1903. Фирма К. Фаберже, мастер М. Перхин. "
           "Золото, серебро, сталь, сапфир; штамп, чеканка, гравировка, золочение")
LINE_MOSCOW = "Москва, вторая половина XVI века. Дерево, левкас, темпера"
LINE_NO_PLACE = "1908−1917. Серебро; выемчатая эмаль, живопись по эмали"
LINE_PROSE = ("Редким и ценным музейным предметом являются царские врата рубежа XVI–XVII веков. "
              "На них, как обычно, представлено «Благовещение». Поскольку врата были важным "
              "элементом иконостаса, их дополнительно украсили серебряной басмой.")


# ── Фейковый каталог ────────────────────────────────────────────────────────────────────────
class FakeApi:
    """Каталог в памяти: залы → экспонаты. Умеет ровно те ручки, что дёргает бэкфилл."""

    # Поля ExhibitSummary: ни описания, ни места в списочной выдаче НЕТ.
    SUMMARY = ("id", "exhibit_number", "name", "year_created", "master_name",
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
            assert set(body) == {"origin_place"}, "скрипт пишет ровно одно поле"
            ex.update(body)
            return 200, self._detail(ex)
        raise AssertionError(f"фейковый API не знает {method} {path}")

    def patches(self) -> list:
        return [(path, body) for method, path, body in self.calls if method == "PATCH"]

    def places(self) -> dict:
        return {ex["id"]: ex.get("origin_place") for ex in self.exhibits.values()}


def card(ex_id: int, name: str, line=None, hall_id: int = 6, **fields) -> dict:
    base = {
        "id": ex_id, "name": name, "hall_id": hall_id, "exhibit_number": None,
        "showcase_id": 20, "showcase_number": 1, "short_description": line,
        "year_created": None, "master_name": None, "origin_place": None,
    }
    base.update(fields)
    return base


def catalog(*cards, halls=None) -> FakeApi:
    halls = halls or [{"id": 6, "hall_number": 6, "name": "Аванзал", "is_service": False}]
    return FakeApi(halls, list(cards))


def _args(**over) -> argparse.Namespace:
    base = dict(apply=False, ids=None, limit=None, report_file=None,
                rollback_file=None, rollback=None, max_print=200)
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
def test_fills_place_from_the_catalog_line():
    fake = catalog(card(1, "Яйцо «Ландыши»", LINE_A1), card(2, "Икона", LINE_MOSCOW))
    run_backfill(fake, apply=True)
    assert fake.places() == {1: "Санкт-Петербург", 2: "Москва"}


def test_dry_run_writes_nothing():
    """Сухой прогон — план и только план: ни одного PATCH."""
    fake = catalog(card(1, "Яйцо «Ландыши»", LINE_A1))
    code, out = run_backfill(fake)
    assert code == 0 and not fake.patches()
    assert fake.places() == {1: None}
    assert "сухой прогон" in out.lower()
    assert "Санкт-Петербург" in out            # что именно получится — видно до применения


def test_second_run_is_a_no_op():
    """Повторный прогон не шлёт ни одного PATCH — прод так можно перепроверять безнаказанно."""
    fake = catalog(card(1, "Яйцо «Ландыши»", LINE_A1))
    run_backfill(fake, apply=True)
    before = len(fake.patches())
    _, out = run_backfill(fake, apply=True)
    assert len(fake.patches()) == before
    assert "Правок нет" in out


def test_non_empty_place_is_never_overwritten():
    """Ручная правка музея сильнее разбора путеводителя — расхождение уходит в отчёт."""
    fake = catalog(card(1, "Яйцо «Ландыши»", LINE_A1, origin_place="С.-Петербург"))
    _, out = run_backfill(fake, apply=True)
    assert not fake.patches()
    assert fake.places() == {1: "С.-Петербург"}
    assert "Расходится с каталожной строкой" in out
    assert "С.-Петербург" in out and "Санкт-Петербург" in out


def test_same_value_is_not_a_conflict():
    """Уже проставленное тем же значением — не расхождение и не правка, просто тишина."""
    fake = catalog(card(1, "Яйцо «Ландыши»", LINE_A1, origin_place="санкт-петербург  "))
    _, out = run_backfill(fake, apply=True)
    assert not fake.patches()
    assert "Уже заполнено тем же значением: 1" in out


def test_line_without_place_leaves_the_card_alone():
    fake = catalog(card(1, "Ковш", LINE_NO_PLACE))
    _, out = run_backfill(fake, apply=True)
    assert not fake.patches()
    assert "в каталожной строке нет места" in out


def test_prose_is_skipped_entirely():
    """Связная музейная проза — не каталожная строка: разбирать её нельзя."""
    fake = catalog(card(48, "Царские врата", LINE_PROSE))
    _, out = run_backfill(fake, apply=True)
    assert not fake.patches()
    assert fake.places() == {48: None}
    assert "не каталожная строка" in out


def test_card_without_line_is_not_even_counted():
    fake = catalog(card(1, "Карточка без описания", None))
    _, out = run_backfill(fake, apply=True)
    assert not fake.patches()
    assert "с каталожной строкой: 0" in out


def test_unknown_toponym_is_flagged_for_a_human():
    """Парсер сам сказал, что топоним не из словаря, — заполняем, но показываем человеку.

    Реальный случай из отчёта 12.08.2026: у «Карл Брюллов (1799–1852). Эскиз. 1850» в место
    уезжает слово «Эскиз». Скрипт его не выбрасывает (решать за музей он не вправе), но и не
    прячет: карточка попадает в секцию «требует глаз».
    """
    line = "Карл Брюллов (1799–1852). Эскиз. 1850. Бумага, акварель"
    fake = catalog(card(1120, "Эскиз", line))
    _, out = run_backfill(fake, apply=True)
    assert fake.places() == {1120: "Эскиз"}
    assert "Требует глаз" in out
    assert "не из словаря топонимов" in out


# ── Точечный прогон и отчёт ─────────────────────────────────────────────────────────────────
def test_ids_mode_reads_the_card_directly():
    """С --ids списочная выдача не нужна вовсе: карточка отдаёт все поля разом."""
    fake = catalog(card(1, "Яйцо «Ландыши»", LINE_A1), card(2, "Икона", LINE_MOSCOW))
    run_backfill(fake, ids="2", apply=True)
    assert fake.places() == {1: None, 2: "Москва"}
    assert not any(path.startswith("/halls") for _, path, _ in fake.calls)


def test_report_file_lists_every_change():
    fake = catalog(card(1, "Яйцо «Ландыши»", LINE_A1), card(48, "Царские врата", LINE_PROSE))
    path = os.path.join(tempfile.mkdtemp(), "report.json")
    run_backfill(fake, report_file=path)
    doc = json.load(open(path, encoding="utf-8"))
    assert doc["summary"]["changes"] == 1
    assert doc["changes"][0]["after"] == "Санкт-Петербург"
    assert doc["summary"]["skipped"] == 1


# ── Откат ───────────────────────────────────────────────────────────────────────────────────
def test_rollback_restores_null():
    fake = catalog(card(1, "Яйцо «Ландыши»", LINE_A1))
    rollback = os.path.join(tempfile.mkdtemp(), "rollback.json")
    run_backfill(fake, apply=True, rollback_file=rollback)
    assert fake.places() == {1: "Санкт-Петербург"}
    run_backfill(fake, rollback=rollback, apply=True)
    assert fake.places() == {1: None}


def test_rollback_keeps_a_manual_edit():
    """После прогона поле правили руками — откат его не трогает и говорит об этом."""
    fake = catalog(card(1, "Яйцо «Ландыши»", LINE_A1))
    rollback = os.path.join(tempfile.mkdtemp(), "rollback.json")
    run_backfill(fake, apply=True, rollback_file=rollback)
    fake.exhibits[1]["origin_place"] = "Санкт-Петербург (уточнено музеем)"
    _, out = run_backfill(fake, rollback=rollback, apply=True)
    assert fake.places() == {1: "Санкт-Петербург (уточнено музеем)"}
    assert "разбирайтесь руками" in out


def test_rollback_dry_run_changes_nothing():
    fake = catalog(card(1, "Яйцо «Ландыши»", LINE_A1))
    rollback = os.path.join(tempfile.mkdtemp(), "rollback.json")
    run_backfill(fake, apply=True, rollback_file=rollback)
    patches = len(fake.patches())
    _, out = run_backfill(fake, rollback=rollback)
    assert len(fake.patches()) == patches
    assert "сухой прогон отката" in out


def test_rollback_dry_run_does_not_claim_anything_was_returned():
    """Сухой прогон не имеет права рапортовать «возвращено»: он ничего не отправлял.

    Раньше печать и счётчик стояли ДО запроса, и оператор видел «Возвращено карточек: 1»
    там, где не ушло ни одного PATCH.
    """
    fake = catalog(card(1, "Яйцо «Ландыши»", LINE_A1))
    rollback = os.path.join(tempfile.mkdtemp(), "rollback.json")
    run_backfill(fake, apply=True, rollback_file=rollback)
    _, out = run_backfill(fake, rollback=rollback)
    assert "будет возвращено карточек 1" in out
    assert "Итог отката" not in out


def test_rollback_reports_three_numbers():
    """Итог — возвращено / пропущено / ошибок. По одному числу чистый прогон от кривого не отличить."""
    fake = catalog(card(1, "Яйцо «Ландыши»", LINE_A1), card(2, "Икона", LINE_MOSCOW))
    rollback = os.path.join(tempfile.mkdtemp(), "rollback.json")
    run_backfill(fake, apply=True, rollback_file=rollback)
    fake.exhibits[2]["origin_place"] = "Москва (уточнено музеем)"      # чужая правка — пропуск
    _, out = run_backfill(fake, rollback=rollback, apply=True)
    assert "Итог отката: возвращено карточек 1, пропущено 1, ошибок 0" in out


def test_rollback_counts_a_failed_patch_as_an_error():
    """Провал запроса не должен попадать в «возвращено» — а попадал: счётчик рос до PATCH."""
    fake = catalog(card(1, "Яйцо «Ландыши»", LINE_A1))
    rollback = os.path.join(tempfile.mkdtemp(), "rollback.json")
    run_backfill(fake, apply=True, rollback_file=rollback)

    def refusing(method, path, body=None):
        if method == "PATCH":
            return 500, {"detail": "прод прилёг"}
        return fake(method, path, body)

    code, out = run_backfill(refusing, rollback=rollback, apply=True)
    assert code == 1
    assert "ОШИБКА отката id=1" in out
    assert "возвращено карточек 0" in out and "ошибок 1" in out


def test_rollback_detects_a_silent_no_op():
    """200 с ПРЕЖНИМ значением в теле — не успех, а «НЕ ОТКАТИЛОСЬ».

    Ровно так вёл себя откат стадии image у соседнего скрипта: бэкенд подставлял главное
    фото из галереи и отвечал 200, в БД не менялось ничего, а скрипт печатал успех.
    """
    fake = catalog(card(1, "Яйцо «Ландыши»", LINE_A1))
    rollback = os.path.join(tempfile.mkdtemp(), "rollback.json")
    run_backfill(fake, apply=True, rollback_file=rollback)

    def stubborn(method, path, body=None):
        if method == "PATCH":
            return 200, {"id": 1, "origin_place": "Санкт-Петербург"}   # проигнорировал null
        return fake(method, path, body)

    code, out = run_backfill(stubborn, rollback=rollback, apply=True)
    assert code == 1
    assert "НЕ ОТКАТИЛОСЬ id=1" in out
    assert "возвращено карточек 0" in out and "ошибок 1" in out


def test_rolled_back_checks_the_response_body():
    """Чистая проверка ответа: сверяем значение, а не код, и «поля нет» успехом не считаем."""
    assert backfill.rolled_back(None, {"origin_place": None}) is True
    assert backfill.rolled_back("Москва", {"origin_place": "Москва"}) is True
    assert backfill.rolled_back(None, {"origin_place": "Санкт-Петербург"}) is False
    assert backfill.rolled_back(None, {"id": 1}) is False          # поля в ответе нет — не проверили
    assert backfill.rolled_back(None, "500 Internal Server Error") is False


# ── Чистое ядро ─────────────────────────────────────────────────────────────────────────────
def test_build_plan_needs_no_network():
    """План строится по обычным словарям — ядро скрипта отделено от сети сознательно."""
    plan = backfill.build_plan([
        {"id": 1, "name": "Яйцо", "short_description": LINE_A1, "origin_place": None},
        {"id": 2, "name": "Ковш", "short_description": LINE_NO_PLACE, "origin_place": None},
    ])
    assert plan.scanned == 2 and plan.with_line == 2 and plan.with_place == 1
    assert [c.after for c in plan.changes] == ["Санкт-Петербург"]
    assert len(plan.skips) == 1


if __name__ == "__main__":
    import pytest

    sys.exit(pytest.main([__file__, "-v"]))
