#!/usr/bin/env python3
"""Разовый бэкфилл места создания экспоната (баг-репорт 31.08.2026, п. I-2).

На скриншоте карточки яйца «Ландыши» музей красным дописал, что над названием должны идти
«Расположение экспоната», «Дата создания И МЕСТО» и «Фирма и мастер». Даты в карточке есть,
места не было вовсе — хотя каталожная строка путеводителя начинается именно с него:

    «Санкт-Петербург, 1899–1903. Фирма К. Фаберже, мастер М. Перхин.
     Золото, серебро, сталь, сапфир; штамп, чеканка, гравировка, золочение»

Разбор строки живёт в ``app/services/catalog_line`` (там же тесты и все краевые случаи) и
место выделял давно — ``ParsedLine.origin_place``, — но колонки под него не существовало, и
значение уходило только в отчёт, глазами заказчику. 31.08.2026 колонку завели
(``db/migrations/2026-08-31_exhibit_origin_place.sql``); этот скрипт её наполняет.

Отдельный скрипт, а не ещё одно поле в ``scripts/backfill_catalog_fields.py``, — потому что
это отдельная поставка: колонка новая, прогон по проду будет ровно один, и его отчёт музей
должен читать про место, а не искать место среди правок материалов и техник.

ЧТО СКРИПТ НЕ ДЕЛАЕТ — как и его старший брат, это важнее того, что делает:

* **Не перезаписывает непустое поле. Никогда.** Сегодня колонка пуста у всех карточек, но
  прогон не последний: музей правит карточки руками, и разбор путеводителя 2014 года не
  может быть основанием затереть более свежее решение музея. Расхождение «в поле одно, в
  строке другое» печатается отдельной секцией — это материал для разговора, а не работа
  для машины.
* **Не трогает ``short_description``.** Это ИСТОЧНИК разбора: перепишем его — и повторный
  прогон будет разбирать собственный вывод. Побочная выгода та же, что у бэкфилла полей:
  раз описания нет в патче, ``admin.patch_exhibit`` не зовёт ``_autofill_spoken`` и озвучка
  не уезжает на перегенерацию в LLM (E15).
* **Не пишет пустую строку.** «Места не знаем» и «место пустое» для карточки одно и то же,
  и двух способов записать это быть не должно: пусто — значит поля в патче нет.
* **Не разбирает связную музейную прозу.** ``looks_like_catalog_line`` отсеивает очерки до
  разбора; их карточки остаются как были и уходят в секцию «пропущено».
* **Не чинит содержательные ошибки указателя** и не переводит топонимы в современные
  названия: «Санкт-Петербург» и «С.-Петербург» останутся такими, как напечатаны. Приводить
  их к одному виду — решение музея, а не наше.

Пометку парсера «место … не из словаря топонимов — проверить» скрипт показывает отдельно:
ровно так в отчёт 12.08.2026 попал «Эскиз» из «Карл Брюллов (1799–1852). Эскиз. 1850»
(id 1120) — парсер честно сказал, что не уверен, и правка ушла человеку.

Обход каталога — по залам (``GET /halls?include_service=true`` → ``GET /halls/{id}/exhibits``),
как в ``scripts/backfill_catalog_fields.py``: сводка идёт по залам. ``include_service=true``
стоит не ради конкретного зала, а чтобы объём не зависел от сегодняшних значений
``halls.is_service``: скрипт должен обойти ВЕСЬ каталог независимо от того, какие залы
сейчас скрыты от посетителя (зал 1 «Парадная лестница» этим релизом как раз перестал быть
служебным — п. I-1 баг-репорта 31.08.2026). ``short_description`` и ``origin_place`` списочная
выдача не отдаёт — за ними скрипт идёт в ``GET /admin/exhibits/{id}``. Карточку без витрины
обход по залам не видит (фильтр зала идёт через его витрины) — для таких есть ``--ids``.

    BASE_URL=http://localhost:8000 ADMIN_TOKEN=dev-admin-token \\
        python scripts/backfill_exhibit_origin_place.py            # сухой прогон, весь каталог
    ... --limit 50                                                 # проба на первых 50 карточках
    ... --ids 459,511,540                                          # точечно
    ... --report-file origin_place_20260831.csv                    # список правок заказчику
    ... --apply                                                    # применить
    ... --rollback origin_place_rollback_20260831-120000.json --apply

Идемпотентен: ``parse_catalog_line`` детерминирован, а дозаполняются только ПУСТЫЕ поля —
после ``--apply`` они непусты, и второй прогон печатает пустой план и не шлёт ни одного PATCH.
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
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.catalog_line import STATUS_SKIPPED, parse_catalog_line  # noqa: E402

BASE = os.environ.get("BASE_URL", "http://localhost:8000").rstrip("/")
TOKEN = os.environ.get("ADMIN_TOKEN", os.environ.get("ADMIN_API_TOKEN", "dev-admin-token"))
UA = "faberge-origin-place-backfill/1.0"

FIELD = "origin_place"          # единственное поле, которое скрипт пишет
FIELD_TITLE = "место создания"
SOURCE_FIELD = "short_description"      # источник разбора; в патч не попадает никогда

# Пометка парсера про топоним не из словаря: «место «Эскиз» не из словаря топонимов —
# проверить». Такую карточку заполняем, но обязательно показываем человеку.
_PLACE_NOTE_RE = re.compile(r"^место «.+» не из словаря топонимов")


# ── Сетевой слой (тонкий, один в один с scripts/backfill_catalog_fields.py) ─────────────────
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


# ── Чистое ядро: план правок (без сети; разбор строки и его тесты — в catalog_line) ─────────
@dataclass
class Change:
    """Одна правка: карточке проставляется место, разобранное из каталожной строки."""

    exhibit_id: int
    exhibit_name: str
    hall_number: Optional[int]
    before: object
    after: object
    note: str = ""


@dataclass
class Conflict:
    """Место УЖЕ заполнено и не совпадает с разбором строки. Не чиним — показываем заказчику."""

    exhibit_id: int
    exhibit_name: str
    hall_number: Optional[int]
    current: object
    parsed: object


@dataclass
class Skip:
    """Места из строки не добыли: карточка не меняется, id уходит в отчёт."""

    exhibit_id: int
    exhibit_name: str
    hall_number: Optional[int]
    reason: str
    line: str


@dataclass
class Plan:
    changes: List[Change] = field(default_factory=list)
    conflicts: List[Conflict] = field(default_factory=list)
    skips: List[Skip] = field(default_factory=list)
    scanned: int = 0        # карточек просмотрено
    with_line: int = 0      # из них с непустым short_description
    with_place: int = 0     # из них строк, где место нашлось
    already: int = 0        # из них уже заполненных тем же значением

    def hall_counts(self) -> "Counter[object]":
        return Counter(change.hall_number for change in self.changes)

    def place_counts(self) -> "Counter[str]":
        """Какие места получатся. Заказчику это первая проверка «то ли мы разобрали»."""
        return Counter(str(change.after) for change in self.changes)

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
    """Значения совпадают по сути? Регистр и лишние пробелы расхождением не считаем."""
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
    hall = rec.get("hall") or (rec.get("location") or {})
    return hall.get("hall_number")


def build_plan(records: Iterable[dict]) -> Plan:
    """Собрать план правок по записям каталога. Ни сети, ни БД — только словари."""
    plan = Plan()
    for rec in records:
        exhibit_id = rec.get("id")
        if exhibit_id is None:
            continue
        plan.scanned += 1
        title = rec.get("name") or f"id={exhibit_id}"
        hall = _hall_number(rec)
        line = rec.get(SOURCE_FIELD)
        if _is_empty(line):
            continue                      # каталожной строки нет — разбирать нечего

        plan.with_line += 1
        parsed = parse_catalog_line(str(line))
        if parsed.status == STATUS_SKIPPED:
            reason = parsed.notes[0] if parsed.notes else "строка не разобрана"
            plan.skips.append(Skip(exhibit_id, title, hall, reason, str(line)))
            continue
        if _is_empty(parsed.origin_place):
            # Форма без места («Фирма К. Фаберже. Серебро; чеканка») — не ошибка разбора:
            # в указателе место напечатано не у всех карточек.
            plan.skips.append(Skip(exhibit_id, title, hall, "в каталожной строке нет места", str(line)))
            continue

        plan.with_place += 1
        # Парсер сам сказал, что топоним не из словаря, — заполняем, но показываем человеку.
        note = next((n for n in parsed.notes if _PLACE_NOTE_RE.match(n)), "")
        current = rec.get(FIELD)
        if _is_empty(current):
            plan.changes.append(Change(exhibit_id, title, hall, current, parsed.origin_place, note))
        elif _same(current, parsed.origin_place):
            plan.already += 1
        else:
            plan.conflicts.append(Conflict(exhibit_id, title, hall, current, parsed.origin_place))
    return plan


# ── Сбор каталога ───────────────────────────────────────────────────────────────────────────
def fetch_records(ids: Sequence[int], limit: Optional[int]) -> List[dict]:
    """Записи каталога для разбора: обход по залам + карточка на каждый экспонат."""
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

    # short_description и origin_place списочная выдача (ExhibitSummary) не отдаёт — без
    # карточки разбирать нечего и пустоту поля не проверить.
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


def print_report(plan: Plan, apply: bool, max_print: int) -> None:
    print("Применение" if apply else "План (сухой прогон)")
    print(f"Каталог: {BASE}")
    print(f"Просмотрено карточек: {plan.scanned}, с каталожной строкой: {plan.with_line}, "
          f"место найдено в строке: {plan.with_place}")
    if plan.already:
        print(f"Уже заполнено тем же значением: {plan.already} — повторный прогон их не трогает")

    print(f"\nПравок: {len(plan.changes)}")
    if plan.changes:
        print("По залам: " + ", ".join(
            f"{_hall_title(hall)} — {count}" for hall, count in plan.hall_counts().most_common()
        ))
        print("Какие места получатся: " + ", ".join(
            f"{place} — {count}" for place, count in plan.place_counts().most_common(15)
        ))

    print()
    if not plan.changes:
        print("  Правок нет: место уже проставлено (или в строках его нет).")
    for index, change in enumerate(plan.changes, 1):
        if index > max_print:
            print(f"  … и ещё {len(plan.changes) - max_print} — полный список: --report-file")
            break
        tail = f"  ({change.note})" if change.note else ""
        print(f"  + id={change.exhibit_id} · {_hall_title(change.hall_number)} · "
              f"{FIELD_TITLE}: {_short(change.before, 40)} → {_short(change.after)}{tail}")

    # Секции ниже — то, что скрипт трогать отказался. Заказчик несёт их в музей.
    print(f"\nРасходится с каталожной строкой — НЕ ТРОГАЛИ ({len(plan.conflicts)}):")
    if not plan.conflicts:
        print("  — нет: заполненные места совпадают с разбором")
    for conflict in plan.conflicts[:max_print]:
        print(f"  ! id={conflict.exhibit_id} · {_hall_title(conflict.hall_number)}: "
              f"в поле «{_short(conflict.current)}», в строке «{_short(conflict.parsed)}»")
    if len(plan.conflicts) > max_print:
        print(f"  … и ещё {len(plan.conflicts) - max_print}")

    flagged = [c for c in plan.changes if c.note]
    print(f"\nТребует глаз — топоним не из словаря ({len(flagged)}):")
    if not flagged:
        print("  — нет")
    for change in flagged[:max_print]:
        print(f"  ? id={change.exhibit_id} · {_hall_title(change.hall_number)}: "
              f"«{_short(change.after)}» — {change.note}")

    print(f"\nМеста не добыли — карточку не трогаем вовсе ({len(plan.skips)}):")
    if not plan.skips:
        print("  — нет")
    for reason, count in plan.skip_families().most_common():
        print(f"  • {reason}: {count}")
    if len(plan.skips) > max_print:
        print("    полный список — в --report-file")

    if not apply:
        print("\nЭто сухой прогон. Повторите с --apply.")


def write_report(plan: Plan, path: str) -> None:
    """Список правок файлом. ``.csv`` — для Excel музея (разделитель «;» и BOM), иначе JSON."""
    changes = [
        {
            "exhibit_id": c.exhibit_id, "exhibit_name": c.exhibit_name, "hall_number": c.hall_number,
            "before": c.before, "after": c.after, "note": c.note,
        }
        for c in plan.changes
    ]
    conflicts = [
        {
            "exhibit_id": c.exhibit_id, "exhibit_name": c.exhibit_name, "hall_number": c.hall_number,
            "current": c.current, "parsed": c.parsed,
        }
        for c in plan.conflicts
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
            writer.writerow(["статус", "id", "экспонат", "зал", "было", "стало", "пометка"])
            for row in changes:
                writer.writerow([
                    "дозаполнение", row["exhibit_id"], row["exhibit_name"], row["hall_number"],
                    row["before"], row["after"], row["note"],
                ])
            for row in conflicts:
                writer.writerow([
                    "расходится", row["exhibit_id"], row["exhibit_name"], row["hall_number"],
                    row["current"], row["parsed"], "",
                ])
            for row in skips:
                writer.writerow([
                    "пропущено", row["exhibit_id"], row["exhibit_name"], row["hall_number"],
                    row["short_description"], "", row["reason"],
                ])
    else:
        doc = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "base_url": BASE,
            "summary": {
                "scanned": plan.scanned,
                "with_line": plan.with_line,
                "with_place": plan.with_place,
                "already_filled": plan.already,
                "changes": len(plan.changes),
                "conflicts": len(plan.conflicts),
                "skipped": len(plan.skips),
                "by_hall": {str(k): v for k, v in plan.hall_counts().items()},
                "by_place": dict(plan.place_counts()),
                "skip_reasons": dict(plan.skip_families()),
            },
            "changes": changes,
            "conflicts": conflicts,
            "skipped": skips,
        }
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False, indent=2)
    print(f"\nСписок правок: {os.path.abspath(path)}")


# ── Применение и откат ──────────────────────────────────────────────────────────────────────
def _default_rollback_path() -> str:
    return f"origin_place_rollback_{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"


def apply_plan(plan: Plan, rollback_path: str) -> int:
    """Применить план: один PATCH на карточку, ровно одним полем.

    ``short_description`` в патч не попадает по построению — значит ``_autofill_spoken`` на
    бэкенде не срабатывает и озвучка карточки остаётся ручной (E15).

    Файл отката пишется в ``finally``: даже если прогон свалился на середине, откатывать уже
    применённые правки чем-то надо.
    """
    log = {"generated_at": datetime.now().isoformat(timespec="seconds"), "base_url": BASE, "items": []}
    errors = 0
    try:
        for change in plan.changes:
            status, body = api("PATCH", f"/admin/exhibits/{change.exhibit_id}", {FIELD: change.after})
            if status != 200:
                print(f"  ОШИБКА id={change.exhibit_id}: {status} {body}")
                errors += 1
                continue
            log["items"].append({
                "exhibit_id": change.exhibit_id,
                "exhibit_name": change.exhibit_name,
                "hall_number": change.hall_number,
                "before": {FIELD: change.before},
                "after": {FIELD: change.after},
            })
            print(f"  ~ id={change.exhibit_id}: {FIELD_TITLE} → {_short(change.after)}")
    finally:
        with open(rollback_path, "w", encoding="utf-8") as fh:
            json.dump(log, fh, ensure_ascii=False, indent=2)
        print(f"\nЗаполнено карточек: {len(log['items'])}")
        print(f"Файл отката: {os.path.abspath(rollback_path)}")
    return errors


_MISSING = object()      # «поля нет в ответе» — не то же самое, что «поле равно None»


def rolled_back(original: Optional[str], response: object) -> bool:
    """Убедиться по ТЕЛУ ответа, что сервер действительно записал исходное значение.

    Кода 200 недостаточно: бэкенд вправе поправить присланное (у соседнего
    ``scripts/restore_wiped_cards_20260831.py`` так и вышло — сервер подставлял главное
    фото из галереи и отвечал 200, а скрипт рапортовал успешный откат при неизменившейся
    БД). Отсутствие поля в ответе — тоже «не проверили», то есть не успех.
    """
    if not isinstance(response, dict):
        return False
    return response.get(FIELD, _MISSING) == original


def run_rollback(path: str, apply: bool) -> int:
    """Вернуть исходные значения по файлу отката.

    Поле, которое после прогона правили руками (текущее значение не совпадает с тем, что
    записал скрипт), не трогаем: чужую правку молча затирать нельзя — то же правило, по
    которому бэкфилл не перезаписывает непустое. Если поле уже равно исходному — пропускаем
    без шума, поэтому откат можно повторять.

    Печать и счётчик — ПОСЛЕ успешного PATCH и проверки ответа: иначе «Возвращено карточек»
    росло бы и в сухом прогоне, и при провале запроса. Итог печатается тремя числами —
    возвращено / пропущено / ошибок.
    """
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)

    planned = restored = skipped = errors = 0
    for item in doc.get("items", []):
        exhibit_id = item["exhibit_id"]
        status, card = api("GET", f"/admin/exhibits/{exhibit_id}")
        if status != 200 or not isinstance(card, dict):
            print(f"  ПРОПУСК id={exhibit_id}: карточка недоступна ({status} {card})")
            errors += 1
            continue
        original = item.get("before", {}).get(FIELD)
        current = card.get(FIELD)
        if current == original:
            continue                                       # уже как было — откат идемпотентен
        if current != item.get("after", {}).get(FIELD):
            print(f"  ПРОПУСК id={exhibit_id}: {FIELD_TITLE} правили после прогона — разбирайтесь руками")
            skipped += 1
            continue
        planned += 1
        if not apply:
            print(f"  ← id={exhibit_id}: будет возвращено {FIELD_TITLE} → {_short(original)}")
            continue
        status, body = api("PATCH", f"/admin/exhibits/{exhibit_id}", {FIELD: original})
        if status != 200:
            print(f"    ОШИБКА отката id={exhibit_id}: {status} {body}")
            errors += 1
            continue
        if not rolled_back(original, body):
            print(f"  НЕ ОТКАТИЛОСЬ id={exhibit_id}: сервер вернул не то, что просили "
                  "— разбирайтесь руками")
            errors += 1
            continue
        print(f"  ← id={exhibit_id}: {FIELD_TITLE} → {_short(original)}")
        restored += 1

    if apply:
        print(f"\nИтог отката: возвращено карточек {restored}, пропущено {skipped}, ошибок {errors}")
    else:
        print(f"\nИтог сухого прогона: будет возвращено карточек {planned}, "
              f"пропущено {skipped}, ошибок {errors}")
        print("Это сухой прогон отката. Повторите с --apply.")
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
    plan = build_plan(records)
    print_report(plan, args.apply, args.max_print)
    if args.report_file:
        write_report(plan, args.report_file)
    if not args.apply or not plan.changes:
        return 0
    print()
    return 1 if apply_plan(plan, args.rollback_file or _default_rollback_path()) else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="применить изменения (без ключа — сухой прогон)")
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
    if args.rollback and (args.ids or args.limit or args.report_file):
        parser.error("--rollback несовместим с ключами разбора каталога")
    if args.ids and args.limit:
        parser.error("--ids и --limit вместе не имеют смысла: --ids уже задаёт точный список")
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
