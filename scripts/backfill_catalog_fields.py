#!/usr/bin/env python3
"""Разовый бэкфилл структурных полей каталога из каталожной строки (баг-репорт 12.08.2026, п.5).

Заказчик: «Прогнать по существующим stub-карточкам разово, идемпотентно». Импорт залил
каталожную строку указателя целиком в ``short_description``, а ``year_created`` /
``master_name`` / ``material`` оставил пустыми — на проде 12.08.2026 таких карточек 1048 из
1252 (у 1020 пусты сразу все три поля; в Бежевом зале — 213 из 215). Контент верный, но
фильтры, поиск, аналитика и распознавание работают по структурным полям и этих карточек
не видят. Разбор строки живёт в ``app/services/catalog_line`` (там же его тесты и вся
логика краевых случаев) — скрипт только раскладывает результат по колонкам, печатает отчёт
и умеет откатиться.

ЧТО СКРИПТ НЕ ДЕЛАЕТ — это важнее того, что делает:

* **Не перезаписывает непустое поле. Никогда.** Музей и фронт правят карточки руками —
  12.08 фронт применил 38 правок на 34 карточках (мастера, годы, материалы, п.7 таски).
  Разбор строки указателя 2014 года не может быть основанием затереть более свежее
  решение музея. Поэтому непустое поле не трогаем, а расхождение «в поле одно, в строке
  другое» печатаем отдельной секцией «расходится с каталожной строкой»: это материал для
  разговора с музеем, а не работа для машины.
* **Не трогает ``short_description``.** Это ИСТОЧНИК разбора, а не мусор: перепишем его —
  и повторный прогон будет разбирать уже собственный вывод. Побочная выгода: раз
  ``short_description`` не в запросе, ``admin.patch_exhibit`` не зовёт ``_autofill_spoken``
  и озвучка не уезжает на перегенерацию в LLM (E15) — гасить её явно, как в
  ``scripts/fix_catalog_typography.py``, здесь не нужно.
* **Не чинит содержательные ошибки указателя.** Перевёрнутый диапазон «1897−1809» (id 483)
  разбирается как есть с пометкой в отчёте: молча поменять годы местами — значит подделать
  источник.
* **Не разбирает связную музейную прозу.** 20 карточек (id 8, 48, 232 …) — это очерки, а не
  каталожные строки; ``looks_like_catalog_line`` отсеивает их до разбора, и все поля таких
  карточек остаются как были. Наивное правило «есть «;» → каталожная строка» на них
  ломается: точка с запятой встречается и внутри прозы (id 5, 11).

Смежная задача того же пункта — «в material не должно быть техник». Скан всех 1252 карточек
прода по словарю из 77 техник дал РОВНО ОДНОГО нарушителя: id 48 «Царские врата», material
«Серебро, Дерево, Левкас, Темпера, Тиснение». Причина видна в самом путеводителе — там
напечатано «Оклад • XVII век • Серебро, тиснение», запятая вместо «;». Это правка НЕПУСТОГО
поля, поэтому она живёт за отдельным ключом ``--clean-material-techniques`` и по умолчанию
не выполняется. «Акварель», на которую жалуется заказчик (id 163, 130, 114), в поле material
сегодня отсутствует — она осталась только в legacy-строке ``short_description``, и чинить её
надо там, отдельным прогоном.

Токен «эмаль» чистка не выносит и показывает в секции «требует глаз»: в указателе он стоит и
слева от «;» (материал — id 1079 «Компас настольный», сверено с путеводителем), и справа
(техника, 32 случая). Позиции у поля material нет, значит машинного признака нет тоже.

Обход каталога — по залам (``GET /halls?include_service=true`` → ``GET /halls/{id}/exhibits``),
чтобы сводка шла по залам и чтобы в объём попал служебный зал 1 «Парадная лестница», которого
нет в публичном списке залов (п.1 этой же таски). ``short_description``, ``material`` и
``techniques`` в списочной выдаче отсутствуют — за ними скрипт идёт в
``GET /admin/exhibits/{id}``, как и ``scripts/fix_catalog_typography.py``. Карточку без
витрины (``showcase_id = null``) обход по залам не видит: фильтр зала идёт через его витрины —
для таких есть ``--ids``.

    BASE_URL=http://localhost:8000 ADMIN_TOKEN=dev-admin-token \\
        python scripts/backfill_catalog_fields.py                     # сухой прогон, весь каталог
    ... --limit 50                                                    # проба на первых 50 карточках
    ... --ids 459,511,540                                             # точечно
    ... --report-file backfill_20260812.csv                           # список правок заказчику
    ... --apply                                                       # применить
    ... --clean-material-techniques --apply                           # + вынести технику из material
    ... --rollback catalog_backfill_rollback_20260812-120000.json --apply

Идемпотентен, и это главный критерий приёмки из ТЗ («Повторный прогон импорта ничего не
ломает»). Держится на двух свойствах: ``parse_catalog_line`` детерминирован (одна строка →
один результат), а дозаполняются только ПУСТЫЕ поля — после ``--apply`` они непусты, и
второй прогон печатает пустой план и не шлёт ни одного PATCH. То же с чисткой material: из
вычищенного значения технику больше не выделить.

Требует зависимостей проекта: разбор берётся из ``app/services/catalog_line.py`` — тот же
модуль, что и в импорте, чтобы правила были в одном месте.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import urllib.error
import urllib.request
from collections import Counter, OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.catalog_line import (  # noqa: E402
    PRECISION_AFTER,
    PRECISION_BEFORE,
    PRECISION_CENTURY,
    PRECISION_CIRCA,
    PRECISION_DECADE,
    PRECISION_EXACT,
    PRECISION_RANGE,
    PRECISION_RANGE_DECADE,
    STATUS_PARSED,
    STATUS_PARTIAL,
    STATUS_REPAIRED,
    STATUS_SKIPPED,
    TECHNIQUE_PREFIXES,
    TECHNIQUES,
    parse_catalog_line,
)

BASE = os.environ.get("BASE_URL", "http://localhost:8000").rstrip("/")
TOKEN = os.environ.get("ADMIN_TOKEN", os.environ.get("ADMIN_API_TOKEN", "dev-admin-token"))
UA = "faberge-catalog-backfill/1.0"

# Поля, которые бэкфилл дозаполняет. Имена колонок совпадают с именами полей ``ParsedLine``,
# поэтому отдельной таблицы соответствий не нужно — берём через getattr.
# ``origin_place`` и ``provenance`` парсер тоже отдаёт, но колонок под них нет: они идут
# только в отчёт, чтобы заказчик глазом видел, ТУ ЛИ строку разобрал парсер.
FIELD_TITLES: "OrderedDict[str, str]" = OrderedDict((
    ("year_created", "год"),
    ("dating", "датировка"),
    ("master_name", "мастер"),
    ("material", "материалы"),
    ("techniques", "техники"),
))

SOURCE_FIELD = "short_description"      # источник разбора; в патч не попадает никогда

STATUS_TITLES: "OrderedDict[str, str]" = OrderedDict((
    (STATUS_PARSED, "разобрано"),
    (STATUS_REPAIRED, "разобрано с починкой строки"),
    (STATUS_PARTIAL, "разобрано частично"),
    (STATUS_SKIPPED, "пропущено"),
))

# Датировки в строке нет вовсе: у трёх legacy-карточек формы B (id 95, 133, 173) год
# не напечатан. Это не ошибка разбора — остальные поля у них берутся как обычно.
PRECISION_NONE = "—"

PRECISION_TITLES: "OrderedDict[str, str]" = OrderedDict((
    (PRECISION_EXACT, "точный год"),
    (PRECISION_RANGE, "диапазон лет"),
    (PRECISION_RANGE_DECADE, "диапазон десятилетий"),
    (PRECISION_DECADE, "десятилетие"),
    (PRECISION_CIRCA, "около"),
    (PRECISION_BEFORE, "не позднее"),
    (PRECISION_AFTER, "после"),
    (PRECISION_CENTURY, "век — года нет, year_created пуст"),
    (PRECISION_NONE, "датировки в строке нет"),
))

KIND_FILL = "fill"        # дозаполнение пустого поля разбором строки
KIND_CLEAN = "clean"      # вынос техники из material (только под --clean-material-techniques)
KIND_TITLES = {KIND_FILL: "дозаполнение", KIND_CLEAN: "чистка material"}

# Токены, которые указатель использует и как материал, и как технику: решает только позиция
# относительно «;», а у поля material позиции нет. Чистка их не выносит — показывает человеку.
# На сегодня список ровно один: «эмаль» встретилась материалом дважды (id 1079, 1097 — сверено
# с путеводителем) и техникой 32 раза.
AMBIGUOUS_TECHNIQUES: frozenset = frozenset({"эмаль"})


# ── Сетевой слой (тонкий, один в один с scripts/fix_catalog_typography.py) ──────────────────
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


def get_all(path: str, key: str = "items", page: int = 100) -> List[dict]:  # page ≤ limit публичного API
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


# ── Чистое ядро: план правок (без сети; сам разбор и его тесты — в catalog_line) ────────────
@dataclass
class Change:
    """Одна правка поля карточки: было/стало и откуда взялось."""

    exhibit_id: int
    exhibit_name: str
    hall_number: Optional[int]
    field_name: str
    before: object
    after: object
    kind: str
    note: str = ""


@dataclass
class Conflict:
    """Поле НЕПУСТО и не совпадает с разбором строки. Не чиним — показываем заказчику."""

    exhibit_id: int
    exhibit_name: str
    hall_number: Optional[int]
    field_name: str
    current: object
    parsed: object
    note: str = ""


@dataclass
class Review:
    """Карточка, которую скрипт сознательно НЕ ТРОГАЕТ: нужен человек."""

    exhibit_id: int
    exhibit_name: str
    hall_number: Optional[int]
    reason: str
    value: str


@dataclass
class Skip:
    """Строка не разобрана: ни одно поле карточки не меняется, id уходит в отчёт."""

    exhibit_id: int
    exhibit_name: str
    hall_number: Optional[int]
    reason: str
    line: str


@dataclass
class Plan:
    changes: List[Change] = field(default_factory=list)
    conflicts: List[Conflict] = field(default_factory=list)
    reviews: List[Review] = field(default_factory=list)
    skips: List[Skip] = field(default_factory=list)
    notes: List[Tuple[int, Tuple[str, ...]]] = field(default_factory=list)
    scanned: int = 0        # карточек просмотрено
    with_line: int = 0      # из них с непустым short_description
    parsed: int = 0         # из них разобрано (status != skipped)
    statuses: "Counter[str]" = field(default_factory=Counter)
    precisions: "Counter[str]" = field(default_factory=Counter)

    def by_exhibit(self) -> "OrderedDict[int, List[Change]]":
        """Правки, сгруппированные по экспонату: один PATCH на карточку, а не на поле."""
        grouped: "OrderedDict[int, List[Change]]" = OrderedDict()
        for change in self.changes:
            grouped.setdefault(change.exhibit_id, []).append(change)
        return grouped

    def field_counts(self) -> "Counter[str]":
        return Counter(change.field_name for change in self.changes)

    def hall_counts(self) -> "Counter[object]":
        """Сколько КАРТОЧЕК затронуто в каждом зале (а не сколько полей)."""
        seen: Dict[int, object] = {}
        for change in self.changes:
            seen[change.exhibit_id] = change.hall_number
        return Counter(seen.values())

    def skip_families(self) -> "Counter[str]":
        """Причины пропуска, схлопнутые до семейства: в самой причине сидит цитата строки."""
        return Counter(_skip_family(skip.reason) for skip in self.skips)


_WS_RE = re.compile(r"\s+")


def _is_empty(value: object) -> bool:
    """Поле пустое? Пробельная строка — тоже пустое: заполнением её считать нельзя."""
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    return False


def _same(current: object, parsed: object) -> bool:
    """Значения совпадают по сути? Регистр и лишние пробелы расхождением не считаем.

    Иначе «Золото, Серебро» против «Золото, серебро» уехало бы в секцию расхождений и
    утопило в шуме те две-три позиции, где музей действительно правил карточку руками.
    """
    if isinstance(current, str) and isinstance(parsed, str):
        return _WS_RE.sub(" ", current.strip()).casefold() == _WS_RE.sub(" ", parsed.strip()).casefold()
    return current == parsed


def _skip_family(reason: str) -> str:
    """Причина без цитаты строки: «…неопознанный сегмент «Х»» → «…неопознанный сегмент»."""
    return reason.split("«")[0].strip().rstrip(":").strip() or reason


def _hall_number(rec: dict) -> Optional[int]:
    """Номер зала: из обхода по залам, иначе из карточки (``--ids`` списочную выдачу не читает)."""
    value = rec.get("hall_number")
    if value is not None:
        return value
    return (rec.get("hall") or {}).get("hall_number")


def _is_technique(token: str) -> bool:
    low = token.strip().lower()
    return low in TECHNIQUES or low.startswith(TECHNIQUE_PREFIXES)


def _material_tokens(value: str) -> List[str]:
    return [token.strip() for token in value.split(",") if token.strip()]


def build_plan(records: Iterable[dict], clean_material: bool = False) -> Plan:
    """Собрать план правок по записям каталога. Ни сети, ни БД — только словари.

    Поля, которых в записи нет (карточка не прочиталась), считаются пустыми: «нет ключа» и
    «пустое значение» здесь одно и то же, а значит поле будет дозаполнено. Это безопасно —
    админ-API вернёт 200 и запишет ровно то, что мы разобрали.
    """
    plan = Plan()
    for rec in records:
        exhibit_id = rec.get("id")
        if exhibit_id is None:
            continue
        plan.scanned += 1
        title = rec.get("name") or f"id={exhibit_id}"
        hall = _hall_number(rec)
        line = rec.get(SOURCE_FIELD)

        if not _is_empty(line):
            plan.with_line += 1
            _plan_backfill(plan, rec, exhibit_id, title, hall, str(line))
        if clean_material:
            _plan_material_cleanup(plan, rec, exhibit_id, title, hall)
    return plan


def _plan_backfill(plan: Plan, rec: dict, exhibit_id: int, title: str, hall: Optional[int], line: str) -> None:
    """Разобрать каталожную строку и дозаполнить ПУСТЫЕ поля карточки."""
    parsed = parse_catalog_line(line)
    plan.statuses[parsed.status] += 1
    if parsed.status == STATUS_SKIPPED:
        reason = parsed.notes[0] if parsed.notes else "строка не разобрана"
        plan.skips.append(Skip(exhibit_id, title, hall, reason, line))
        return

    plan.parsed += 1
    plan.precisions[parsed.precision or PRECISION_NONE] += 1
    if parsed.notes:
        plan.notes.append((exhibit_id, parsed.notes))

    for field_name in FIELD_TITLES:
        value = getattr(parsed, field_name)
        if _is_empty(value):
            continue                      # разбор этого поля не дал — дозаполнять нечем
        current = rec.get(field_name)
        if _is_empty(current):
            plan.changes.append(Change(exhibit_id, title, hall, field_name, current, value, KIND_FILL))
        elif not _same(current, value):
            # Непустое поле не трогаем НИКОГДА: 12.08 фронт применил сюда 38 ручных правок.
            plan.conflicts.append(Conflict(exhibit_id, title, hall, field_name, current, value))


def _plan_material_cleanup(plan: Plan, rec: dict, exhibit_id: int, title: str, hall: Optional[int]) -> None:
    """Вынести технику из ``material`` в ``techniques`` (ключ --clean-material-techniques).

    Это изменение НЕПУСТОГО поля, поэтому оно и спрятано за ключом. Скан всех 1252 карточек
    прода по словарю из 77 техник дал одного нарушителя — id 48 «Царские врата»
    («…, Темпера, Тиснение»): в путеводителе там напечатана запятая вместо «;».
    """
    current = rec.get("material")
    if _is_empty(current) or not isinstance(current, str):
        return
    tokens = _material_tokens(current)
    ambiguous = [t for t in tokens if t.strip().lower() in AMBIGUOUS_TECHNIQUES]
    moved = [t for t in tokens if _is_technique(t) and t.strip().lower() not in AMBIGUOUS_TECHNIQUES]
    if ambiguous:
        plan.reviews.append(Review(
            exhibit_id, title, hall,
            f"«{', '.join(ambiguous)}» в указателе бывает и материалом, и техникой — решает "
            f"позиция относительно «;», а у поля material её нет",
            current,
        ))
    if not moved:
        return
    kept = [t for t in tokens if t not in moved]
    if not kept:
        plan.reviews.append(Review(
            exhibit_id, title, hall,
            "в material одни только техники — чистка оставила бы поле пустым", current,
        ))
        return

    plan.changes.append(Change(
        exhibit_id, title, hall, "material", current, ", ".join(kept), KIND_CLEAN,
        note=f"вынесено из материалов: {', '.join(moved)}",
    ))

    # Техники, которые бэкфилл уже собирается записать этой карточке из каталожной строки,
    # для чистки — «непустое значение»: два источника в одно поле не пишем.
    pending = next(
        (c.after for c in plan.changes
         if c.exhibit_id == exhibit_id and c.field_name == "techniques" and c.kind == KIND_FILL),
        None,
    )
    target = pending if pending is not None else rec.get("techniques")
    value = ", ".join(token.lower() for token in moved)
    if _is_empty(target):
        plan.changes.append(Change(
            exhibit_id, title, hall, "techniques", rec.get("techniques"), value, KIND_CLEAN,
            note="перенесено из материалов",
        ))
        return
    existing = {t.strip().lower() for t in _material_tokens(str(target))}
    if not {t.lower() for t in moved} <= existing:
        plan.conflicts.append(Conflict(
            exhibit_id, title, hall, "techniques", target, value,
            note="техника вынесена из material, но поле techniques уже занято — не перезаписываем",
        ))


# ── Сбор каталога ───────────────────────────────────────────────────────────────────────────
def fetch_records(ids: Sequence[int], limit: Optional[int]) -> List[dict]:
    """Записи каталога для разбора: обход по залам + карточка на каждый экспонат.

    ``include_service=true`` — чтобы в объём попал зал 1 «Парадная лестница»: он помечен
    служебным и в публичном списке залов не появляется (п.1 этой же таски), но экспонат в нём
    настоящий. С ``--ids`` списочная выдача не нужна вовсе — карточка отдаёт все поля разом.
    """
    if ids:
        records = []
        for exhibit_id in ids:
            status, body = api("GET", f"/admin/exhibits/{exhibit_id}")
            if status != 200 or not isinstance(body, dict):
                print(f"  ПРОПУСК id={exhibit_id}: карточка недоступна ({status} {body})", file=sys.stderr)
                continue
            records.append(body)
        return records

    records = []
    for hall in get_all("/halls?include_service=true"):
        for item in get_all(f"/halls/{hall['id']}/exhibits"):
            item["hall_number"] = hall.get("hall_number")
            records.append(item)
    if limit is not None:
        records = records[:limit]

    # short_description, material и techniques списочная выдача (ExhibitSummary) не отдаёт —
    # без карточки разбирать нечего и пустоту поля не проверить.
    total = len(records)
    for index, rec in enumerate(records, 1):
        status, body = api("GET", f"/admin/exhibits/{rec['id']}")
        if status != 200 or not isinstance(body, dict):
            print(f"  ПРОПУСК id={rec['id']}: карточка недоступна ({status} {body})", file=sys.stderr)
            continue
        rec.update(body)
        if index % 200 == 0 or index == total:
            print(f"  … прочитано карточек {index}/{total}", file=sys.stderr)
    return records


# ── Отчёт ───────────────────────────────────────────────────────────────────────────────────
def _short(value: object, width: int = 96) -> str:
    text = "—" if value is None or value == "" else str(value)
    text = text.replace("\n", " ")
    return text if len(text) <= width else text[: width - 1] + "…"


def _hall_title(hall: object) -> str:
    return f"зал {hall}" if hall is not None else "без номера зала"


def _title(field_name: str) -> str:
    return FIELD_TITLES.get(field_name, field_name)


def print_report(plan: Plan, apply: bool, clean_material: bool, max_print: int) -> None:
    print("Применение" if apply else "План (сухой прогон)")
    print(f"Каталог: {BASE}")
    print(f"Просмотрено карточек: {plan.scanned}, с каталожной строкой: {plan.with_line}")

    if plan.with_line:
        share = 100.0 * plan.parsed / plan.with_line
        by_status = ", ".join(
            f"{title}: {plan.statuses.get(key, 0)}" for key, title in STATUS_TITLES.items()
            if plan.statuses.get(key)
        )
        print(f"Разобрано строк: {plan.parsed}/{plan.with_line} ({share:.1f} %)"
              + (f" — {by_status}" if by_status else ""))

    if plan.precisions:
        print("Точность датировок: " + ", ".join(
            f"{PRECISION_TITLES.get(key, key)} — {plan.precisions[key]}"
            for key, _ in plan.precisions.most_common()
        ))

    grouped = plan.by_exhibit()
    print(f"\nПравок: {len(plan.changes)} на карточках: {len(grouped)}")
    if plan.changes:
        by_field = plan.field_counts()
        print("По полям: " + ", ".join(f"{_title(name)} — {count}" for name, count in by_field.most_common()))
        print("По залам: " + ", ".join(
            f"{_hall_title(hall)} — {count}" for hall, count in plan.hall_counts().most_common()
        ))

    print()
    if not plan.changes:
        print("  Правок нет: структурные поля уже заполнены (или строки не разобрались).")
    for index, change in enumerate(plan.changes, 1):
        if index > max_print:
            print(f"  … и ещё {len(plan.changes) - max_print} — полный список: --report-file")
            break
        mark = "~" if change.kind == KIND_CLEAN else "+"
        tail = f"  ({change.note})" if change.note else ""
        print(f"  {mark} id={change.exhibit_id} · {_hall_title(change.hall_number)} · "
              f"{_title(change.field_name)}: {_short(change.before, 40)} → {_short(change.after)}{tail}")

    # Секции ниже — то, что скрипт трогать отказался. Заказчик несёт их в музей.
    print(f"\nРасходится с каталожной строкой — НЕ ТРОГАЛИ ({len(plan.conflicts)}):")
    if not plan.conflicts:
        print("  — нет: заполненные поля совпадают с разбором")
    for conflict in plan.conflicts[:max_print]:
        tail = f"  ({conflict.note})" if conflict.note else ""
        print(f"  ! id={conflict.exhibit_id} · {_hall_title(conflict.hall_number)} · "
              f"{_title(conflict.field_name)}: в поле «{_short(conflict.current)}», "
              f"в строке «{_short(conflict.parsed)}»{tail}")
    if len(plan.conflicts) > max_print:
        print(f"  … и ещё {len(plan.conflicts) - max_print}")

    print(f"\nТребует глаз ({len(plan.reviews)}):")
    if not plan.reviews:
        print("  — нет")
    for review in plan.reviews[:max_print]:
        print(f"  ? id={review.exhibit_id} · {_hall_title(review.hall_number)}: {review.reason}")
        print(f"      material: {_short(review.value, 140)}")

    print(f"\nПропущено — карточку не трогаем вовсе ({len(plan.skips)}):")
    if not plan.skips:
        print("  — нет: разобрались все строки")
    for reason, count in plan.skip_families().most_common():
        print(f"  • {reason}: {count}")
    for skip in plan.skips[:max_print]:
        print(f"    id={skip.exhibit_id} · {_hall_title(skip.hall_number)} · {_short(skip.reason, 120)}")
    if len(plan.skips) > max_print:
        print(f"    … и ещё {len(plan.skips) - max_print} — полный список: --report-file")

    if plan.notes:
        print(f"\nПометки разбора ({len(plan.notes)} карточек) — починенные строки, многочастные "
              f"записи, опечатки указателя:")
        for exhibit_id, notes in plan.notes[:max_print]:
            print(f"  · id={exhibit_id}: {_short('; '.join(notes), 160)}")
        if len(plan.notes) > max_print:
            print(f"  … и ещё {len(plan.notes) - max_print}")

    if not clean_material:
        print("\nТехники в material не проверялись: ключ --clean-material-techniques не задан "
              "(это правка непустого поля).")
    if not apply:
        print("\nЭто сухой прогон. Повторите с --apply.")


def write_report(plan: Plan, path: str) -> None:
    """Список правок файлом — заказчик просил «список применённых замен, откат возможен».

    ``.csv`` — для Excel музея: разделитель «;» и BOM, иначе кириллица приезжает
    кракозябрами. Всё остальное — JSON с полными значениями и сводкой.
    """
    changes = [
        {
            "exhibit_id": c.exhibit_id, "exhibit_name": c.exhibit_name, "hall_number": c.hall_number,
            "field": c.field_name, "field_title": _title(c.field_name),
            "kind": c.kind, "kind_title": KIND_TITLES.get(c.kind, c.kind),
            "before": c.before, "after": c.after, "note": c.note,
        }
        for c in plan.changes
    ]
    conflicts = [
        {
            "exhibit_id": c.exhibit_id, "exhibit_name": c.exhibit_name, "hall_number": c.hall_number,
            "field": c.field_name, "field_title": _title(c.field_name),
            "current": c.current, "parsed": c.parsed, "note": c.note,
        }
        for c in plan.conflicts
    ]
    reviews = [
        {
            "exhibit_id": r.exhibit_id, "exhibit_name": r.exhibit_name, "hall_number": r.hall_number,
            "reason": r.reason, "value": r.value,
        }
        for r in plan.reviews
    ]
    skips = [
        {
            "exhibit_id": s.exhibit_id, "exhibit_name": s.exhibit_name, "hall_number": s.hall_number,
            "reason": s.reason, "reason_family": _skip_family(s.reason), "short_description": s.line,
        }
        for s in plan.skips
    ]

    if path.lower().endswith(".csv"):
        with open(path, "w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.writer(fh, delimiter=";")
            writer.writerow(["статус", "id", "экспонат", "зал", "поле", "было", "стало", "пометка"])
            for row in changes:
                writer.writerow([
                    row["kind_title"], row["exhibit_id"], row["exhibit_name"], row["hall_number"],
                    row["field_title"], row["before"], row["after"], row["note"],
                ])
            for row in conflicts:
                writer.writerow([
                    "расходится", row["exhibit_id"], row["exhibit_name"], row["hall_number"],
                    row["field_title"], row["current"], row["parsed"], row["note"],
                ])
            for row in reviews:
                writer.writerow([
                    "требует глаз", row["exhibit_id"], row["exhibit_name"], row["hall_number"],
                    "материалы", row["value"], "", row["reason"],
                ])
            for row in skips:
                writer.writerow([
                    "пропущено", row["exhibit_id"], row["exhibit_name"], row["hall_number"],
                    "краткое описание", row["short_description"], "", row["reason"],
                ])
    else:
        doc = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "base_url": BASE,
            "summary": {
                "scanned": plan.scanned,
                "with_line": plan.with_line,
                "parsed": plan.parsed,
                "skipped": len(plan.skips),
                "changes": len(plan.changes),
                "exhibits": len(plan.by_exhibit()),
                "by_status": {STATUS_TITLES.get(k, k): v for k, v in plan.statuses.items()},
                "by_precision": {PRECISION_TITLES.get(k, k): v for k, v in plan.precisions.items()},
                "by_field": {_title(k): v for k, v in plan.field_counts().items()},
                "by_hall": {str(k): v for k, v in plan.hall_counts().items()},
                "skip_reasons": dict(plan.skip_families()),
            },
            "changes": changes,
            "conflicts": conflicts,
            "needs_review": reviews,
            "skipped": skips,
            "notes": [{"exhibit_id": exhibit_id, "notes": list(notes)} for exhibit_id, notes in plan.notes],
        }
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False, indent=2)
    print(f"\nСписок правок: {os.path.abspath(path)}")


# ── Применение и откат ──────────────────────────────────────────────────────────────────────
def _default_rollback_path() -> str:
    return f"catalog_backfill_rollback_{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"


def apply_plan(plan: Plan, rollback_path: str) -> int:
    """Применить план: один PATCH на карточку, только изменившимися полями.

    ``short_description`` в патч не попадает по построению — значит ``_autofill_spoken`` на
    бэкенде не срабатывает и озвучка карточки остаётся ручной (E15).

    Файл отката пишется в ``finally``: даже если прогон свалился на середине, откатывать уже
    применённые правки чем-то надо.
    """
    log = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "base_url": BASE,
        "items": [],
    }
    errors = 0
    try:
        for exhibit_id, items in plan.by_exhibit().items():
            patch = {change.field_name: change.after for change in items}
            before = {change.field_name: change.before for change in items}
            status, body = api("PATCH", f"/admin/exhibits/{exhibit_id}", patch)
            if status != 200:
                print(f"  ОШИБКА id={exhibit_id}: {status} {body}")
                errors += 1
                continue
            log["items"].append({
                "exhibit_id": exhibit_id,
                "exhibit_name": items[0].exhibit_name,
                "hall_number": items[0].hall_number,
                "before": before,
                "after": patch,
            })
            fields = ", ".join(_title(change.field_name) for change in items)
            print(f"  ~ id={exhibit_id}: {fields}")
    finally:
        with open(rollback_path, "w", encoding="utf-8") as fh:
            json.dump(log, fh, ensure_ascii=False, indent=2)
        print(f"\nЗаполнено карточек: {len(log['items'])}")
        print(f"Файл отката: {os.path.abspath(rollback_path)}")
    return errors


def run_rollback(path: str, apply: bool) -> int:
    """Вернуть исходные значения по файлу отката.

    Поле, которое после прогона правили руками (текущее значение не совпадает с тем, что
    записал скрипт), не трогаем: чужую правку молча затирать нельзя — ровно то же правило,
    по которому бэкфилл не перезаписывает непустое. Если поле уже равно исходному —
    пропускаем без шума, поэтому откат можно повторять.
    """
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)

    restored = skipped = errors = 0
    for item in doc.get("items", []):
        exhibit_id = item["exhibit_id"]
        status, card = api("GET", f"/admin/exhibits/{exhibit_id}")
        if status != 200 or not isinstance(card, dict):
            print(f"  ПРОПУСК id={exhibit_id}: карточка недоступна ({status} {card})")
            errors += 1
            continue
        patch: Dict[str, object] = {}
        for field_name, original in item.get("before", {}).items():
            current = card.get(field_name)
            if current == original:
                continue                                   # уже как было — откат идемпотентен
            written = item.get("after", {}).get(field_name)
            if current != written:
                print(f"  ПРОПУСК id={exhibit_id} · {_title(field_name)}: "
                      "значение правили после прогона — разбирайтесь руками")
                skipped += 1
                continue
            patch[field_name] = original
        if not patch:
            continue
        print(f"  ← id={exhibit_id}: " + ", ".join(_title(name) for name in patch))
        restored += 1
        if not apply:
            continue
        status, body = api("PATCH", f"/admin/exhibits/{exhibit_id}", patch)
        if status != 200:
            print(f"    ОШИБКА отката: {status} {body}")
            errors += 1

    print(f"\nВозвращено карточек: {restored}" + (f", пропущено полей: {skipped}" if skipped else ""))
    if not apply:
        print("\nЭто сухой прогон отката. Повторите с --apply.")
    return errors


# ── CLI ─────────────────────────────────────────────────────────────────────────────────────
def _parse_ids(raw: Optional[str]) -> List[int]:
    if not raw:
        return []
    ids = []
    for token in raw.replace(";", ",").split(","):
        token = token.strip()
        if not token:
            continue
        if not token.isdigit():
            raise SystemExit(f"Не похоже на id экспоната: {token!r}")
        ids.append(int(token))
    return ids


def run(args: argparse.Namespace) -> int:
    if args.rollback:
        return 1 if run_rollback(args.rollback, args.apply) else 0

    records = fetch_records(_parse_ids(args.ids), args.limit)
    plan = build_plan(records, clean_material=args.clean_material_techniques)
    print_report(plan, args.apply, args.clean_material_techniques, args.max_print)
    if args.report_file:
        write_report(plan, args.report_file)
    if not args.apply or not plan.changes:
        return 0
    print()
    return 1 if apply_plan(plan, args.rollback_file or _default_rollback_path()) else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="применить изменения (без ключа — сухой прогон)")
    parser.add_argument(
        "--clean-material-techniques", action="store_true",
        help="вынести технику из material в techniques (правка НЕПУСТОГО поля: на проде 12.08 "
             "это ровно одна карточка — id 48 «Царские врата», «Тиснение»)",
    )
    parser.add_argument("--ids", help="разобрать только эти экспонаты: 48,459,540")
    parser.add_argument("--limit", type=int, help="ограничить разбор первыми N карточками каталога")
    parser.add_argument(
        "--report-file", metavar="FILE",
        help="выгрузить список правок: .csv — для Excel музея, иначе JSON",
    )
    parser.add_argument(
        "--rollback-file", default=_default_rollback_path(),
        help="куда писать файл отката при --apply (по умолчанию — с датой в имени, в текущем каталоге)",
    )
    parser.add_argument("--rollback", metavar="FILE", help="вернуть исходные значения по файлу отката")
    parser.add_argument(
        "--max-print", type=int, default=200,
        help="сколько строк печатать в каждой секции (по умолчанию 200; полный список — в --report-file)",
    )
    args = parser.parse_args()
    if args.rollback and (args.ids or args.limit or args.report_file or args.clean_material_techniques):
        parser.error("--rollback несовместим с ключами разбора каталога")
    if args.ids and args.limit:
        parser.error("--ids и --limit вместе не имеют смысла: --ids уже задаёт точный список")
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
