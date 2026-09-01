"""Зал №1 «Парадная лестница» становится первым залом экспозиции (баг-репорт 31.08.2026, п. I-1).

Дословно: «Во вкладке "Основная экспозиция" добавить первым залом "Парадная лестница"
с рассказом о дворце и истории создания Музея.»

Флаг `halls.is_service = true` был не артефактом импорта, а решением по п.5 баг-репорта
28.07.2026 (разбор — docs/staircase-hall-decision.md). Музей решение отменил, и здесь
закрепляется всё, что при отмене легко сломать:

  • счётчик гида: «В музее 11 залов» → «В музее 12 залов» — тот самый побочный эффект,
    который музей просил снять. Он снимается САМ, без правок кода, и именно поэтому
    его надо держать тестом, а не наблюдением на проде;
  • порядок: лестница первая, при этом `sort_order` слепо не переписывается —
    безусловная единица увела бы зал в КОНЕЦ списка на непробэкфилленной базе;
  • гейт по описанию: пока текст зала не залит, флаг не снимается. Иначе первым залом
    экспозиции открылась бы архитектурная справка про перила вместо рассказа о музее;
  • идемпотентность скрипта и миграции;
  • два пути регресса, которыми флаг возвращался бы сам: дефолт cleanup-скрипта и сид.

Точный ТЕКСТ описания здесь намеренно не проверяется — его ещё вычитывает музей.
Тесты берут утверждённый текст из db/hall_descriptions.json как чёрный ящик, а его
содержательные опоры проверяет tests/test_hall_descriptions.py.

БД и сеть не нужны: сетевой слой скрипта — единственная функция `api`, её подменяет FakeApi.
Запуск:
    python -m pytest tests/test_staircase_public.py
    python tests/test_staircase_public.py     # standalone
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

import cleanup_hall_catalog  # noqa: E402
import publish_staircase_hall as stair  # noqa: E402
from app import models as m  # noqa: E402
from app.routers.guide import _describe_halls  # noqa: E402

STAIRCASE = "Парадная лестница"
OUTSIDE = "Вне постоянной экспозиции"

MIGRATION = os.path.join(ROOT, "db", "migrations", "2026-08-31_staircase_public.sql")
SEED = os.path.join(ROOT, "db", "seed_fabergemuseum.sql")


# ── Снимок каталога на 31.08.2026 (до прогона) ──────────────────────────────────────────────
def approved_description() -> str:
    """Утверждённый текст зала — из файла, а не строкой в тесте: музей его ещё вычитывает."""
    text = stair.load_expected_description()
    assert text, "в db/hall_descriptions.json пропала запись «Парадная лестница»"
    return text


def live(hall_id: int, number, name: str, *, sort_order: int,
         is_service: bool = False, description=None) -> dict:
    return {"id": hall_id, "hall_number": number, "name": name, "description": description,
            "is_service": is_service, "is_temporary": False, "sort_order": sort_order}


def prod_halls(*, staircase_description=None, staircase_service: bool = True,
               staircase_sort: int = 1, others_sort_from_number: bool = True) -> list:
    """GET /halls?include_service=true, как он отвечает на проде 31.08.2026.

    Ключевое для пункта: у лестницы `sort_order` УЖЕ равен 1, а у остальных залов 2…12 —
    зал встаёт первым сам, править порядок не нужно. Флаг `others_sort_from_number`
    выключает это, изображая окружение, где порядок залам ещё не бэкфилнули (у всех 0):
    там лестница с единицей уехала бы в конец, и правка порядка обязана появиться.
    """
    if staircase_description is None:
        staircase_description = approved_description()
    halls = [live(1, 1, STAIRCASE, sort_order=staircase_sort,
                  is_service=staircase_service, description=staircase_description)]
    names = ["Рыцарский зал", "Красная гостиная", "Синяя гостиная", "Золотая гостиная",
             "Аванзал", "Белая гостиная", "Голубая гостиная", "Выставочный зал",
             "Готический зал", "Верхняя буфетная", "Бежевый зал"]
    for offset, name in enumerate(names, start=2):
        halls.append(live(offset, offset, name,
                          sort_order=offset if others_sort_from_number else 0,
                          description=f"{name}, текст путеводителя"))
    halls.append(live(13, None, OUTSIDE, sort_order=99,
                      description="Экспонаты каталога, не привязанные к залу."))
    return halls


def catalog_order(halls) -> list:
    """Порядок выдачи: `sort_order`, затем номер, залы без номера последними.

    Повторяет app/crud.py::_HALL_ORDER — ORDER BY уезжает в PostgreSQL, а тесты идут без
    БД. Дублирование осознанное и узкое: проверяем не сам SQL, а что при таких данных
    лестница оказывается первой.
    """
    return sorted(halls, key=lambda h: (h["sort_order"], h["hall_number"] is None, h["hall_number"] or 0))


def orm_halls(halls) -> list:
    """Словари снимка → ORM-объекты для функций гида (в БД ничего не пишется)."""
    return [
        m.Hall(id=h["id"], hall_number=h["hall_number"], name=h["name"],
               is_temporary=h["is_temporary"], is_service=h["is_service"], sort_order=h["sort_order"])
        for h in halls
    ]


# ── Фейковый каталог (копия подхода tests/test_hall_descriptions.py) ────────────────────────
class FakeApi:
    """Залы в памяти. Умеет ровно те ручки, что дёргает скрипт."""

    def __init__(self, halls_list) -> None:
        self.halls = {h["id"]: dict(h) for h in halls_list}
        self.calls: list = []
        self.patch_status = 200          # чем отвечать на PATCH (для проверки ветки ошибки)

    @property
    def patches(self) -> list:
        return [(path, body) for method, path, body in self.calls if method == "PATCH"]

    @staticmethod
    def _params(query: str) -> dict:
        return dict(p.split("=", 1) for p in query.split("&") if "=" in p)

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        head, _, query = path.partition("?")
        params = self._params(query)
        parts = head.strip("/").split("/")

        if method == "GET" and head == "/halls":
            include_service = params.get("include_service") == "true"
            items = [h for h in self.halls.values() if include_service or not h["is_service"]]
            items = catalog_order(items)
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
    saved, buffer = stair.api, io.StringIO()
    stair.api = fake
    try:
        with contextlib.redirect_stdout(buffer):
            return func(), buffer.getvalue()
    finally:
        stair.api = saved


def _rollback_path() -> str:
    return os.path.join(tempfile.mkdtemp(), "rollback.json")


def _args(**over):
    """Аргументы CLI со значениями по умолчанию — для прогона run() без argparse."""
    base = dict(rollback=None, apply=False, file=stair.DESC_FILE,
                skip_description_check=False, rollback_file=_rollback_path())
    base.update(over)
    return argparse.Namespace(**base)


# ── Побочный эффект, который музей просил снять: «11 залов» → «12 залов» ────────────────────
def test_guide_counts_twelve_halls_after_the_flag_is_cleared():
    """Гид говорит «В музее 12 залов» и называет лестницу первой — без единой правки кода.

    Счётчик в `_describe_halls` считает пронумерованные непослужебные залы из
    crud.all_halls_ordered, а та фильтрует ровно по is_service. Снятый флаг добавляет
    один пронумерованный зал — и «11 залов» (побочный эффект решения от 28.07, записанный
    в docs/staircase-hall-decision.md) становится «12 залов», как в путеводителе музея.
    """
    public = [h for h in catalog_order(prod_halls(staircase_service=False)) if not h["is_service"]]
    answer = _describe_halls(orm_halls(public))

    assert "В музее 12 залов." in answer
    assert answer.split("Основная экспозиция: ")[1].startswith(f"зал 1 «{STAIRCASE}»")
    # Зал без номера в счётчик не попадает и уезжает в хвост — это не менялось.
    assert OUTSIDE not in answer.split("Кроме того: ")[0]
    assert f"Кроме того: {OUTSIDE}." in answer


def test_guide_still_counts_eleven_while_the_flag_is_on():
    """Контрольный замер: со старым флагом ответ прежний. Иначе тест выше ничего не доказывает."""
    public = [h for h in catalog_order(prod_halls()) if not h["is_service"]]
    assert "В музее 11 залов." in _describe_halls(orm_halls(public))


def test_staircase_leads_the_public_catalog():
    """Лестница — первая запись публичной выдачи, и первый ПРОНУМЕРОВАННЫЙ зал тоже она."""
    public = [h for h in catalog_order(prod_halls(staircase_service=False)) if not h["is_service"]]
    assert public[0]["name"] == STAIRCASE
    assert [h["hall_number"] for h in public] == list(range(1, 13)) + [None]


# ── Чистое ядро скрипта: план правки ────────────────────────────────────────────────────────
def test_plan_on_prod_snapshot_only_clears_the_flag():
    """На проде правка ровно одна: is_service → false. sort_order НЕ трогаем.

    У зала уже sort_order = 1 при 2…12 у остальных — он встанет первым сам. Слепой
    `SET sort_order = 1` был бы не «на всякий случай», а активно вредным (см. тест ниже).
    """
    halls = prod_halls()
    plan = stair.build_plan(halls, approved_description())

    assert plan.ok and plan.problem is None
    assert plan.changes == {"is_service": False}
    assert plan.before == {"is_service": True}
    assert plan.numbered_after == 12


def test_plan_fixes_the_order_only_when_another_hall_stands_first():
    """Там, где залам не бэкфилнули порядок (у всех 0), правка порядка появляется.

    Это и есть ловушка из плана: лестница с sort_order = 1 уехала бы в КОНЕЦ списка.
    Ставим не константу, а «минимум среди остальных − 1», чтобы зал гарантированно
    оказался первым при любом раскладе.
    """
    halls = prod_halls(others_sort_from_number=False)
    plan = stair.build_plan(halls, approved_description())

    assert plan.changes == {"is_service": False, "sort_order": -1}
    assert plan.before["sort_order"] == 1
    # Проверяем результат, а не формулу: после правки зал действительно первый.
    patched = [dict(h, **(plan.changes if h["id"] == 1 else {})) for h in halls]
    assert catalog_order(patched)[0]["name"] == STAIRCASE


def test_second_run_produces_an_empty_plan():
    """Идемпотентность: у уже опубликованного зала править нечего."""
    plan = stair.build_plan(prod_halls(staircase_service=False), approved_description())
    assert plan.ok and plan.changes == {}


def test_missing_hall_is_reported_and_not_invented():
    """Зала нет — печатаем причину, ничего не создаём и не трогаем соседей."""
    halls = [h for h in prod_halls() if h["name"] != STAIRCASE]
    plan = stair.build_plan(halls, approved_description())
    assert not plan.ok and plan.changes == {}
    assert STAIRCASE in plan.problem


def test_two_halls_with_the_same_name_are_not_touched():
    """Тёзки — не повод выбирать наугад: обе записи остаются как есть."""
    halls = prod_halls()
    halls.append(live(99, None, "парадная  Лестница", sort_order=50))
    plan = stair.build_plan(halls, approved_description())
    assert not plan.ok and plan.changes == {}
    assert "больше одного" in plan.problem


# ── Гейт по описанию: зал не становится публичным раньше своего текста ──────────────────────
def test_stale_description_blocks_the_flag():
    """Пока в зале лежит старая справка про перила, флаг не снимается.

    Главная содержательная часть I-1: музей просил зал «с рассказом о дворце и истории
    создания Музея». Снять флаг раньше заливки текста — значит формально закрыть пункт
    и не выполнить его.
    """
    halls = prod_halls(staircase_description="Сохранившаяся до наших дней роскошная Парадная лестница…")
    plan = stair.build_plan(halls, approved_description())

    assert not plan.ok and plan.changes == {}
    assert "не совпадает" in plan.problem
    assert "apply_hall_descriptions.py" in plan.hint


def test_skip_description_check_opens_the_gate():
    """`--skip-description-check` — осознанный обход (музей утвердил другой текст и залил руками)."""
    halls = prod_halls(staircase_description="какой-то другой утверждённый музеем текст")
    plan = stair.build_plan(halls, approved_description(), check_description=False)
    assert plan.ok and plan.changes == {"is_service": False}


def test_gate_survives_the_run_and_sends_no_patch():
    """Полный прогон с --apply на непролитом тексте: код возврата 1 и НИ ОДНОГО PATCH."""
    fake = FakeApi(prod_halls(staircase_description="старая справка про перила"))
    code, out = with_api(fake, lambda: stair.run(_args(apply=True)))

    assert code == 1
    assert fake.patches == []
    assert "НЕ ТРОГАЛИ" in out


# ── Разрушающая половина: применение, повтор, откат ─────────────────────────────────────────
def test_dry_run_sends_no_patch():
    """Сухой прогон — по умолчанию: план печатается, каталог не меняется."""
    fake = FakeApi(prod_halls())
    code, out = with_api(fake, lambda: stair.run(_args()))

    assert code == 0
    assert fake.patches == []
    assert fake.halls[1]["is_service"] is True
    assert "сухой прогон" in out.lower()


def test_apply_sends_one_patch_and_writes_the_rollback_file():
    """`--apply`: ровно один PATCH /admin/halls/1 с телом {"is_service": false} + файл отката."""
    fake = FakeApi(prod_halls())
    path = _rollback_path()
    code, out = with_api(fake, lambda: stair.run(_args(apply=True, rollback_file=path)))

    assert code == 0
    assert fake.patches == [("/admin/halls/1", {"is_service": False})]
    assert fake.halls[1]["is_service"] is False
    assert "В музее 12 залов" in out

    with open(path, encoding="utf-8") as fh:
        log = json.load(fh)
    assert log["items"][0]["before"] == {"is_service": True}
    assert log["items"][0]["after"] == {"is_service": False}


def test_apply_twice_is_idempotent():
    """Повторный `--apply` по тому же каталогу не шлёт ни одного PATCH."""
    fake = FakeApi(prod_halls())
    with_api(fake, lambda: stair.run(_args(apply=True)))
    before = len(fake.patches)
    code, out = with_api(fake, lambda: stair.run(_args(apply=True)))

    assert code == 0
    assert len(fake.patches) == before
    assert "Править нечего" in out


def test_rollback_returns_the_flag_and_respects_manual_edits():
    """Откат возвращает is_service = true; зал, поправленный руками после прогона, не трогает."""
    fake = FakeApi(prod_halls())
    path = _rollback_path()
    with_api(fake, lambda: stair.run(_args(apply=True, rollback_file=path)))

    # Кто-то поправил зал после прогона — молча затирать чужую правку нельзя.
    fake.halls[1]["is_service"] = "не то, что писал скрипт"
    _, out = with_api(fake, lambda: stair.run(_args(rollback=path, apply=True)))
    assert "ПРОПУСК" in out

    fake.halls[1]["is_service"] = False
    code, out = with_api(fake, lambda: stair.run(_args(rollback=path, apply=True)))
    assert code == 0
    assert fake.halls[1]["is_service"] is True
    assert "ОТМЕНЯЕТ решение музея" in out


# ── Пути, которыми флаг возвращался бы сам ──────────────────────────────────────────────────
def test_cleanup_script_no_longer_hides_the_staircase_by_default():
    """`python scripts/cleanup_hall_catalog.py --apply` больше не прячет зал.

    Самый вероятный путь регресса: скрипт зовут ради живых пунктов 2 и 3 (номер у «Вне
    постоянной экспозиции», удаление тестового зала), а он третьим действием возвращал
    is_service = true и отменял решение музея. БД не нужна — парсер строится отдельно.
    """
    default = cleanup_hall_catalog.build_parser().parse_args([])
    assert default.hide_staircase is False
    assert default.delete_staircase is False

    explicit = cleanup_hall_catalog.build_parser().parse_args(["--apply", "--hide-staircase"])
    assert explicit.hide_staircase is True


class FakeConn:
    """Соединение-заглушка под scripts/cleanup_hall_catalog.py: в каталоге одна лестница.

    Нужна, чтобы проверить не только дефолт парсера, но и сам SQL: дефолт можно оставить
    на месте и всё равно вернуть флаг, если убрать ветку в `_run`. Записываем выполненные
    команды и смотрим, что среди них.
    """

    def __init__(self) -> None:
        self.executed: list = []

    async def fetchrow(self, query, *args):
        if args and args[0] == STAIRCASE:
            return {"id": 1, "hall_number": 1, "name": STAIRCASE, "is_service": False}
        return None                                   # остальных залов в этом каталоге нет

    async def fetchval(self, query, *args):
        return 0

    async def execute(self, query, *args):
        self.executed.append((" ".join(query.split()), args))

    def transaction(self):
        return contextlib.nullcontext()

    async def close(self):
        return None


def _run_cleanup(**kwargs) -> FakeConn:
    """Прогнать _run с фейковым соединением и вернуть его — для разбора выполненного SQL."""
    import asyncio

    conn = FakeConn()

    async def fake_connect(*_a, **_kw):
        return conn

    saved = cleanup_hall_catalog.asyncpg.connect
    cleanup_hall_catalog.asyncpg.connect = fake_connect
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            asyncio.run(cleanup_hall_catalog._run(apply=True, delete_staircase=False, **kwargs))
    finally:
        cleanup_hall_catalog.asyncpg.connect = saved
    return conn


def test_cleanup_apply_does_not_reinstate_the_service_flag():
    """Штатный `--apply` не выполняет UPDATE по is_service; `--hide-staircase` — выполняет.

    Проверяется уже не дефолт парсера, а исполняемый SQL: именно этой командой (README,
    раздел «Каталог залов и витрин») решение музея откатывалось бы само.
    """
    default = _run_cleanup()
    assert not [q for q, _ in default.executed if "is_service" in q]

    hidden = _run_cleanup(hide_staircase=True)
    assert [args for q, args in hidden.executed if "SET is_service = true" in q] == [(1,)]


def test_seed_no_longer_marks_the_staircase_as_service():
    """Переналивка db/seed_fabergemuseum.sql не возвращает флаг.

    Второй путь регресса, и он уже срабатывал: ровно так возвращался is_temporary у
    Выставочного зала (п.2 баг-репорта 06.08.2026). Причина-объяснение в сиде осталась —
    удалён только сам UPDATE.
    """
    with open(SEED, encoding="utf-8") as fh:
        sql = fh.read()
    # Комментарии выкидываем: в них флаг НАЗВАН намеренно — это объяснение, почему его
    # больше нет. Проверять надо исполняемый SQL, иначе тест ловил бы собственную документацию.
    body = "\n".join(line for line in sql.splitlines() if not line.lstrip().startswith("--"))
    assert "is_service" not in body, "сид снова проставляет служебный флаг"
    assert "docs/staircase-hall-decision.md" in sql, "из сида пропало объяснение, почему флага нет"


def test_migration_is_idempotent_and_never_hardcodes_sort_order():
    """Миграция: предикат идемпотентности на месте, безусловной единицы в порядке нет.

    Проверяем текстом, а не прогоном: тесты идут без БД. Обе проверки узкие и ловят ровно
    те две правки, которыми эту миграцию проще всего испортить, — снять предикат
    `WHERE is_service` (повторный прогон начнёт дёргать триггер updated_at) и заменить
    условную правку порядка на `SET sort_order = 1` (зал уедет в конец списка там, где
    остальным залам порядок ещё не бэкфилнули).
    """
    with open(MIGRATION, encoding="utf-8") as fh:
        sql = fh.read()
    body = "\n".join(line for line in sql.splitlines() if not line.lstrip().startswith("--"))

    assert "SET is_service = false" in body
    assert "WHERE is_service" in body, "пропал предикат идемпотентности"
    assert "SET sort_order = 1" not in body, "безусловный sort_order = 1 уводит зал в конец списка"
    assert "min(sort_order)" in body


if __name__ == "__main__":
    failures = 0
    for name, func in sorted(globals().items()):
        if name.startswith("test_") and callable(func):
            try:
                func()
                print(f"PASS {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
    sys.exit(1 if failures else 0)
