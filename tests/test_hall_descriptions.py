"""Юнит-тесты заливки описаний залов (баг-репорт 12.08.2026, п.3).

Проверяется scripts/apply_hall_descriptions.py целиком:
  • чистое ядро — сопоставление файла с живым каталогом ПО ИМЕНИ (снимок залов словарями);
  • разрушающая половина — apply_plan/run_rollback/run поверх фейкового API (класс FakeApi).

Главный тест здесь — test_matching_survives_the_number_shift: старая версия скрипта
сопоставляла залы по `hall_number`, а после разделения Белой и Голубой гостиных нумерация
прода сдвинулась на +1 начиная с №8. Прогон по номеру залил бы описание Выставочного зала
в Голубую гостиную и сдвинул ещё три зала — молча и с кодом возврата 0.

БД и сеть не нужны: сетевой слой скрипта — единственная функция `api`, её подменяет FakeApi.
Запуск:
    python -m pytest tests/test_hall_descriptions.py
    python tests/test_hall_descriptions.py     # standalone
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import apply_hall_descriptions as halls  # noqa: E402

DESC_FILE = os.path.join(ROOT, "db", "hall_descriptions.json")


# ── Снимок прода 12.08.2026: тот самый сдвиг нумерации ──────────────────────────────────────
def live(hall_id: int, number, name: str, description=None, is_service: bool = False) -> dict:
    return {"id": hall_id, "hall_number": number, "name": name,
            "description": description, "is_service": is_service}


def prod_halls() -> list:
    """GET /halls?include_service=true, как он отвечает на проде.

    Ключевое: id 14 «Голубая гостиная» стоит под №8, а Выставочный (id 8) уехал на №9 —
    и дальше по списку каждый зал на единицу больше своего номера в старом сиде.
    """
    return [
        live(1, 1, "Парадная лестница", "Сохранившаяся до наших дней…", is_service=True),
        live(7, 7, "Белая гостиная", "СКЛЕЙКА: Белая\n\nГолубая"),
        live(14, 8, "Голубая гостиная", None),
        live(8, 9, "Выставочный зал", "Выставочный, старое"),
        live(9, 10, "Готический зал", "Готический, старое"),
        live(12, None, "Вне постоянной экспозиции", "Экспонаты каталога, не привязанные к залу."),
    ]


def entry(key: str, number, name: str, description: str) -> halls.Entry:
    return halls.Entry(key, number, name, description)


def file_entries() -> list:
    """db/hall_descriptions.json в разрезанном виде: у Голубой — свой текст и свой номер."""
    return [
        entry("1", 1, "Парадная лестница", "Лестница, текст путеводителя"),
        entry("7", 7, "Белая гостиная", "Белая, текст путеводителя"),
        entry("14", 8, "Голубая гостиная", "Голубая, текст путеводителя"),
        entry("8", 9, "Выставочный зал", "Выставочный, текст путеводителя"),
        entry("9", 10, "Готический зал", "Готический, старое"),
    ]


def by_hall_id(plan: halls.Plan) -> dict:
    return {upd.hall_id: upd.after for upd in plan.updates}


# ── Фейковый каталог ────────────────────────────────────────────────────────────────────────
class FakeApi:
    """Залы в памяти. Умеет ровно те ручки, что дёргает скрипт."""

    def __init__(self, halls_list) -> None:
        self.halls = {h["id"]: dict(h) for h in halls_list}
        self.calls: list = []
        self.patch_status = 200          # чем отвечать на PATCH (для проверки ветки ошибки)

    @staticmethod
    def _params(query: str) -> dict:
        return dict(p.split("=", 1) for p in query.split("&") if "=" in p)

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        head, _, query = path.partition("?")
        params = self._params(query)
        parts = head.strip("/").split("/")

        if method == "GET" and head == "/halls":
            # Служебные залы отдаём ТОЛЬКО с флагом — как публичная ручка (п.1 баг-репорта).
            include_service = params.get("include_service") == "true"
            items = [h for h in self.halls.values() if include_service or not h["is_service"]]
            items.sort(key=lambda h: (h["hall_number"] is None, h["hall_number"] or 0))
            limit, offset = int(params.get("limit", 100)), int(params.get("offset", 0))
            return 200, {"items": items[offset:offset + limit], "total": len(items)}
        if method == "GET" and len(parts) == 2 and parts[0] == "halls":
            hall = self.halls.get(int(parts[1]))
            return (200, hall) if hall else (404, {"detail": "Зал не найден."})
        if method == "PATCH" and len(parts) == 3 and parts[:2] == ["admin", "halls"]:
            if self.patch_status != 200:
                return self.patch_status, {"detail": "нет"}
            hall = self.halls[int(parts[2])]
            hall.update(body or {})
            return 200, hall
        raise AssertionError(f"неожиданный вызов API: {method} {path}")


def with_api(fake, func):
    """Выполнить func с подменённым сетевым слоем и вернуть (результат, напечатанное)."""
    saved, buffer = halls.api, io.StringIO()
    halls.api = fake
    try:
        with contextlib.redirect_stdout(buffer):
            return func(), buffer.getvalue()
    finally:
        halls.api = saved


def _rollback_path() -> str:
    return os.path.join(tempfile.mkdtemp(), "rollback.json")


def _args(**over):
    """Аргументы CLI со значениями по умолчанию — для прогона run() без argparse."""
    base = dict(rollback=None, apply=False, file=DESC_FILE,
                report_file=None, rollback_file=_rollback_path())
    base.update(over)
    return argparse.Namespace(**base)


# ── Сопоставление по имени ──────────────────────────────────────────────────────────────────
def test_matching_survives_the_number_shift():
    """Тот самый дефект: по номеру Выставочный уехал бы в Голубую гостиную. По имени — нет."""
    plan = halls.build_plan(file_entries(), prod_halls())
    landed = by_hall_id(plan)
    assert landed[14] == "Голубая, текст путеводителя"      # id 14 = №8 на проде
    assert landed[8] == "Выставочный, текст путеводителя"   # id 8 = №9 на проде
    assert landed[7] == "Белая, текст путеводителя"
    assert plan.unmatched == [] and plan.ambiguous == []


def test_number_mismatch_is_only_a_hint():
    """Номер разошёлся с продом — это подсказка в отчёте, а не причина промахнуться."""
    file_entry = entry("8", 8, "Выставочный зал", "Выставочный, текст путеводителя")  # старый №8
    plan = halls.build_plan([file_entry], prod_halls())
    upd = plan.updates[0]
    assert (upd.hall_id, upd.live_number, upd.file_number) == (8, 9, 8)
    assert upd.number_moved
    report = io.StringIO()
    with contextlib.redirect_stdout(report):
        halls.print_report(plan, apply=False)
    assert "сопоставили по имени" in report.getvalue()


def test_name_matching_ignores_case_yo_and_spaces():
    """Нормализация имени: регистр, «ё/е» и лишние пробелы сопоставлению не мешают."""
    live_halls = [live(10, 11, "Верхняя Буфетная")]
    plan = halls.build_plan([entry("10", 11, "  вЕрхняя   буфётная ", "текст")], live_halls)
    assert by_hall_id(plan) == {10: "текст"}


def test_unknown_name_is_skipped_loudly():
    """Зала с таким именем на проде нет — не трогаем ничего и говорим об этом громко."""
    plan = halls.build_plan([entry("99", 99, "Зал слоновой кости", "текст")], prod_halls())
    assert plan.updates == [] and [e.name for e in plan.unmatched] == ["Зал слоновой кости"]
    report = io.StringIO()
    with contextlib.redirect_stdout(report):
        halls.print_report(plan, apply=False)
    assert "НЕ НАЙДЕНЫ" in report.getvalue() and "Зал слоновой кости" in report.getvalue()


def test_namesakes_are_never_guessed():
    """Два зала с одним именем — выбирать наугад нельзя, пропускаем оба."""
    live_halls = [live(20, 5, "Золотая гостиная"), live(21, 6, "Золотая гостиная")]
    plan = halls.build_plan([entry("5", 5, "Золотая гостиная", "текст")], live_halls)
    assert plan.updates == [] and [e.name for e in plan.ambiguous] == ["Золотая гостиная"]


def test_hall_without_file_entry_is_reported_but_not_touched():
    """«Вне постоянной экспозиции» в файле нет — это не ошибка, просто к сведению."""
    plan = halls.build_plan(file_entries(), prod_halls())
    assert [h["id"] for h in plan.extra_live] == [12]


def test_already_matching_description_is_idempotent():
    """Готический зал на проде уже с нужным текстом — в план он не попадает."""
    plan = halls.build_plan(file_entries(), prod_halls())
    assert 9 not in by_hall_id(plan)
    assert [upd.hall_id for upd in plan.unchanged] == [9]


# ── Служебные залы ──────────────────────────────────────────────────────────────────────────
def test_service_hall_is_fetched_only_with_the_flag():
    """Зал №1 «Парадная лестница» служебный: без include_service его описание не залить никогда."""
    fake = FakeApi(prod_halls())
    fetched, _ = with_api(fake, halls.fetch_halls)
    assert 1 in [h["id"] for h in fetched]
    assert all("include_service=true" in path for _, path, _ in fake.calls)

    # Тот же файл против ПУБЛИЧНОГО списка (как читала прошлая версия) — лестница теряется.
    public = [h for h in prod_halls() if not h["is_service"]]
    plan = halls.build_plan(file_entries(), public)
    assert [e.name for e in plan.unmatched] == ["Парадная лестница"]


# ── Применение, повторный прогон, откат ─────────────────────────────────────────────────────
def test_apply_writes_descriptions_and_second_run_is_empty():
    """Полный цикл: заливка применена — повторный разбор того же каталога даёт пустой план."""
    fake = FakeApi(prod_halls())
    plan = halls.build_plan(file_entries(), fake.__call__("GET", "/halls?include_service=true")[1]["items"])
    with_api(fake, lambda: halls.apply_plan(plan, _rollback_path()))
    assert fake.halls[14]["description"] == "Голубая, текст путеводителя"
    assert fake.halls[8]["description"] == "Выставочный, текст путеводителя"

    again, _ = with_api(fake, lambda: halls.build_plan(file_entries(), halls.fetch_halls()))
    assert again.updates == []
    assert len(again.unchanged) == 5


def test_run_is_dry_by_default():
    """Без --apply скрипт не пишет ничего: прошлая версия делала наоборот."""
    fake = FakeApi(prod_halls())
    before = {i: h.get("description") for i, h in fake.halls.items()}
    saved = halls.load_entries
    halls.load_entries = lambda path=None: file_entries()
    try:
        code, out = with_api(fake, lambda: halls.run(_args()))
    finally:
        halls.load_entries = saved
    assert code == 0
    assert not any(m == "PATCH" for m, _, _ in fake.calls)
    assert {i: h.get("description") for i, h in fake.halls.items()} == before
    assert "сухой прогон" in out


def test_run_returns_error_code_when_a_hall_is_missing():
    """Ненайденный зал — невыполненная работа, прогон не должен сойти за успешный."""
    fake = FakeApi([h for h in prod_halls() if h["id"] != 14])
    saved = halls.load_entries
    halls.load_entries = lambda path=None: file_entries()
    try:
        code, out = with_api(fake, lambda: halls.run(_args(apply=True)))
    finally:
        halls.load_entries = saved
    assert code == 1
    assert "Голубая гостиная" in out


def test_rollback_returns_the_original_null():
    """Откат обязан вернуть NULL, а не пустую строку: у Голубой описания не было вовсе."""
    fake = FakeApi(prod_halls())
    path = _rollback_path()
    plan, _ = with_api(fake, lambda: halls.build_plan(file_entries(), halls.fetch_halls()))
    with_api(fake, lambda: halls.apply_plan(plan, path))
    log = json.load(open(path, encoding="utf-8"))
    assert {i["hall_id"]: i["before"] for i in log["items"]}[14] is None

    with_api(fake, lambda: halls.run_rollback(path, apply=True))
    assert fake.halls[14]["description"] is None
    assert fake.halls[7]["description"] == "СКЛЕЙКА: Белая\n\nГолубая"


def test_rollback_is_dry_by_default_and_repeatable():
    """Откат без --apply ничего не пишет; повторный откат — тоже (значения уже исходные)."""
    fake = FakeApi(prod_halls())
    path = _rollback_path()
    plan, _ = with_api(fake, lambda: halls.build_plan(file_entries(), halls.fetch_halls()))
    with_api(fake, lambda: halls.apply_plan(plan, path))

    with_api(fake, lambda: halls.run_rollback(path, apply=False))
    assert fake.halls[14]["description"] == "Голубая, текст путеводителя"

    with_api(fake, lambda: halls.run_rollback(path, apply=True))
    patches = len([m for m, _, _ in fake.calls if m == "PATCH"])
    with_api(fake, lambda: halls.run_rollback(path, apply=True))
    assert len([m for m, _, _ in fake.calls if m == "PATCH"]) == patches


def test_rollback_keeps_a_hand_edit():
    """После прогона описание правили руками — откат обязан его сохранить, а не затереть."""
    fake = FakeApi(prod_halls())
    path = _rollback_path()
    plan, _ = with_api(fake, lambda: halls.build_plan(file_entries(), halls.fetch_halls()))
    with_api(fake, lambda: halls.apply_plan(plan, path))
    fake.halls[14]["description"] = "Голубая, правка музея"

    _, out = with_api(fake, lambda: halls.run_rollback(path, apply=True))
    assert fake.halls[14]["description"] == "Голубая, правка музея"
    assert "правили после прогона" in out


def test_failed_patch_is_counted_and_still_leaves_a_rollback_file():
    """PATCH упал — считаем ошибку и всё равно оставляем файл отката для уже применённого."""
    fake = FakeApi(prod_halls())
    fake.patch_status = 409
    path = _rollback_path()
    plan, _ = with_api(fake, lambda: halls.build_plan(file_entries(), halls.fetch_halls()))
    errors, _ = with_api(fake, lambda: halls.apply_plan(plan, path))
    assert errors == len(plan.updates)
    assert json.load(open(path, encoding="utf-8"))["items"] == []


# ── Сам файл описаний ───────────────────────────────────────────────────────────────────────
def test_shipped_file_matches_prod_layout():
    """db/hall_descriptions.json: 12 залов, номера 1–12 без дыр, имена уникальны."""
    entries = halls.load_entries(DESC_FILE)
    numbers = sorted(e.hall_number for e in entries)
    assert numbers == list(range(1, 13))
    keys = {normalized(e.name) for e in entries}
    assert len(keys) == len(entries)
    assert all(e.description.strip() for e in entries)


def test_shipped_file_has_the_two_drawing_rooms_split():
    """Гостиные разъехались: склейки «Белая и Голубая» в файле больше нет."""
    by_name = {e.name: e for e in halls.load_entries(DESC_FILE)}
    assert by_name["Белая гостиная"].hall_number == 7
    assert by_name["Голубая гостиная"].hall_number == 8
    for e in by_name.values():
        # «\n\n» — шов старой склейки. «<Название зала>. » в начале — вклеенный заголовок
        # места (тот же дефект импорта, что в п.4 ТЗ у экспоната id=458); отличаем его от
        # честного начала фразы «Рыцарский зал был отделан…» по точке с пробелом.
        assert "\n\n" not in e.description, f"склейка осталась в «{e.name}»"
        assert not e.description.startswith(e.name + ". "), f"заголовок-префикс остался в «{e.name}»"
    # Тексты не перепутаны местами: у каждой гостиной свой герой из путеводителя.
    assert by_name["Белая гостиная"].description.startswith("Возрождение эмалевого дела")
    assert by_name["Голубая гостиная"].description.startswith("Иван Хлебников")
    assert "Рюкерта (1840–1917)" in by_name["Голубая гостиная"].description


def normalized(name: str) -> str:
    return halls.normalize_name(name)


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
