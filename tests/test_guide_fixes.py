"""Юнит-тесты точечных правок каталога по путеводителю (баг-репорт 12.08.2026, п.4, п.6, п.7.1).

Проверяется scripts/apply_guide_fixes_20260812.py целиком:
  • чистое ядро — разбор декларативного файла правок и раскладка по статусам (снимок карточек
    словарями);
  • РАЗРУШАЮЩАЯ половина — apply_plan/run_rollback/run поверх фейкового API (класс FakeApi).
Второе здесь важнее обычного: цена ошибки — затёртая карточка на проде, а главная защита
(сверка expect_current) срабатывает именно в момент записи. FakeApi воспроизводит и поведение
бэкенда, из-за которого правка описания перегенерирует озвучку через LLM (E15): без этого
нельзя проверить, что откат возвращает исходную озвучку, а не машинную.

Отдельным тестом читается настоящий db/guide_fixes_20260812.json — файл правок такая же часть
поставки, как код, и опечатка в нём стоит столько же. БД и сеть не нужны. Запуск:
    python -m pytest tests/test_guide_fixes.py
    python tests/test_guide_fixes.py     # standalone
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

import apply_guide_fixes_20260812 as guide  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXES_FILE = os.path.join(REPO_ROOT, "db", "guide_fixes_20260812.json")


# ── Сборка входных данных ───────────────────────────────────────────────────────────────────
def fix(exhibit_id=144, field_name="master_name", expect="Алексей Иванов", value="Карл Фаберже",
        **extra) -> dict:
    """Одна строка файла правок в том виде, в каком её пишет человек."""
    row = {
        "exhibit_id": exhibit_id,
        "exhibit": "Портсигар",
        "where": {"hall": "Золотая гостиная", "showcase": "01", "exhibit_number": "12"},
        "field": field_name,
        "expect_current": expect,
        "value": value,
        "printed_page": 45,
        "quote": "12 Портсигар • Москва, 1908–1917 • Фирма К. Фаберже",
        "verdict": "подтверждена",
        "reason": "Атрибуция приехала от соседней позиции №15.",
    }
    row.update(extra)
    return row


def doc(*fixes, rejected=(), eye=()) -> dict:
    return {"fixes": list(fixes), "rejected": list(rejected), "needs_eye_check": list(eye)}


def write_doc(document: dict) -> str:
    path = os.path.join(tempfile.mkdtemp(), "fixes.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(document, fh, ensure_ascii=False)
    return path


def card(exhibit_id=144, **fields) -> dict:
    base = {"id": exhibit_id, "name": "Портсигар", "master_name": None, "material": None,
            "year_created": None, "dating": None, "short_description": None,
            "short_description_spoken": None, "exhibit_number": "12"}
    base.update(fields)
    return base


def load_plan(document: dict, cards: dict) -> guide.Plan:
    fixes, rejected, eye = guide.load_fixes(write_doc(document))
    return guide.build_plan(fixes, cards, rejected, eye)


def statuses(plan: guide.Plan) -> list:
    return [(f.exhibit_id, f.field_name, f.status) for f in plan.fixes]


# ── Разбор файла правок ─────────────────────────────────────────────────────────────────────
def test_forbidden_field_is_a_hard_error():
    """label_slug — класс распознавания: правка на него должна ронять скрипт, а не пропускаться.

    Тихий пропуск был бы худшим исходом: отчёт покажет строку, музей решит, что распознавание
    перенастроено, а на деле не изменилось ничего.
    """
    path = write_doc(doc(fix(field_name="label_slug", expect="a", value="b")))
    try:
        guide.load_fixes(path)
    except SystemExit as exc:
        assert "label_slug" in str(exc)
    else:
        raise AssertionError("правка на label_slug должна быть отвергнута при разборе")


def test_unknown_field_is_a_hard_error():
    path = write_doc(doc(fix(field_name="raw_history", expect="a", value="b")))
    try:
        guide.load_fixes(path)
    except SystemExit as exc:
        assert "raw_history" in str(exc)
    else:
        raise AssertionError("неизвестное поле должно быть отвергнуто")


def test_fix_without_quote_is_rejected():
    """Заказчик получает список замен с цитатами — правка без источника в него попасть не может."""
    path = write_doc(doc(fix(quote="")))
    try:
        guide.load_fixes(path)
    except SystemExit as exc:
        assert "цитаты" in str(exc)
    else:
        raise AssertionError("правка без цитаты должна быть отвергнута")


def test_same_field_twice_is_rejected():
    """Две правки одного поля — молча потерянная правка: в PATCH уедет только последняя."""
    path = write_doc(doc(fix(value="Карл Фаберже"), fix(value="Михаил Перхин")))
    try:
        guide.load_fixes(path)
    except SystemExit as exc:
        assert "дважды" in str(exc)
    else:
        raise AssertionError("дубль правки должен быть отвергнут")


def test_empty_string_and_null_are_the_same_absence():
    """Админка оставляет пустую строку, API отдаёт null — это одно «значения нет»."""
    plan = load_plan(doc(fix(expect=None, value="Алексей Иванов")), {144: card(master_name="  ")})
    assert statuses(plan) == [(144, "master_name", guide.WRITE)]


# ── Раскладка по статусам ───────────────────────────────────────────────────────────────────
def test_matching_expect_current_goes_to_write():
    plan = load_plan(doc(fix()), {144: card(master_name="Алексей Иванов")})
    assert statuses(plan) == [(144, "master_name", guide.WRITE)]
    assert plan.to_write[0].current == "Алексей Иванов"


def test_target_value_already_on_card_is_done():
    """Фронт внёс правку руками 12.08 — показываем «уже применено» и ничего не шлём."""
    plan = load_plan(doc(fix()), {144: card(master_name="Карл Фаберже")})
    assert statuses(plan) == [(144, "master_name", guide.DONE)]
    assert plan.to_write == []


def test_front_applied_fix_has_expect_equal_to_value():
    """Строки вида «expect_current == value» (id 163 material, id 134/124 год) — всегда DONE.

    Порядок проверок в build_plan не случаен: если сравнивать сначала с expect_current, такая
    правка попадала бы в план записи при КАЖДОМ прогоне и слала бы PATCH с тем же значением.
    """
    plan = load_plan(
        doc(fix(field_name="year_created", expect=1904, value=1904)),
        {144: card(year_created=1904)},
    )
    assert statuses(plan) == [(144, "year_created", guide.DONE)]


def test_changed_state_goes_to_conflict_and_is_not_written():
    """Значение правили после разбора — не наше дело его затирать."""
    plan = load_plan(doc(fix()), {144: card(master_name="Кто-то третий")})
    assert statuses(plan) == [(144, "master_name", guide.CONFLICT)]
    assert plan.to_write == []


def test_unavailable_card_goes_to_missing():
    plan = load_plan(doc(fix()), {144: None})
    assert statuses(plan) == [(144, "master_name", guide.MISSING)]


def test_patches_group_by_exhibit():
    """Один PATCH на карточку, а не на поле: иначе бэкенд перегенерирует озвучку дважды."""
    plan = load_plan(
        doc(fix(), fix(field_name="year_created", expect=1911, value=1908),
            fix(exhibit_id=540, field_name="material", expect=None, value="Золото")),
        {144: card(master_name="Алексей Иванов", year_created=1911), 540: card(540)},
    )
    grouped = plan.patches()
    assert list(grouped) == [144, 540]
    assert [f.field_name for f in grouped[144]] == ["master_name", "year_created"]


# ── Фейковый API ────────────────────────────────────────────────────────────────────────────
class FakeApi:
    """Мини-каталог в памяти вместо сети: скрипт ходит через модульный guide.api.

    Ведёт себя как боевой бэкенд в двух местах, на которых всё и держится:
      • публичный GET /exhibits/{id} НЕ отдаёт short_description_spoken (как sch.Exhibit);
      • PATCH с short_description и без short_description_spoken перегенерирует озвучку —
        это _autofill_spoken (E15). LLM заменён на детерминированную заглушку.
    """

    def __init__(self, cards) -> None:
        self.cards = {c["id"]: dict(c) for c in cards}
        self.calls: list = []
        self.admin_read = 200            # код ответа GET /admin/exhibits/{id}
        self.fail_patch: set = set()     # id, PATCH по которым отвечает 500

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        head = path.partition("?")[0]
        parts = head.strip("/").split("/")

        if head.startswith("/admin/exhibits/"):
            ex_id = int(parts[-1])
            if method == "GET":
                if self.admin_read != 200:
                    return self.admin_read, {"detail": "нет доступа"}
                return (200, dict(self.cards[ex_id])) if ex_id in self.cards else (404, {})
            if method == "PATCH":
                if ex_id in self.fail_patch:
                    return 500, {"detail": "БД недоступна"}
                if ex_id not in self.cards:
                    return 404, {}
                card_now = self.cards[ex_id]
                card_now.update(body)
                if "short_description" in body and "short_description_spoken" not in body:
                    card_now["short_description_spoken"] = f"LLM({body['short_description']})"
                return 200, dict(card_now)
        if method == "GET" and head.startswith("/exhibits/"):
            ex_id = int(parts[-1])
            if ex_id not in self.cards:
                return 404, {}
            public = dict(self.cards[ex_id])
            public.pop("short_description_spoken", None)      # публичная схема его не отдаёт
            return 200, public
        raise AssertionError(f"фейковый API не знает {method} {path}")

    def patch_calls(self) -> list:
        return [(p, b) for m, p, b in self.calls if m == "PATCH"]


def with_api(fake, func):
    """Подменить сетевой слой и проглотить печать: тесты проверяют состояние, а не вывод."""
    saved = guide.api
    guide.api = fake
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return func()
    finally:
        guide.api = saved


def _args(**over) -> argparse.Namespace:
    base = dict(apply=False, fixes_file=None, ids=None, report_file=None,
                rollback_file=None, rollback=None, max_print=200)
    base.update(over)
    return argparse.Namespace(**base)


def _rollback_path() -> str:
    return os.path.join(tempfile.mkdtemp(), "rollback.json")


# ── Применение ──────────────────────────────────────────────────────────────────────────────
def test_apply_writes_only_planned_fields():
    fake = FakeApi([card(master_name="Алексей Иванов", year_created=1911, material="Кость")])
    path = write_doc(doc(fix(), fix(field_name="year_created", expect=1911, value=1908)))
    code = with_api(fake, lambda: guide.run(_args(fixes_file=path, apply=True,
                                                  rollback_file=_rollback_path())))
    assert code == 0
    assert fake.patch_calls() == [("/admin/exhibits/144",
                                   {"master_name": "Карл Фаберже", "year_created": 1908})]
    assert fake.cards[144]["material"] == "Кость"          # непланируемое поле не тронуто


def test_apply_is_idempotent_second_run_sends_nothing():
    """Требование ТЗ: повторный прогон не должен слать ни одного PATCH."""
    fake = FakeApi([card(master_name="Алексей Иванов")])
    path = write_doc(doc(fix()))
    with_api(fake, lambda: guide.run(_args(fixes_file=path, apply=True, rollback_file=_rollback_path())))
    assert len(fake.patch_calls()) == 1
    code = with_api(fake, lambda: guide.run(_args(fixes_file=path, apply=True,
                                                  rollback_file=_rollback_path())))
    assert code == 0
    assert len(fake.patch_calls()) == 1                    # новых PATCH не появилось


def test_apply_refuses_when_expect_current_does_not_match():
    """Главная защита: карточку правили после разбора — PATCH не уходит, прогон сигналит 1."""
    fake = FakeApi([card(master_name="Кто-то третий")])
    path = write_doc(doc(fix()))
    code = with_api(fake, lambda: guide.run(_args(fixes_file=path, apply=True,
                                                  rollback_file=_rollback_path())))
    assert code == 1
    assert fake.patch_calls() == []
    assert fake.cards[144]["master_name"] == "Кто-то третий"


def test_conflict_does_not_block_the_other_fixes():
    """Одна разъехавшаяся карточка не должна отменять правки остальных."""
    fake = FakeApi([card(master_name="Кто-то третий"), card(540, master_name=None)])
    path = write_doc(doc(fix(), fix(exhibit_id=540, expect=None, value="Алексей Иванов")))
    code = with_api(fake, lambda: guide.run(_args(fixes_file=path, apply=True,
                                                  rollback_file=_rollback_path())))
    assert code == 1                                       # расхождение осталось нерешённым
    assert fake.patch_calls() == [("/admin/exhibits/540", {"master_name": "Алексей Иванов"})]


def test_apply_is_blocked_without_admin_read():
    """Без админского чтения не снять озвучку — файл отката будет неполным, применять нельзя."""
    fake = FakeApi([card(master_name="Алексей Иванов")])
    fake.admin_read = 401
    path = write_doc(doc(fix()))
    code = with_api(fake, lambda: guide.run(_args(fixes_file=path, apply=True,
                                                  rollback_file=_rollback_path())))
    assert code == 1
    assert fake.patch_calls() == []


def test_dry_run_works_without_admin_token():
    """Сухой прогон на проде мы делаем без токена — он обязан отработать по публичной ручке."""
    fake = FakeApi([card(master_name="Алексей Иванов")])
    fake.admin_read = 403
    path = write_doc(doc(fix()))
    code = with_api(fake, lambda: guide.run(_args(fixes_file=path)))
    assert code == 0
    assert fake.patch_calls() == []


def test_patch_failure_is_reported_and_rollback_file_still_written():
    fake = FakeApi([card(master_name="Алексей Иванов")])
    fake.fail_patch = {144}
    path = write_doc(doc(fix()))
    rollback = _rollback_path()
    code = with_api(fake, lambda: guide.run(_args(fixes_file=path, apply=True, rollback_file=rollback)))
    assert code == 1
    with open(rollback, encoding="utf-8") as fh:
        assert json.load(fh)["items"] == []                # неудавшийся PATCH в откат не попал


# ── Откат ───────────────────────────────────────────────────────────────────────────────────
def test_rollback_restores_original_values():
    fake = FakeApi([card(master_name="Алексей Иванов", year_created=1911)])
    path = write_doc(doc(fix(), fix(field_name="year_created", expect=1911, value=1908)))
    rollback = _rollback_path()
    with_api(fake, lambda: guide.run(_args(fixes_file=path, apply=True, rollback_file=rollback)))
    assert fake.cards[144]["master_name"] == "Карл Фаберже"

    code = with_api(fake, lambda: guide.run(_args(rollback=rollback, apply=True)))
    assert code == 0
    assert fake.cards[144]["master_name"] == "Алексей Иванов"
    assert fake.cards[144]["year_created"] == 1911


def test_rollback_restores_spoken_description_too():
    """Правка описания перегенерирует озвучку через LLM — откат обязан вернуть исходную.

    Иначе на карточке останется машинный текст, которого до прогона не было, и потеря ручной
    озвучки музея файлом отката не покрывается.
    """
    fake = FakeApi([card(short_description="Старое описание",
                         short_description_spoken="Ручная озвучка музея")])
    path = write_doc(doc(fix(field_name="short_description",
                             expect="Старое описание", value="Новое описание")))
    rollback = _rollback_path()
    with_api(fake, lambda: guide.run(_args(fixes_file=path, apply=True, rollback_file=rollback)))
    assert fake.cards[144]["short_description_spoken"] == "LLM(Новое описание)"

    with_api(fake, lambda: guide.run(_args(rollback=rollback, apply=True)))
    assert fake.cards[144]["short_description"] == "Старое описание"
    assert fake.cards[144]["short_description_spoken"] == "Ручная озвучка музея"


def test_rollback_is_idempotent():
    fake = FakeApi([card(master_name="Алексей Иванов")])
    path = write_doc(doc(fix()))
    rollback = _rollback_path()
    with_api(fake, lambda: guide.run(_args(fixes_file=path, apply=True, rollback_file=rollback)))
    with_api(fake, lambda: guide.run(_args(rollback=rollback, apply=True)))
    before = len(fake.patch_calls())
    with_api(fake, lambda: guide.run(_args(rollback=rollback, apply=True)))
    assert len(fake.patch_calls()) == before               # второй откат ничего не шлёт


def test_rollback_skips_field_edited_after_the_run():
    """Значение правили руками уже после прогона — молча затирать чужую правку нельзя."""
    fake = FakeApi([card(master_name="Алексей Иванов")])
    path = write_doc(doc(fix()))
    rollback = _rollback_path()
    with_api(fake, lambda: guide.run(_args(fixes_file=path, apply=True, rollback_file=rollback)))
    fake.cards[144]["master_name"] = "Правка музея"

    with_api(fake, lambda: guide.run(_args(rollback=rollback, apply=True)))
    assert fake.cards[144]["master_name"] == "Правка музея"


# ── Отчёт: секция «требует проверки глазами» ────────────────────────────────────────────────
def test_eye_check_section_is_printed_and_not_empty():
    """Прямой пункт DoD: фото и label_slug остались от прежнего предмета — нужен человек."""
    eye = [{
        "exhibit_id": 144,
        "exhibit_after_fix": "Портсигар (Золотая гостиная, витрина 01, №12)",
        "media": {"image_url": True, "images": 7, "label_slug": "portcigar-dly-cera-doycona"},
        "expected_subject": "Московский портсигар 1908–1917 гг. фирмы К. Фаберже",
        "risk": "Снимки от в.01 №15 — портсигара для сэра Доусона",
    }]
    fake = FakeApi([card(master_name="Алексей Иванов")])
    path = write_doc(doc(fix(), eye=eye))
    out = io.StringIO()
    saved = guide.api
    guide.api = fake
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            guide.run(_args(fixes_file=path))
    finally:
        guide.api = saved
    text = out.getvalue()
    assert "ТРЕБУЕТ ПРОВЕРКИ ГЛАЗАМИ" in text
    assert "portcigar-dly-cera-doycona" in text
    assert "Московский портсигар" in text
    assert fake.patch_calls() == []                        # секция ничего не правит


def test_report_file_json_carries_rejected_and_eye_checks():
    """Заказчик несёт файл в музей: решения «почему не применили» нужны не меньше применённых."""
    rejected = [{"exhibit_id": 67, "field": "master_name", "proposed": "Дмитрий Шелапутин",
                 "reason": "Та же строка стоит у соседей — привязка не доказана"}]
    eye = [{"exhibit_id": 72, "exhibit_after_fix": "Иконка «Спас Нерукотворный»",
            "expected_subject": "Петербургская иконка 1875–1900", "risk": "Фото от в.08 №1"}]
    fake = FakeApi([card(master_name="Алексей Иванов")])
    path = write_doc(doc(fix(), rejected=rejected, eye=eye))
    report = os.path.join(tempfile.mkdtemp(), "report.json")
    with_api(fake, lambda: guide.run(_args(fixes_file=path, report_file=report)))
    with open(report, encoding="utf-8") as fh:
        saved = json.load(fh)
    assert saved["summary"]["к записи"] == 1
    assert saved["rejected"][0]["exhibit_id"] == 67
    assert saved["needs_eye_check"][0]["exhibit_id"] == 72
    assert saved["fixes"][0]["current"] == "Алексей Иванов"


def test_ids_filter_narrows_the_plan():
    fake = FakeApi([card(master_name="Алексей Иванов"), card(540, master_name=None)])
    path = write_doc(doc(fix(), fix(exhibit_id=540, expect=None, value="Алексей Иванов")))
    with_api(fake, lambda: guide.run(_args(fixes_file=path, ids="540", apply=True,
                                           rollback_file=_rollback_path())))
    assert fake.patch_calls() == [("/admin/exhibits/540", {"master_name": "Алексей Иванов"})]


# ── Настоящий файл правок ───────────────────────────────────────────────────────────────────
def test_shipped_fixes_file_loads_and_is_consistent():
    """db/guide_fixes_20260812.json — часть поставки: он обязан читаться и быть непротиворечивым."""
    fixes, rejected, eye = guide.load_fixes(FIXES_FILE)
    assert len(fixes) == 47, len(fixes)
    assert len(rejected) == 1 and rejected[0]["exhibit_id"] == 67
    assert {e["exhibit_id"] for e in eye} == {67, 72, 73, 144}

    for f in fixes:
        assert f.printed_page and 1 <= f.printed_page <= 200, f.exhibit_id
        assert f.where.get("hall"), f.exhibit_id
        # Правка либо что-то меняет, либо документирует уже достигнутое состояние —
        # но никогда не «пишет пустоту поверх пустоты».
        assert not (f.expect_current is None and f.value is None), f.exhibit_id


def test_shipped_fixes_split_into_write_and_already_applied():
    """Восемь строк файла — уже применённое состояние (в т.ч. три правки фронта от 12.08)."""
    fixes, _, _ = guide.load_fixes(FIXES_FILE)
    # Снимок прода 12.08.2026 — это и есть expect_current каждой правки.
    cards: dict = {}
    for f in fixes:
        cards.setdefault(f.exhibit_id, {"id": f.exhibit_id})[f.field_name] = f.expect_current
    plan = guide.build_plan(fixes, cards)
    assert len(plan.to_write) == 39
    assert len(plan.by_status(guide.DONE)) == 8
    assert plan.by_status(guide.CONFLICT) == []
    done = {(f.exhibit_id, f.field_name) for f in plan.by_status(guide.DONE)}
    for key in ((163, "material"), (134, "year_created"), (124, "year_created")):
        assert key in done, key                       # правки, внесённые фронтом через админку


def test_shipped_fixes_restore_dating_for_the_two_rejected_rows():
    """Правки dating у id 144 и 124 отклоняли из-за отсутствия колонки — колонка появилась."""
    fixes, _, _ = guide.load_fixes(FIXES_FILE)
    dating = {f.exhibit_id: f for f in fixes if f.field_name == "dating"}
    assert set(dating) == {144, 124}
    assert dating[144].value == "1908–1917"
    assert dating[124].value == "1899–1903"
    for f in dating.values():
        assert f.expect_current is None
        assert "–" in f.value                    # тире диапазона — U+2013, как в указателе
        # Нижняя граница диапазона обязана лежать в year_created той же карточки.
        year = next((x for x in fixes if x.exhibit_id == f.exhibit_id and x.field_name == "year_created"), None)
        assert year is not None and str(year.value) == f.value.split("–")[0]


def test_shipped_fixes_never_touch_label_slug_or_media():
    """Жёсткое правило задачи: класс распознавания и фотографии — не наше дело."""
    fixes, _, _ = guide.load_fixes(FIXES_FILE)
    assert {f.field_name for f in fixes} <= set(guide.FIELD_TITLES)
    with open(FIXES_FILE, encoding="utf-8") as fh:
        raw = json.load(fh)
    for row in raw["fixes"]:
        assert row["field"] not in guide.FORBIDDEN_FIELDS


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
