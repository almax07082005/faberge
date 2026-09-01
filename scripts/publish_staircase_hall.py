#!/usr/bin/env python3
"""Делает зал «Парадная лестница» публичным первым залом основной экспозиции.

Баг-репорт заказчика 31.08.2026, п. I-1, дословно:
    «Во вкладке "Основная экспозиция" добавить первым залом "Парадная лестница"
     с рассказом о дворце и истории создания Музея.»

Что отменяется
--------------
Флаг ``halls.is_service = true`` у зала №1 стоял НЕ по ошибке: он поставлен во
исполнение п.5 баг-репорта от 28.07.2026 («убрать Парадную лестницу из списка
залов») и разобран письменно в docs/staircase-hall-decision.md. 31.08.2026 музей
это решение отменил сам. Скрипт исполняет отмену, а не «чинит баг», — отсюда и
тон сообщений, и раздел «Отменено 31.08.2026» в документе-решении.

Почему скрипт, а не разовый PATCH руками
----------------------------------------
Прод-БД и админ-токен есть не у всех, а psql — тем более (см. README, раздел
про миграции). Штатный канал разовых правок каталога здесь — админ-API, как у
всех соседних скриптов. Эквивалент миграцией — db/migrations/2026-08-31_staircase_public.sql.

ГЕЙТ ПО ОПИСАНИЮ (главное содержательное требование пункта)
-----------------------------------------------------------
Старое описание зала — архитектурная справка про перила, купол и статую
Аполлона: слов «дворец Шуваловых как музей», «фонд», «Вексельберг», «Форбс» в
нём нет вовсе. Снять флаг раньше заливки нового текста — значит открыть
экспозицию справкой про перила вместо рассказа о музее, то есть формально
закрыть I-1 и не выполнить его. Поэтому скрипт СНАЧАЛА сверяет описание зала на
проде с записью ключа «1» в db/hall_descriptions.json и, если оно не совпало,
ничего не трогает и возвращает 1 с подсказкой прогнать
``scripts/apply_hall_descriptions.py --only "Парадная лестница" --apply``.
Порядок «сначала текст, потом флаг» — свойство скрипта, а не устная
договорённость. Ключ ``--skip-description-check`` гейт снимает (например, если
музей утвердил другой текст и залил его руками).

Порядок не трогаем без нужды
----------------------------
Залы сортируются как ``sort_order, hall_number NULLS LAST`` (app/crud.py::_HALL_ORDER),
и у лестницы ``sort_order`` уже равен 1, а у остальных залов 2…12 — она встанет
первой сама. Безусловный ``sort_order = 1`` был бы ОПАСЕН на окружении, где
порядок залам ещё не бэкфилнули (у всех 0): зал уехал бы в КОНЕЦ списка вместо
начала. Поэтому правку порядка добавляем в план, только если перед лестницей
реально стоит другой зал, и ставим не константу, а «минимум среди остальных − 1».

Запуск
------
    BASE_URL=https://api.example.ru ADMIN_TOKEN=secret \\
        python scripts/publish_staircase_hall.py                  # сухой прогон (по умолчанию)
    ... --apply                                                   # применить
    ... --rollback staircase_rollback_20260831-120000.json --apply # вернуть как было

Сухой прогон — ПО УМОЛЧАНИЮ (как у соседних скриптов каталога). При ``--apply``
пишется файл отката с ИСХОДНЫМИ значениями полей; сам откат означает отмену
решения музея, а не возврат к «правильному» состоянию, — делать только по явной
просьбе музея.

Идемпотентен: у зала, который уже публичный и стоит первым, план пуст, поэтому
повторный прогон печатает ноль правок. Зависимость одна — нормализация имён из
app/services/recognizer.py, чтобы сшивка залов везде была одна и та же.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.recognizer import normalize_name  # noqa: E402

BASE = os.environ.get("BASE_URL", "http://localhost:8000").rstrip("/")
TOKEN = os.environ.get("ADMIN_TOKEN", os.environ.get("ADMIN_API_TOKEN", "dev-admin-token"))
UA = "faberge-publish-staircase/1.0"

HERE = os.path.dirname(os.path.abspath(__file__))
DESC_FILE = os.path.join(HERE, "..", "db", "hall_descriptions.json")

# Имя зала — единственный надёжный ключ: id в окружениях разные (тот же довод, что
# в db/migrations/2026-08-06_bugreport_iter2.sql). Номер — запасной признак.
STAIRCASE_NAME = "Парадная лестница"
STAIRCASE_NUMBER = 1

# Служебные записи попадают в список только с этим флагом — а лестница до прогона
# как раз служебная, без флага скрипт её бы и не увидел.
HALLS_PATH = "/halls?include_service=true"


# ── Сетевой слой (тонкий, один в один с scripts/apply_hall_descriptions.py) ─────────────────
def api(method: str, path: str, body: Optional[dict] = None) -> Tuple[int, object]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{BASE}{path}", data=data, method=method,
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json", "User-Agent": UA},
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            raw = r.read().decode("utf-8", "replace")
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, raw


def get_all(path: str, key: str = "items", page: int = 100) -> List[dict]:
    """Собрать все страницы списочного эндпоинта."""
    out: List[dict] = []
    offset = 0
    while True:
        sep = "&" if "?" in path else "?"
        status, body = api("GET", f"{path}{sep}limit={page}&offset={offset}")
        if status != 200 or not isinstance(body, dict):
            raise SystemExit(f"GET {path} → {status}: {body}")
        items = body.get(key, [])
        out += items
        offset += len(items)
        if len(items) < page or offset >= body.get("total", 0):
            return out


# ── Чистое ядро: план правки (без сети) ─────────────────────────────────────────────────────
@dataclass
class Plan:
    """Что скрипт собирается сделать с одним залом.

    Пустой ``changes`` — это НЕ ошибка: так выглядит уже применённое состояние
    (идемпотентность). Ошибку отличаем по ``problem``: зал не найден, тёзки,
    описание ещё не залито. В этих случаях не трогаем ничего вообще.
    """

    hall: Optional[dict] = None
    changes: dict = field(default_factory=dict)       # тело PATCH: только изменившиеся поля
    before: dict = field(default_factory=dict)        # те же поля до правки — для файла отката
    problem: Optional[str] = None                     # человекочитаемая причина отказа
    hint: Optional[str] = None                        # что с этой причиной делать
    numbered_after: int = 0                           # сколько пронумерованных залов увидит посетитель

    @property
    def ok(self) -> bool:
        return self.problem is None


def load_expected_description(path: str = DESC_FILE) -> Optional[str]:
    """Утверждённый текст зала из db/hall_descriptions.json — по ИМЕНИ, не по ключу.

    Ключи файла — id залов прода на 12.08.2026, в других окружениях id другие;
    имя же нормализуется той же функцией, что и в заливке описаний.
    """
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)
    target = normalize_name(STAIRCASE_NAME)
    for value in raw.values():
        if normalize_name(value.get("name") or "") == target:
            return value.get("description") or ""
    return None


def find_staircase(halls: Sequence[dict]) -> Tuple[List[dict], str]:
    """Найти зал по имени; если по имени не нашлось — по номеру. Возвращает (кандидаты, как искали)."""
    target = normalize_name(STAIRCASE_NAME)
    by_name = [h for h in halls if normalize_name(h.get("name") or "") == target]
    if by_name:
        return by_name, "по названию"
    return [h for h in halls if h.get("hall_number") == STAIRCASE_NUMBER], "по номеру"


def build_plan(
    halls: Sequence[dict],
    expected_description: Optional[str],
    check_description: bool = True,
) -> Plan:
    """Решить, что править у зала «Парадная лестница». Без сети и без побочных эффектов.

    Правок ровно две, и вторая — условная:
      • ``is_service: false`` — собственно исполнение п. I-1;
      • ``sort_order`` — ТОЛЬКО если перед залом реально стоит другой зал (см. шапку
        про опасность безусловной единицы).
    """
    found, how = find_staircase(halls)
    if not found:
        return Plan(
            problem=f"зала «{STAIRCASE_NAME}» в каталоге нет",
            hint="проверьте BASE_URL и название зала в админке — вслепую создавать зал скрипт не будет",
        )
    if len(found) > 1:
        names = ", ".join(f"id={h.get('id')} «{h.get('name')}»" for h in found)
        return Plan(
            problem=f"залов, подходящих под «{STAIRCASE_NAME}» ({how}), больше одного: {names}",
            hint="выбирать наугад нельзя — разберитесь с тёзками в админке и повторите",
        )

    hall = found[0]
    plan = Plan(hall=hall)

    # Гейт по описанию — до формирования правок, чтобы в отказе не было соблазна
    # «ну хотя бы флаг снимем». Смысл см. в шапке: зал не должен становиться
    # публичным раньше своего текста.
    if check_description:
        if expected_description is None:
            plan.problem = f"в db/hall_descriptions.json нет записи для «{STAIRCASE_NAME}»"
            plan.hint = "сверять не с чем; поправьте файл описаний или прогоните с --skip-description-check"
            return plan
        if (hall.get("description") or "") != expected_description:
            live = len(hall.get("description") or "")
            plan.problem = (
                f"описание зала на проде ({live} симв.) не совпадает с утверждённым текстом "
                f"({len(expected_description)} симв.) — рассказа о дворце и истории музея там ещё нет"
            )
            plan.hint = (
                'сначала залейте текст: python scripts/apply_hall_descriptions.py '
                f'--only "{STAIRCASE_NAME}" --apply — и повторите этот прогон'
            )
            return plan

    if hall.get("is_service"):
        plan.changes["is_service"] = False
        plan.before["is_service"] = hall.get("is_service")

    others = [h for h in halls if h.get("id") != hall.get("id")]
    if others:
        min_other = min(h.get("sort_order") or 0 for h in others)
        current = hall.get("sort_order") or 0
        if current > min_other:
            plan.changes["sort_order"] = min_other - 1
            plan.before["sort_order"] = hall.get("sort_order")

    # Сколько залов увидит посетитель после правки — та же арифметика, что у гида
    # в app/routers/guide.py::_describe_halls (считаются пронумерованные непослужебные).
    plan.numbered_after = sum(
        1 for h in halls
        if h.get("hall_number") is not None
        and (not h.get("is_service") or h.get("id") == hall.get("id"))
    )
    return plan


# ── Отчёт ───────────────────────────────────────────────────────────────────────────────────
def _plural_halls(n: int) -> str:
    """«12 залов» — та же морфология, что во фразе гида; иначе отчёт врёт про его ответ."""
    if 11 <= n % 100 <= 14:
        return "залов"
    return {1: "зал", 2: "зала", 3: "зала", 4: "зала"}.get(n % 10, "залов")


def print_report(plan: Plan, halls: Sequence[dict], apply: bool) -> None:
    print("Применение" if apply else "План (сухой прогон)")
    print(f"Каталог: {BASE}")

    if not plan.ok:
        print(f"\n❌ НЕ ТРОГАЛИ: {plan.problem}")
        if plan.hint:
            print(f"   {plan.hint}")
        return

    hall = plan.hall or {}
    public_now = sum(1 for h in halls if not h.get("is_service"))
    print(f"Зал: id={hall.get('id')} №{hall.get('hall_number')} «{hall.get('name')}» "
          f"(is_service={hall.get('is_service')}, sort_order={hall.get('sort_order')}, "
          f"описание {len(hall.get('description') or '')} симв.)")

    if not plan.changes:
        print("\n  Править нечего: зал уже публичный и стоит первым.")
        return

    print(f"\nПравки ({len(plan.changes)}):")
    for key, value in plan.changes.items():
        print(f"  ~ {key}: {plan.before.get(key)!r} → {value!r}")
    if "sort_order" not in plan.changes:
        print("  · sort_order не трогаем: зал и так первый по порядку "
              "(слепая единица на непробэкфилленной базе увела бы его в конец списка)")

    print(f"\nПосле правки: GET /halls отдаст {public_now + 1} залов вместо {public_now}; "
          f"первым — №{hall.get('hall_number')} «{hall.get('name')}»; "
          f"гид скажет «В музее {plan.numbered_after} {_plural_halls(plan.numbered_after)}».")
    if not apply:
        print("\nЭто сухой прогон. Повторите с --apply.")


# ── Применение и откат ──────────────────────────────────────────────────────────────────────
def _default_rollback_path() -> str:
    return f"staircase_rollback_{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"


def apply_plan(plan: Plan, rollback_path: str) -> int:
    """Один PATCH /admin/halls/{id} с изменившимися полями + файл отката.

    Файл отката пишется в ``finally``, как в scripts/apply_hall_descriptions.py: даже
    если прогон свалился, откатывать применённое чем-то надо.
    """
    hall = plan.hall or {}
    log = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "base_url": BASE,
        "items": [],
    }
    errors = 0
    try:
        status, body = api("PATCH", f"/admin/halls/{hall['id']}", dict(plan.changes))
        if status != 200:
            print(f"  ОШИБКА id={hall['id']} «{hall.get('name')}»: {status} {body}")
            errors += 1
        else:
            log["items"].append({
                "hall_id": hall["id"],
                "hall_name": hall.get("name"),
                "hall_number": hall.get("hall_number"),
                "before": dict(plan.before),
                "after": dict(plan.changes),
            })
            fields = ", ".join(f"{k}={v!r}" for k, v in plan.changes.items())
            print(f"  ~ id={hall['id']} «{hall.get('name')}»: {fields}")
    finally:
        with open(rollback_path, "w", encoding="utf-8") as fh:
            json.dump(log, fh, ensure_ascii=False, indent=2)
        print(f"\nОбновлено залов: {len(log['items'])}")
        print(f"Файл отката: {os.path.abspath(rollback_path)}")
    return errors


def run_rollback(path: str, apply: bool) -> int:
    """Вернуть исходные значения по файлу отката.

    Зал, который после прогона правили руками (текущее значение не то, что записал
    скрипт), не трогаем: чужую правку молча затирать нельзя. Если значение уже
    исходное — пропускаем без шума, поэтому откат можно повторять.
    """
    print("ВНИМАНИЕ: откат возвращает зал в служебные, то есть ОТМЕНЯЕТ решение музея "
          "от 31.08.2026 (п. I-1). Делайте это только по их явной просьбе.\n")
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)

    restored = skipped = errors = 0
    for item in doc.get("items", []):
        hall_id = item["hall_id"]
        status, hall = api("GET", f"/halls/{hall_id}")
        if status != 200 or not isinstance(hall, dict):
            print(f"  ПРОПУСК id={hall_id}: зал недоступен ({status} {hall})")
            errors += 1
            continue
        before, after = item.get("before", {}), item.get("after", {})
        if all(hall.get(k) == v for k, v in before.items()):
            continue                                   # уже как было — откат идемпотентен
        if any(hall.get(k) != v for k, v in after.items()):
            print(f"  ПРОПУСК id={hall_id} «{item.get('hall_name')}»: "
                  "зал правили после прогона — разбирайтесь руками")
            skipped += 1
            continue
        print(f"  ← id={hall_id} «{item.get('hall_name')}»: "
              + ", ".join(f"{k}={v!r}" for k, v in before.items()))
        restored += 1
        if not apply:
            continue
        status, body = api("PATCH", f"/admin/halls/{hall_id}", dict(before))
        if status != 200:
            print(f"    ОШИБКА отката: {status} {body}")
            errors += 1

    print(f"\nВозвращено залов: {restored}" + (f", пропущено: {skipped}" if skipped else ""))
    if not apply:
        print("\nЭто сухой прогон отката. Повторите с --apply.")
    return errors


# ── CLI ─────────────────────────────────────────────────────────────────────────────────────
def fetch_halls() -> List[dict]:
    return get_all(HALLS_PATH)


def run(args: argparse.Namespace) -> int:
    if args.rollback:
        return 1 if run_rollback(args.rollback, args.apply) else 0

    halls = fetch_halls()
    expected = load_expected_description(args.file)
    plan = build_plan(halls, expected, check_description=not args.skip_description_check)
    print_report(plan, halls, args.apply)

    # Отказ — это невыполненная работа, а не предупреждение: возвращаем 1, чтобы
    # прогон в скрипте/CI не сошёл за успешный (тот же контракт, что у
    # scripts/apply_hall_descriptions.py).
    if not plan.ok:
        return 1
    if not args.apply or not plan.changes:
        return 0
    print()
    return 1 if apply_plan(plan, args.rollback_file) else 0


def build_parser() -> argparse.ArgumentParser:
    """Парсер отдельной функцией — чтобы тест мог проверить дефолты без сети и БД."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="применить изменения (без ключа — сухой прогон)")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="ничего не делает: сухой прогон и так по умолчанию (ключ оставлен для совместимости вызовов)",
    )
    parser.add_argument("--file", default=DESC_FILE, help="файл описаний (по умолчанию db/hall_descriptions.json)")
    parser.add_argument(
        "--skip-description-check", action="store_true",
        help="снять флаг, даже если описание зала ещё не совпадает с утверждённым текстом "
             "(по умолчанию скрипт откажется: посетитель увидит справку про перила вместо рассказа о музее)",
    )
    parser.add_argument(
        "--rollback-file", default=_default_rollback_path(),
        help="куда писать файл отката при --apply (по умолчанию — с датой в имени, в текущем каталоге)",
    )
    parser.add_argument("--rollback", metavar="FILE", help="вернуть исходные значения по файлу отката")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.dry_run and args.apply:
        parser.error("--dry-run и --apply вместе не имеют смысла: выберите одно")
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
