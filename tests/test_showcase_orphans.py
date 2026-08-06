"""Юнит-тесты разбора записей без номера в витринах (баг-репорт 06.08.2026, п.1 и п.4).

Проверяется scripts/fix_showcase_orphans.py целиком:
  • чистое ядро — классификация сирот и планирование действий (снимок каталога словарями);
  • РАЗРУШАЮЩАЯ половина — apply_plan/run_rollback/run поверх фейкового API (класс FakeApi).
Второе появилось не для красоты: тесты по одному лишь ядру пропустили дефект, из-за которого
удаление сироты стирало её label_slug, то есть класс распознавания, — а fetch_catalog/apply_plan
были не покрыты ничем. БД и сеть по-прежнему не нужны. Запуск:
    python -m pytest tests/test_showcase_orphans.py
    python tests/test_showcase_orphans.py     # standalone
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

import fix_showcase_orphans as orphans  # noqa: E402


# ── Сборка снимка каталога в форме, которую отдаёт fetch_catalog ────────────────────────────
def exhibit(ex_id: int, name: str, number=None, **extra) -> dict:
    return {"id": ex_id, "name": name, "exhibit_number": number, **extra}


def showcase(sc_id: int, number, *exhibits, name=None) -> dict:
    return {"showcase": {"id": sc_id, "showcase_number": number, "name": name}, "exhibits": list(exhibits)}


def hall(hall_id: int, name: str, *showcases, **flags) -> dict:
    meta = {"id": hall_id, "name": name, "is_service": False, "hall_number": hall_id}
    meta.update(flags)
    return {"hall": meta, "showcases": list(showcases)}


def ids(actions) -> list:
    return [a.exhibit_id for a in actions]


# ── Что попадает в план ─────────────────────────────────────────────────────────────────────
def test_orphan_in_numbered_showcase_goes_to_plan():
    """Запись без номера в витрине №1 — та самая «лишняя» из п.1: её надо увезти."""
    data = [hall(6, "Аванзал",
                 showcase(20, 1, exhibit(122, "Набор пуговиц"), exhibit(637, "Комплект марок", "7")),
                 showcase(53, None))]
    plan = orphans.classify_orphans(data)
    assert ids(plan.actions) == [122]
    act = plan.actions[0]
    assert act.action == orphans.MOVE
    assert (act.from_showcase_id, act.to_showcase_id) == (20, 53)


def test_numbered_exhibit_is_never_touched():
    """У экспоната есть номер по путеводителю — он сшит, трогать нечего."""
    data = [hall(6, "Аванзал", showcase(20, 1, exhibit(637, "Набор пуговиц", "7")), showcase(53, None))]
    assert orphans.classify_orphans(data).actions == []


def test_blank_exhibit_number_counts_as_missing():
    """Пустая строка и пробелы — это тот же «номера нет», а не номер."""
    data = [hall(6, "Аванзал", showcase(20, 1, exhibit(1, "Портсигар", ""), exhibit(2, "Брошь", "  ")),
                 showcase(53, None))]
    assert ids(orphans.classify_orphans(data).actions) == [1, 2]


def test_exhibit_already_in_unnumbered_group_is_idempotent():
    """Повторный прогон: сирота уже лежит в «Не в витринах» — плана нет, ничего не меняем."""
    data = [hall(6, "Аванзал", showcase(20, 1, exhibit(637, "Набор пуговиц", "7")),
                 showcase(53, None, exhibit(122, "Набор пуговиц")))]
    plan = orphans.classify_orphans(data)
    assert plan.actions == []
    assert plan.empty_showcases == []
    assert plan.groups_to_create == []


def test_missing_unnumbered_group_is_scheduled_for_creation():
    """В Белой гостиной группы «Не в витринах» нет — её надо создать, иначе переносить некуда."""
    data = [hall(7, "Белая гостиная", showcase(60, 1, exhibit(500, "Дамская сумочка")))]
    plan = orphans.classify_orphans(data)
    assert plan.groups_to_create == [7]
    assert plan.actions[0].to_showcase_id is None


# ── Классификация: дубль или «вне путеводителя» ─────────────────────────────────────────────
def test_similar_name_is_marked_as_probable_duplicate():
    data = [hall(5, "Золотая гостиная",
                 showcase(30, 1,
                          exhibit(7, "Пасхальное яйцо «Ренессанс»"),
                          exhibit(531, "Пасхальное яйцо-шкатулка «Ренессанс»", "1")),
                 showcase(44, None))]
    act = orphans.classify_orphans(data).actions[0]
    assert act.kind == orphans.DUPLICATE
    assert act.match.exhibit_id == 531
    assert act.match.similarity >= 0.80
    assert act.action == orphans.MOVE      # дубль всё равно только переносится, не удаляется


def test_unrelated_name_is_off_guide():
    data = [hall(5, "Золотая гостиная",
                 showcase(30, 1, exhibit(9, "Театральный бинокль"), exhibit(531, "Пасхальное яйцо", "1")),
                 showcase(44, None))]
    act = orphans.classify_orphans(data).actions[0]
    assert act.kind == orphans.OFF_GUIDE
    assert act.match is None


def test_similarity_threshold_moves_the_border():
    """Одна и та же пара — дубль при мягком пороге и «вне путеводителя» при строгом."""
    data = [hall(3, "Красная гостиная",
                 showcase(30, 1,
                          exhibit(231, "Вазы (пара) с видами по картинам Ф. Ваувермана"),
                          exhibit(473, "Вазы (пара) с видами по живописному оригиналу Ф. Ваувермана", "3")),
                 showcase(29, None))]
    assert orphans.classify_orphans(data, similarity=0.80).actions[0].kind == orphans.DUPLICATE
    assert orphans.classify_orphans(data, similarity=0.99).actions[0].kind == orphans.OFF_GUIDE


def test_duplicate_is_searched_only_inside_own_hall():
    """Похожий экспонат из соседнего зала — не дубль: путеводитель нумерует внутри зала."""
    data = [
        hall(3, "Красная гостиная", showcase(30, 1, exhibit(1, "Портсигар с эмалью")), showcase(29, None)),
        hall(6, "Аванзал", showcase(40, 1, exhibit(2, "Портсигар с эмалью", "7")), showcase(53, None)),
    ]
    plan = orphans.classify_orphans(data)
    assert [a.kind for a in plan.actions] == [orphans.OFF_GUIDE]


def test_best_match_wins_among_candidates():
    """Кандидатов выше порога несколько — в отчёт музею идёт самый похожий, а не первый попавшийся."""
    data = [hall(6, "Аванзал",
                 showcase(20, 1,
                          exhibit(157, "Портсигар золотой с сапфиром"),
                          exhibit(634, "Ваза стеклянная", "1"),               # 0.23 — мимо порога
                          exhibit(636, "Портсигар золотой с рубином", "2"),   # 0.84 — похож
                          exhibit(640, "Портсигар золотой с сапфирами", "3")),  # 0.95 — похож сильнее
                 showcase(53, None))]
    act = orphans.classify_orphans(data).actions[0]
    assert act.match.exhibit_id == 640
    assert act.match.exhibit_number == "3"
    assert act.match.similarity > 0.9


def test_numbered_exhibit_outside_showcases_is_a_duplicate_candidate():
    """Пронумерованный экспонат из группы «Не в витринах» тоже сшит — сравниваем и с ним."""
    data = [hall(10, "Верхняя буфетная",
                 showcase(4, 1, exhibit(225, "Ваза c изображением цветов")),
                 showcase(92, None, exhibit(1265, "Ваза с изображением цветов", "8")))]
    act = orphans.classify_orphans(data).actions[0]
    assert act.kind == orphans.DUPLICATE and act.match.exhibit_id == 1265


# ── Служебные залы ──────────────────────────────────────────────────────────────────────────
def test_service_hall_is_skipped_by_default():
    data = [hall(1, "Парадная лестница", showcase(13, 1, exhibit(458, "Бюст")), is_service=True)]
    plan = orphans.classify_orphans(data)
    assert plan.actions == []
    assert plan.skipped_halls == [(1, "Парадная лестница", 1)]


def test_outside_exposition_hall_is_skipped_by_default():
    """«Вне постоянной экспозиции» не is_service, но заказчик просил её 4 записи не трогать."""
    data = [hall(12, "Вне постоянной экспозиции", showcase(110, 1, exhibit(900, "Ковш")), hall_number=None)]
    assert orphans.classify_orphans(data).actions == []


def test_include_service_switches_service_halls_on():
    data = [
        hall(1, "Парадная лестница", showcase(13, 1, exhibit(458, "Бюст")), is_service=True),
        hall(12, "Вне постоянной экспозиции", showcase(110, 1, exhibit(900, "Ковш")), hall_number=None),
    ]
    plan = orphans.classify_orphans(data, include_service=True)
    assert ids(plan.actions) == [458, 900]
    assert plan.skipped_halls == []


# ── Опустевшие витрины (п.4 — Верхняя буфетная) ─────────────────────────────────────────────
def test_showcase_is_empty_only_when_all_exhibits_leave():
    data = [hall(10, "Верхняя буфетная",
                 showcase(4, 1, exhibit(225, "Ваза c изображением цветов")),         # уедет целиком
                 showcase(5, 2, exhibit(226, "Блюдо"), exhibit(227, "Чаша", "2")),   # останется экспонат
                 showcase(92, None))]
    plan = orphans.classify_orphans(data)
    assert [ref.showcase_id for ref in plan.empty_showcases] == [4]


def test_already_empty_showcase_is_not_dropped():
    """Пустую витрину, из которой мы ничего не увозили, скрипт не трогает: её мог завести музей."""
    data = [hall(10, "Верхняя буфетная",
                 showcase(4, 1, exhibit(225, "Ваза")),
                 showcase(6, 3),
                 showcase(92, None))]
    assert [ref.showcase_id for ref in orphans.classify_orphans(data).empty_showcases] == [4]


def test_unnumbered_group_never_counted_as_empty():
    """Группа «Не в витринах» — не витрина, удалять её нельзя, даже если она пуста."""
    data = [hall(10, "Верхняя буфетная", showcase(4, 1, exhibit(225, "Ваза")), showcase(92, None))]
    assert all(ref.showcase_number is not None for ref in orphans.classify_orphans(data).empty_showcases)


# ── Удаление только по списку музея ─────────────────────────────────────────────────────────
def test_delete_ids_delete_only_clean_cards():
    """Карточка совсем без данных — единственный случай, когда удаление доходит до API."""
    data = [hall(6, "Аванзал", showcase(20, 1, exhibit(122, "Набор пуговиц")), showcase(53, None))]
    plan = orphans.classify_orphans(data, delete_ids=[122])
    assert ids(plan.deletions) == [122]
    assert plan.moves == []


def test_delete_is_blocked_by_label_slug():
    """label_slug — это класс распознавания: удалив карточку, выключаем поиск предмета по фото.

    На проде label_slug есть у ВСЕХ 46 сирот, причём у 14 из них нет ни фото, ни описания, —
    раньше такие проходили как «чистые» и удалялись молча, а в файле отката slug не сохранялся.
    """
    data = [hall(6, "Аванзал",
                 showcase(20, 1, exhibit(111, "Спичечница-чиркаш в виде свиньи",
                                         label_slug="match-box-holder-with-pig-shaped-lighter")),
                 showcase(53, None))]
    plan = orphans.classify_orphans(data, delete_ids=[111])
    assert plan.deletions == []
    assert ids(plan.moves) == [111]
    assert plan.blocked[0].marks == ("распознавание",)
    # И slug обязан попасть в снимок карточки — иначе восстанавливать будет нечем.
    assert plan.blocked[0].snapshot["label_slug"] == "match-box-holder-with-pig-shaped-lighter"


def test_delete_is_blocked_when_card_has_data():
    """На карточке фото и описание — удалять нельзя, сначала слияние; запись просто переезжает."""
    data = [hall(6, "Аванзал",
                 showcase(20, 1, exhibit(122, "Набор пуговиц", image_url="https://s3/1.jpg",
                                         short_description="Золото, эмаль")),
                 showcase(53, None))]
    plan = orphans.classify_orphans(data, delete_ids=[122])
    assert plan.deletions == []
    assert ids(plan.moves) == [122]
    assert plan.blocked[0].marks == ("фото", "описание")


def test_duplicate_match_carries_marks_of_the_numbered_card():
    """В отчёт идут метки ОБЕИХ карточек: в проде данные обычно на сироте, а номер — пустышка."""
    data = [hall(5, "Золотая гостиная",
                 showcase(30, 1,
                          exhibit(7, "Пасхальное яйцо «Ренессанс»", label_slug="renaissance-egg",
                                  thumbnail_url="https://s3/7.jpg"),
                          exhibit(531, "Пасхальное яйцо-шкатулка «Ренессанс»", "1")),
                 showcase(44, None))]
    act = orphans.classify_orphans(data).actions[0]
    assert act.marks == ("фото", "распознавание")
    assert act.match.marks == ()          # у пронумерованного «оригинала» данных нет


def test_delete_id_outside_orphans_is_reported_not_executed():
    """id из чужого зала или с опечаткой не должен снести живой пронумерованный экспонат."""
    data = [hall(6, "Аванзал", showcase(20, 1, exhibit(122, "Набор пуговиц"),
                                        exhibit(637, "Комплект марок", "7")), showcase(53, None))]
    plan = orphans.classify_orphans(data, delete_ids=[637, 999])
    assert plan.deletions == []
    assert plan.unknown_delete_ids == [637, 999]


def test_media_marks_lists_everything_worth_saving():
    assert orphans.media_marks({"id": 1}) == ()
    assert orphans.media_marks({"thumbnail_url": "u"}) == ("фото",)
    assert orphans.media_marks({"raw_history": "x", "audio_url": "y"}) == ("описание", "озвучка")
    assert orphans.media_marks({"label_slug": "kruzhka"}) == ("распознавание",)
    assert orphans.media_marks({"master_name": "К. Фаберже"}) == ("каталожные поля",)


# ── Итоговая таблица по залам ───────────────────────────────────────────────────────────────
def test_hall_stats_count_only_numbered_showcases():
    data = [hall(10, "Верхняя буфетная",
                 showcase(4, 1, exhibit(225, "Ваза")),
                 showcase(92, None, exhibit(15, "Берег моря", "5")))]
    stat = orphans.classify_orphans(data).halls[0]
    assert (stat.before, stat.after, stat.moved, stat.emptied) == (1, 0, 1, 1)


def test_halls_without_orphans_are_absent_from_the_table():
    data = [hall(11, "Бежевый зал", showcase(70, 1, exhibit(1, "Часы", "1")), showcase(107, None))]
    assert orphans.classify_orphans(data).halls == []


# ── Разбор списка id ────────────────────────────────────────────────────────────────────────
def test_parse_delete_ids_accepts_commas_and_spaces():
    assert orphans._parse_delete_ids("122, 157;161", None) == [122, 157, 161]
    assert orphans._parse_delete_ids(None, None) == []


# ── Разрушающая половина: apply_plan / run_rollback / run поверх фейкового API ───────────────
class FakeApi:
    """Мини-каталог в памяти вместо сети: скрипт ходит через модульный orphans.api.

    Умеет ровно те ручки, что дёргает скрипт, и ведёт себя как боевой API в главном: DELETE
    непустой витрины без ``force`` — 409, а с ``force`` уносит содержимое каскадом. Именно на
    этом держится п.4 («витрина уходит, экспонаты остаются»), и проверить это без фейка нельзя.
    """

    def __init__(self, halls_data) -> None:
        self.showcases: dict = {}
        self.exhibits: dict = {}
        self.calls: list = []
        self.fail_patch: set = set()      # id экспонатов, PATCH по которым отвечает 500
        self.admin_read = 200             # код ответа GET /admin/exhibits/{id}
        self._next = 900
        for entry in halls_data:
            hall_id = entry["hall"]["id"]
            for group in entry["showcases"]:
                sc = dict(group["showcase"], hall_id=hall_id)
                self.showcases[sc["id"]] = sc
                for ex in group.get("exhibits") or []:
                    self.exhibits[ex["id"]] = dict(ex, showcase_id=sc["id"])

    @staticmethod
    def _page(items, query):
        params = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
        limit, offset = int(params.get("limit", 100)), int(params.get("offset", 0))
        return 200, {"items": items[offset:offset + limit], "total": len(items)}

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        head, _, query = path.partition("?")
        parts = head.strip("/").split("/")

        if method == "POST" and head == "/admin/showcases":
            self._next += 1
            sc = {"id": self._next, "hall_id": body["hall_id"],
                  "showcase_number": body.get("showcase_number"), "name": body.get("name")}
            self.showcases[sc["id"]] = sc
            return 201, sc
        if head.startswith("/admin/exhibits/"):
            ex_id = int(parts[-1])
            if method == "GET":
                if self.admin_read != 200:
                    return self.admin_read, {"detail": "нет доступа"}
                return (200, self.exhibits[ex_id]) if ex_id in self.exhibits else (404, {})
            if method == "DELETE":
                self.exhibits.pop(ex_id, None)
                return 204, {}
            if method == "PATCH":
                if ex_id in self.fail_patch:
                    return 500, {"detail": "БД недоступна"}
                self.exhibits[ex_id]["showcase_id"] = body["showcase_id"]
                return 200, self.exhibits[ex_id]
        if method == "GET" and head.startswith("/exhibits/"):
            ex = self.exhibits.get(int(parts[-1]))
            return (200, {"id": ex["id"], "showcase": {"id": ex["showcase_id"]}}) if ex else (404, {})
        if method == "GET" and head.startswith("/showcases/") and head.endswith("/exhibits"):
            sc_id = int(parts[1])
            if sc_id not in self.showcases:
                return 404, {}
            return self._page([e for e in self.exhibits.values() if e["showcase_id"] == sc_id], query)
        if method == "GET" and head.startswith("/halls/") and head.endswith("/showcases"):
            hall_id = int(parts[1])
            return self._page([s for s in self.showcases.values() if s["hall_id"] == hall_id], query)
        if method == "DELETE" and head.startswith("/admin/showcases/"):
            sc_id = int(parts[-1])
            if sc_id not in self.showcases:
                return 404, {}
            inside = [e for e in self.exhibits.values() if e["showcase_id"] == sc_id]
            if inside and "force=true" not in query:
                return 409, {"detail": f"Витрина не пуста: экспонатов — {len(inside)}."}
            for ex in inside:
                del self.exhibits[ex["id"]]
            del self.showcases[sc_id]
            return 204, {}
        raise AssertionError(f"фейковый API не знает {method} {path}")

    def catalog(self, halls_meta):
        """Пересобрать снимок в форме fetch_catalog — чтобы прогнать classify_orphans повторно."""
        return [
            {"hall": meta,
             "showcases": [
                 {"showcase": sc,
                  "exhibits": [e for e in self.exhibits.values() if e["showcase_id"] == sc["id"]]}
                 for sc in self.showcases.values() if sc["hall_id"] == meta["id"]
             ]}
            for meta in halls_meta
        ]


def with_api(fake, func):
    """Выполнить func с подменённым сетевым слоем и вернуть (результат, напечатанное)."""
    saved, buffer = orphans.api, io.StringIO()
    orphans.api = fake
    try:
        with contextlib.redirect_stdout(buffer):
            return func(), buffer.getvalue()
    finally:
        orphans.api = saved


def _rollback_path() -> str:
    return os.path.join(tempfile.mkdtemp(), "rollback.json")


def _args(**over):
    """Аргументы CLI со значениями по умолчанию — для прогона run() без argparse."""
    import argparse
    base = dict(rollback=None, delete_ids=None, delete_ids_file=None, similarity=0.80,
                include_service=False, apply=False, drop_empty_showcases=False,
                rollback_file=_rollback_path())
    base.update(over)
    return argparse.Namespace(**base)


def _buffet(**orphan_fields) -> list:
    """Верхняя буфетная из п.4: витрина №1 с единственной записью без номера и группа рядом."""
    return [hall(10, "Верхняя буфетная",
                 showcase(4, 1, exhibit(225, "Ваза с изображением цветов", **orphan_fields)),
                 showcase(92, None, exhibit(15, "Берег моря", "5")))]


def test_apply_moves_orphan_and_second_run_finds_nothing():
    """Полный цикл: перенос применён — повторный разбор того же каталога даёт пустой план."""
    data = _buffet()
    fake = FakeApi(data)
    plan = orphans.classify_orphans(data)
    errors, _ = with_api(fake, lambda: orphans.apply_plan(plan, False, _rollback_path()))
    assert errors == 0
    assert fake.exhibits[225]["showcase_id"] == 92
    again = orphans.classify_orphans(fake.catalog([data[0]["hall"]]))
    assert again.actions == []


def test_missing_group_is_created_before_the_move():
    """В Белой гостиной группы нет — сперва POST витрины, только потом PATCH экспоната."""
    data = [hall(7, "Белая гостиная", showcase(60, 1, exhibit(500, "Дамская сумочка")))]
    fake = FakeApi(data)
    plan = orphans.classify_orphans(data)
    errors, _ = with_api(fake, lambda: orphans.apply_plan(plan, False, _rollback_path()))
    methods = [(m, p) for m, p, _ in fake.calls]
    assert errors == 0
    assert methods.index(("POST", "/admin/showcases")) < methods.index(("PATCH", "/admin/exhibits/500"))
    assert fake.showcases[fake.exhibits[500]["showcase_id"]]["showcase_number"] is None


def test_empty_showcase_is_deleted_without_force_and_after_fresh_check():
    """П.4: витрина уходит, экспонат остаётся. force=true унёс бы его каскадом — его быть не должно."""
    data = _buffet()
    fake = FakeApi(data)
    plan = orphans.classify_orphans(data)
    errors, _ = with_api(fake, lambda: orphans.apply_plan(plan, True, _rollback_path()))
    check = fake.calls.index(("GET", "/showcases/4/exhibits?limit=1", None))
    drop = fake.calls.index(("DELETE", "/admin/showcases/4", None))
    assert errors == 0 and check < drop            # перечитали витрину и только потом удалили
    assert not any("force" in path for _, path, _ in fake.calls)
    assert 4 not in fake.showcases and 225 in fake.exhibits


def test_failed_move_keeps_the_showcase_alive():
    """PATCH упал — экспонат остался в витрине, значит витрину удалять нельзя ни в коем случае."""
    data = _buffet()
    fake = FakeApi(data)
    fake.fail_patch.add(225)
    plan = orphans.classify_orphans(data)
    errors, printed = with_api(fake, lambda: orphans.apply_plan(plan, True, _rollback_path()))
    assert errors >= 1 and "ПРОПУСК витрины id=4" in printed
    assert 4 in fake.showcases and fake.exhibits[225]["showcase_id"] == 4


def test_rollback_returns_everything_and_repeats_without_harm():
    """Откат возвращает экспонат в свою витрину, пересоздаёт её и повторно ничего не портит."""
    data = _buffet()
    fake = FakeApi(data)
    path = _rollback_path()
    plan = orphans.classify_orphans(data)
    with_api(fake, lambda: orphans.apply_plan(plan, True, path))
    assert 4 not in fake.showcases

    with_api(fake, lambda: orphans.run_rollback(path, apply=True))
    restored = fake.exhibits[225]["showcase_id"]
    assert fake.showcases[restored]["showcase_number"] == 1     # витрина №1 восстановлена
    assert 92 in fake.showcases                                 # чужую группу откат не трогает

    with_api(fake, lambda: orphans.run_rollback(path, apply=True))
    assert fake.exhibits[225]["showcase_id"] == restored         # второй откат — no-op


def test_rollback_does_not_touch_a_card_moved_by_hand():
    """После прогона экспонат переложили руками — откат обязан оставить его в покое."""
    data = _buffet()
    fake = FakeApi(data)
    path = _rollback_path()
    with_api(fake, lambda: orphans.apply_plan(orphans.classify_orphans(data), False, path))
    fake.exhibits[225]["showcase_id"] = 4                        # «музей вернул сам»
    errors, printed = with_api(fake, lambda: orphans.run_rollback(path, apply=True))
    assert errors == 0 and "Возвращено в исходные витрины: 0" in printed


def test_run_cancels_deletion_when_cards_are_not_fully_read():
    """Админ-доступа нет → «данных нет» может означать «данные не прочитались». Удаление отменяется."""
    data = _buffet(label_slug="vaza-s-czvetami")
    fake = FakeApi(data)
    fake.admin_read = 401
    args = _args(delete_ids="225", apply=True)
    saved_fetch = orphans.fetch_catalog
    orphans.fetch_catalog = lambda: data
    try:
        code, _ = with_api(fake, lambda: orphans.run(args))
    finally:
        orphans.fetch_catalog = saved_fetch
    assert code == 1
    assert 225 in fake.exhibits
    assert not any(m == "DELETE" for m, _, _ in fake.calls)


def test_rollback_file_keeps_the_whole_deleted_card():
    """Файл отката должен нести карточку целиком: по id и названию её не восстановить."""
    data = [hall(6, "Аванзал", showcase(20, 1, exhibit(122, "Набор пуговиц")), showcase(53, None))]
    fake = FakeApi(data)
    path = _rollback_path()
    plan = orphans.classify_orphans(data, delete_ids=[122])
    with_api(fake, lambda: orphans.apply_plan(plan, False, path))
    with open(path, encoding="utf-8") as fh:
        log = json.load(fh)
    assert 122 not in fake.exhibits
    assert log["deleted_exhibits"][0]["card"] == {"id": 122, "name": "Набор пуговиц"}


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
