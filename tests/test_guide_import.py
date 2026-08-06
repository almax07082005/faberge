"""Юнит-тесты импорта витрин по путеводителю (баг-репорт 06.08.2026, п.1).

Покрывают то, из-за чего скрипт разносил каталог: сшивку строки выписки с карточкой.
В проде 704 карточки из 1253 носят неуникальное нормализованное название, и 647
пронумерованных из них не имеют label_slug — то есть сшиваются ТОЛЬКО по имени
(«Портсигар» — 150 карточек в 33 витринах, «Ковш» — 57 в 24). Пока индекс отдавал всем
одноимённым строкам одну и ту же первую карточку, выписка, побуквенно повторяющая прод
(заведомый no-op), давала «перепривязать 508, номер 552»: 98 карточек меняли место,
44 уезжали в другой зал, а повторный прогон давал не ноль, а новую порцию.

Сети и БД не нужно: сетевой слой скрипта — единственная функция `api`, её подменяет FakeApi.
Запуск:
    python -m pytest tests/test_guide_import.py
    python tests/test_guide_import.py     # standalone
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import import_guide_showcases as guide  # noqa: E402


# ── Фейковый каталог ────────────────────────────────────────────────────────────────────────
class FakeApi:
    """Каталог в памяти: залы → витрины → экспонаты. Умеет ровно те ручки, что дёргает импорт."""

    def __init__(self, halls, showcases, exhibits) -> None:
        self.halls = {h["id"]: dict(h) for h in halls}
        self.showcases = {s["id"]: dict(s) for s in showcases}
        self.exhibits = {e["id"]: dict(e) for e in exhibits}
        self.calls: list = []
        self.fail_showcase_numbers: set = set()   # номера витрин, POST по которым отвечает 500
        self._next = 700

    @staticmethod
    def _page(items, query):
        params = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
        limit, offset = int(params.get("limit", 100)), int(params.get("offset", 0))
        return 200, {"items": items[offset:offset + limit], "total": len(items)}

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        head, _, query = path.partition("?")
        parts = head.strip("/").split("/")

        if method == "GET" and head == "/halls":
            return self._page(sorted(self.halls.values(), key=lambda h: h["id"]), query)
        if method == "GET" and head == "/exhibits":
            return self._page(sorted(self.exhibits.values(), key=lambda e: e["id"]), query)
        if method == "GET" and head.startswith("/halls/") and head.endswith("/showcases"):
            hall_id = int(parts[1])
            return self._page([s for s in self.showcases.values() if s["hall_id"] == hall_id], query)
        if method == "POST" and head == "/admin/showcases":
            if body.get("showcase_number") in self.fail_showcase_numbers:
                return 500, {"detail": "gateway timeout"}
            self._next += 1
            sc = {"id": self._next, "hall_id": body["hall_id"],
                  "showcase_number": body.get("showcase_number"), "name": body.get("name")}
            self.showcases[sc["id"]] = sc
            return 201, sc
        if method == "POST" and head == "/admin/exhibits":
            self._next += 1
            sc = self.showcases[body["showcase_id"]]
            ex = dict(body, id=self._next, hall_id=sc["hall_id"], showcase_number=sc["showcase_number"])
            self.exhibits[ex["id"]] = ex
            return 201, ex
        if method == "PATCH" and head.startswith("/admin/exhibits/"):
            ex = self.exhibits[int(parts[-1])]
            ex.update(body)
            if "showcase_id" in body:
                sc = self.showcases[body["showcase_id"]]
                ex["hall_id"], ex["showcase_number"] = sc["hall_id"], sc["showcase_number"]
            return 200, ex
        raise AssertionError(f"фейковый API не знает {method} {path}")

    def places(self) -> dict:
        return {e["id"]: (e["showcase_id"], e.get("exhibit_number")) for e in self.exhibits.values()}


def catalog(*halls) -> FakeApi:
    """Собрать FakeApi из компактного описания: hall(id, имя, showcase(...), ...)."""
    halls_meta, showcases, exhibits = [], [], []
    for hall_meta, groups in halls:
        halls_meta.append(hall_meta)
        for sc_meta, cards in groups:
            sc = dict(sc_meta, hall_id=hall_meta["id"])
            showcases.append(sc)
            for card in cards:
                exhibits.append(dict(card, showcase_id=sc["id"], hall_id=sc["hall_id"],
                                     showcase_number=sc["showcase_number"]))
    return FakeApi(halls_meta, showcases, exhibits)


def hall(hall_id: int, name: str, *groups):
    return ({"id": hall_id, "name": name, "hall_number": hall_id, "is_service": False}, list(groups))


def showcase(sc_id: int, number, *cards):
    return ({"id": sc_id, "showcase_number": number, "name": None}, list(cards))


def card(ex_id: int, name: str, number=None, slug=None):
    return {"id": ex_id, "name": name, "exhibit_number": number, "label_slug": slug}


def spec(number, *rows) -> dict:
    return {"number": number, "exhibits": list(rows)}


def row(name: str, number=None, slug=None) -> dict:
    return {"number": number, "name": name, "label_slug": slug}


def run_import(fake: FakeApi, doc: dict, apply=True, sweep=False):
    """Прогнать импорт по выписке; вернуть (код возврата, напечатанное)."""
    path = os.path.join(tempfile.mkdtemp(), "guide.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False)
    saved, buffer = guide.api, io.StringIO()
    guide.api = fake
    try:
        with contextlib.redirect_stdout(buffer):
            code = guide.run(path, apply=apply, sweep=sweep)
    finally:
        guide.api = saved
    return code, buffer.getvalue()


def totals(printed: str) -> dict:
    """Разобрать итоговую строку «Витрин создать: N; …» в словарь."""
    line = next(ln for ln in printed.splitlines() if ln.startswith("Витрин создать"))
    out = {}
    for part in line.split(";"):
        key, _, value = part.partition(":")
        out[key.strip()] = int(value.split("(")[0].strip())
    return out


# ── Потребляющая сшивка (blocker: by_name отдавал всем одну карточку) ────────────────────────
def test_same_name_rows_take_different_cards():
    """Две строки «Портсигар» в разных витринах зала — это две РАЗНЫЕ карточки, а не одна дважды."""
    fake = catalog(hall(6, "Аванзал",
                        showcase(20, 1, card(157, "Портсигар", "1")),
                        showcase(21, 2, card(161, "Портсигар", "2"))))
    before = fake.places()
    _, printed = run_import(fake, {"halls": [{"hall_number": 6, "showcases": [
        spec(1, row("Портсигар", "1")), spec(2, row("Портсигар", "2"))]}]})
    assert totals(printed)["перепривязать"] == 0
    assert fake.places() == before


def test_noop_extract_changes_nothing_and_repeats_cleanly():
    """Выписка, повторяющая каталог, — заведомый no-op. Это и есть DoD «повторный прогон не возвращает»."""
    fake = catalog(hall(6, "Аванзал",
                        showcase(20, 1, card(157, "Портсигар", "1"), card(158, "Ковш", "2")),
                        showcase(21, 2, card(161, "Портсигар", "3"))))
    doc = {"halls": [{"hall_number": 6, "showcases": [
        spec(1, row("Портсигар", "1"), row("Ковш", "2")), spec(2, row("Портсигар", "3"))]}]}
    before = fake.places()
    for _ in range(2):
        _, printed = run_import(fake, doc)
        assert totals(printed)["перепривязать"] == 0
        assert totals(printed)["проставить номер"] == 0
        assert fake.places() == before
    assert not any(m == "PATCH" for m, _, _ in fake.calls)


def test_used_card_is_never_handed_out_twice():
    """Строк в выписке больше, чем одноимённых карточек: лишняя заводит НОВУЮ, а не крадёт чужую."""
    fake = catalog(hall(6, "Аванзал", showcase(20, 1, card(157, "Портсигар", "1"))))
    _, printed = run_import(fake, {"halls": [{"hall_number": 6, "showcases": [
        spec(1, row("Портсигар", "1"), row("Портсигар", "2"))]}]})
    assert totals(printed)["экспонатов завести"] == 1
    assert fake.exhibits[157]["exhibit_number"] == "1"      # старую карточку не перенумеровали


def test_name_match_never_crosses_halls():
    """Сшивка по одному лишь названию заперта в целевом зале: 44 карточки уезжали именно так."""
    fake = catalog(hall(2, "Рыцарский зал", showcase(10, 4, card(96, "Кружка", "4"))),
                   hall(8, "Голубая гостиная", showcase(35, 35)))
    _, printed = run_import(fake, {"halls": [{"hall_number": 8, "showcases": [
        spec(35, row("Кружка", "1"))]}]})
    assert fake.exhibits[96]["hall_id"] == 2               # осталась в Рыцарском
    assert totals(printed)["экспонатов завести"] == 1      # в Голубой заведена своя карточка


def test_label_slug_beats_name_and_may_cross_halls():
    """label_slug — уникальный осознанный ключ: ему верим, даже если карточка в другом зале."""
    fake = catalog(hall(2, "Рыцарский зал", showcase(10, 4, card(96, "Кружка", "4", slug="kruzhka"))),
                   hall(8, "Голубая гостиная", showcase(35, 35, card(500, "Кружка"))))
    _, printed = run_import(fake, {"halls": [{"hall_number": 8, "showcases": [
        spec(35, row("Кружка", "1", slug="kruzhka"))]}]})
    assert fake.exhibits[96]["showcase_id"] == 35
    assert "ДРУГОЙ ЗАЛ" in printed                        # перенос через зал виден в плане


def test_card_already_in_the_target_showcase_wins():
    """Из одноимённых предпочитаем ту, что уже лежит куда надо, — иначе двигали бы обе зря."""
    fake = catalog(hall(6, "Аванзал",
                        showcase(20, 1, card(157, "Портсигар")),
                        showcase(21, 2, card(161, "Портсигар"))))
    run_import(fake, {"halls": [{"hall_number": 6, "showcases": [spec(2, row("Портсигар", "3"))]}]})
    assert fake.exhibits[161]["exhibit_number"] == "3"     # взяли ту, что уже в витрине 2
    assert fake.exhibits[157]["exhibit_number"] is None


# ── Сухой прогон показывает ровно то, что сделает --apply ───────────────────────────────────
def test_dry_run_counts_moves_into_a_showcase_that_does_not_exist_yet():
    """Витрины ещё нет: раньше план молчал («перепривязать: 0»), а --apply двигал карточки."""
    fake = catalog(hall(6, "Аванзал", showcase(20, 1, card(157, "Портсигар", "1"))))
    doc = {"halls": [{"hall_number": 6, "showcases": [spec(7, row("Портсигар", "1"))]}]}
    _, dry = run_import(catalog(hall(6, "Аванзал", showcase(20, 1, card(157, "Портсигар", "1")))),
                        doc, apply=False)
    _, wet = run_import(fake, doc, apply=True)
    assert totals(dry) == totals(wet)
    assert totals(dry)["перепривязать"] == 1
    assert "новая витрина 7" in dry


# ── Ошибка API не должна оборачиваться потерей содержимого витрин ───────────────────────────
def test_failed_showcase_creation_cancels_the_sweep_for_that_hall():
    """POST витрины упал → экспонаты её spec'а не проверены. Свип по залу обязан отмениться.

    Иначе `continue` уносил их из matched_ids, свип считал «лишними» и выносил из ЧУЖИХ витрин —
    витрина пустела руками самого импорта, ровно то, на что жалуется заказчик.
    """
    fake = catalog(hall(6, "Аванзал",
                        showcase(20, 1, card(157, "Портсигар", "1"), card(122, "Набор пуговиц")),
                        showcase(53, None)))
    fake.fail_showcase_numbers.add(9)
    before = fake.places()
    _, printed = run_import(fake, {"halls": [{"hall_number": 6, "showcases": [
        spec(1, row("Портсигар", "1")), spec(9, row("Новый предмет", "1"))]}]}, sweep=True)
    assert "сшивка оборвалась на ошибке API" in printed
    assert fake.places() == before                          # ни одного переноса
    assert totals(printed)["не сшито"] == 0                 # хвост по залу не считался


def test_sweep_moves_only_cards_without_number():
    """Свип трогает записи без номера; запись С номером — промах сшивки, её оставляем в витрине."""
    fake = catalog(hall(6, "Аванзал",
                        showcase(20, 1, card(157, "Портсигар", "1"), card(122, "Набор пуговиц"),
                                 card(199, "Ковш", "5")),
                        showcase(53, None)))
    _, printed = run_import(fake, {"halls": [{"hall_number": 6, "showcases": [
        spec(1, row("Портсигар", "1"))]}]}, sweep=True)
    assert fake.exhibits[122]["showcase_id"] == 53          # без номера — уехал
    assert fake.exhibits[199]["showcase_id"] == 20          # с номером — остался
    assert "оставлен в витрине" in printed


# ── Индекс отдельно ─────────────────────────────────────────────────────────────────────────
def test_index_returns_none_when_pool_is_exhausted():
    index = guide.ExhibitIndex([{"id": 1, "name": "Ковш", "hall_id": 6, "showcase_id": 20}])
    assert index.take(None, "Ковш", hall_id=6, showcase_id=20, number=None)[0]["id"] == 1
    assert index.take(None, "Ковш", hall_id=6, showcase_id=20, number=None) == (None, "")


def test_index_prefers_the_card_with_the_expected_number():
    cards = [
        {"id": 1, "name": "Ковш", "hall_id": 6, "showcase_id": 20, "exhibit_number": "9"},
        {"id": 2, "name": "Ковш", "hall_id": 6, "showcase_id": 20, "exhibit_number": "3"},
    ]
    found, how = guide.ExhibitIndex(cards).take(None, "Ковш", hall_id=6, showcase_id=20, number="3")
    assert (found["id"], how) == (2, "name")


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
