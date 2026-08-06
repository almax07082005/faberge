#!/usr/bin/env python3
"""Разовая машинная чистка типографики каталога экспонатов (баг-репорт 06.08.2026, п.9).

Заказчик: «Почистить опечатки и ошибки в тексте». Полную вычитку текстов делает музей,
скрипт ловит только СИСТЕМНЫЕ дефекты — те, что машина определяет без интерпретации
смысла: прямые кавычки → «ёлочки», «пресс- папье» → «пресс-папье», двойные пробелы,
хвостовые и ведущие пробелы, невидимые символы из PDF/Word. Сами правила и разбор
краевых случаев живут в ``app/services/text_normalize.analyze_typography`` — здесь
только обход каталога через админ-API.

Что нашлось на проде 06.08.2026 (1253 карточки, поле ``name``): 30 названий с прямыми
кавычками при «ёлочках» в остальных, 3 с разорванным дефисом («пресс- папье» id=358,
id=359; «Санкт- Петербурга» id=901) и один невидимый LTR-mark внутри «Морозные узоры»
(id=81), из-за которого карточка не находилась поиском. Описаний в списочной выдаче нет,
за ними скрипт ходит в ``GET /admin/exhibits/{id}`` — там же ``raw_history``, которого
публичная карточка не отдаёт.

Заказчик просил «прогнать сначала в dry-run и приложить список замен — вдруг где-то
кавычки осмысленные» и «список замен сохранён, откат возможен», поэтому:
  • без ``--apply`` не пишем ничего — только печатаем список замен с подсветкой различий;
  • ``--report-file`` выгружает тот же список в JSON или CSV (это и есть «приложить»);
  • при ``--apply`` пишется файл отката с ИСХОДНЫМИ значениями, обратный прогон —
    ``--rollback <file> --apply``.

Строку с НЕПАРНОЙ кавычкой скрипт не трогает вовсе и показывает отдельной секцией
«требует глаз»: превратить одиночную ``"`` в «ёлочку» наугад — значит получить мусор,
который потом никто не найдёт (5" трубы, оборванная цитата, вложенная цитата без пары).

Сшивку названий чистка не ломает: ``recognizer.normalize_name`` (распознавание и импорт
путеводителя) гасит кавычки и лишние пробелы, поэтому «ёлочки» вместо прямых на поиск
и на сопоставление с ML-индексом не влияют.

Залы (``halls``) в объём НЕ входят: п.9 сформулирован про каталог экспонатов, а по слепку
прода названия и описания всех 13 залов уже чистые — ни одной замены. Понадобится —
тот же ``analyze_typography`` применим к ``PATCH /admin/halls/{id}``, но это отдельный
прогон со своим файлом отката: мешать правки залов и экспонатов в один список замен,
который заказчик несёт в музей, не стоит.

    BASE_URL=http://localhost:8000 ADMIN_TOKEN=dev-admin-token \\
        python scripts/fix_catalog_typography.py                     # сухой прогон, весь каталог
    ... --names-only                                                 # только поля из списка, без карточек
    ... --ids 25,358,901                                             # точечно
    ... --report-file typography_20260806.csv                        # список замен заказчику
    ... --apply                                                      # применить
    ... --rollback catalog_typography_rollback_20260806-120000.json --apply

Идемпотентен: ``analyze_typography`` — замыкание (``f(f(x)) == f(x)``), поэтому повторный
прогон после ``--apply`` находит ноль замен.
"""
from __future__ import annotations

import argparse
import csv
import difflib
import json
import os
import sys
import unicodedata
import urllib.error
import urllib.request
from collections import Counter, OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.text_normalize import (  # noqa: E402
    CHANGE_HYPHEN,
    CHANGE_INVISIBLE,
    CHANGE_QUOTES,
    CHANGE_SPACES,
    analyze_typography,
)

BASE = os.environ.get("BASE_URL", "http://localhost:8000").rstrip("/")
TOKEN = os.environ.get("ADMIN_TOKEN", os.environ.get("ADMIN_API_TOKEN", "dev-admin-token"))
UA = "faberge-typography-fix/1.0"

# Текстовые поля карточки: (имя в API, как назвать в отчёте, нужна ли карточка).
# «Нужна ли карточка» — списочная выдача GET /exhibits (ExhibitSummary) отдаёт name и
# master_name, всё остальное только в GET /admin/exhibits/{id}. Поэтому при --names-only
# запрос карточки на каждый из 1253 экспонатов не делается вовсе.
# НЕ трогаем идентификаторы и ссылки (label_slug, image_url, video_url, model_3d_*,
# audio_url, source_url) и exhibit_number: это не проза, а ключи — «типографика» там
# означала бы порчу данных.
FIELDS: Tuple[Tuple[str, str, bool], ...] = (
    ("name", "название", False),
    ("master_name", "мастер", False),
    ("material", "материал", True),
    ("short_description", "краткое описание", True),
    ("short_description_spoken", "озвучка описания", True),
    ("raw_history", "история (для LLM)", True),
)
FIELD_TITLES = {name: title for name, title, _ in FIELDS}
LIST_FIELDS = tuple(name for name, _, needs_card in FIELDS if not needs_card)

DESCRIPTION_FIELD = "short_description"
SPOKEN_FIELD = "short_description_spoken"

# Человеческие имена типов правок для сводки «кавычки: N, дефис: N, пробелы: N».
CHANGE_TITLES = OrderedDict((
    (CHANGE_QUOTES, "кавычки"),
    (CHANGE_HYPHEN, "дефис"),
    (CHANGE_SPACES, "пробелы"),
    (CHANGE_INVISIBLE, "невидимые"),
))


# ── Сетевой слой (тонкий, один в один с scripts/import_guide_showcases.py) ───────────────────
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


# ── Чистое ядро: план замен (без сети; сами правила и их тесты — в text_normalize) ──────────
@dataclass
class Replacement:
    """Одна правка: экспонат, поле, было/стало и какие правила сработали."""

    exhibit_id: int
    exhibit_name: str
    field_name: str
    before: str
    after: str
    changes: Tuple[str, ...]


@dataclass
class Review:
    """Строка, которую скрипт сознательно НЕ ТРОГАЕТ: кавычки не сходятся, нужен человек."""

    exhibit_id: int
    exhibit_name: str
    field_name: str
    text: str


@dataclass
class Plan:
    replacements: List[Replacement] = field(default_factory=list)
    reviews: List[Review] = field(default_factory=list)
    scanned: int = 0                 # карточек просмотрено
    fields_seen: int = 0             # непустых текстовых полей просмотрено
    # Снимок озвучки описания на момент сканирования: нужен, чтобы правка
    # short_description не запустила на бэкенде перегенерацию через LLM (см. _with_spoken_guard).
    spoken: Dict[int, Optional[str]] = field(default_factory=dict)

    def by_exhibit(self) -> "OrderedDict[int, List[Replacement]]":
        """Замены, сгруппированные по экспонату: один PATCH на карточку, а не на поле."""
        grouped: "OrderedDict[int, List[Replacement]]" = OrderedDict()
        for rep in self.replacements:
            grouped.setdefault(rep.exhibit_id, []).append(rep)
        return grouped

    def change_counts(self) -> "Counter[str]":
        counter: "Counter[str]" = Counter()
        for rep in self.replacements:
            counter.update(rep.changes)
        return counter

    def field_counts(self) -> "Counter[str]":
        return Counter(rep.field_name for rep in self.replacements)


def build_plan(records: Iterable[dict], fields: Sequence[str] = tuple(FIELD_TITLES)) -> Plan:
    """Собрать план замен по записям каталога. Ни сети, ни БД — только словари.

    Поле, которого в записи нет (карточку не читали — режим --names-only), молча
    пропускается: «нет ключа» и «пустое значение» здесь одно и то же — правки не будет.
    """
    plan = Plan()
    for rec in records:
        exhibit_id = rec.get("id")
        if exhibit_id is None:
            continue
        plan.scanned += 1
        title = rec.get("name") or f"id={exhibit_id}"
        if SPOKEN_FIELD in rec:
            plan.spoken[exhibit_id] = rec.get(SPOKEN_FIELD)
        for field_name in fields:
            value = rec.get(field_name)
            if not isinstance(value, str) or not value:
                continue
            plan.fields_seen += 1
            result = analyze_typography(value)
            if result.needs_review:
                plan.reviews.append(Review(exhibit_id, title, field_name, value))
                continue
            if result.text != value:
                plan.replacements.append(
                    Replacement(exhibit_id, title, field_name, value, result.text, result.changes)
                )
    return plan


def _with_spoken_guard(patch: Dict[str, Optional[str]], spoken: Optional[str]) -> Dict[str, Optional[str]]:
    """Не дать админке переписать озвучку описания через LLM на косметической правке.

    ``admin.patch_exhibit`` зовёт ``_autofill_spoken``: если в запросе меняется
    ``short_description`` и НЕ передан ``short_description_spoken``, бэкенд идёт в LLM и
    генерирует озвучку заново (E15). Для замены кавычек это лишнее: ответ LLM
    недетерминирован, он затрёт ручную озвучку музея, и файл отката такую потерю уже
    не восстановит — в нём лежит только то, что мы правили сами. Поэтому текущее значение
    передаём явно (в том числе ``null``) — тогда автогенерация не срабатывает.
    """
    if DESCRIPTION_FIELD in patch and SPOKEN_FIELD not in patch:
        patch[SPOKEN_FIELD] = spoken
    return patch


# ── Сбор каталога ───────────────────────────────────────────────────────────────────────────
def fetch_records(ids: Sequence[int], limit: Optional[int], names_only: bool) -> List[dict]:
    """Записи каталога для разбора: из списочной выдачи + (по умолчанию) карточки.

    Лишних запросов не делаем: ``name`` и ``master_name`` есть уже в ``GET /exhibits``,
    карточка нужна только за описаниями. С ``--ids`` списочная выдача не нужна вовсе —
    там нет фильтра по id, а карточка отдаёт все поля разом.
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

    records = get_all("/exhibits")
    if limit is not None:
        records = records[:limit]
    if names_only:
        return records

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


# ── Подсветка различий ──────────────────────────────────────────────────────────────────────
# В tty подсвечиваем инверсией, иначе (перенаправление в файл, CI) обрамляем скобками —
# заказчику список замен чаще приезжает текстом, а не картинкой терминала.
_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
_MARK_OPEN, _MARK_CLOSE = ("\033[7m", "\033[27m") if _COLOR else ("⟦", "⟧")
_VISIBLE_ESCAPES = {"\n": "⏎", "\t": "⇥"}


def _visible(text: str) -> str:
    """Сделать видимым невидимое: перенос строки, табуляцию, zero-width и bidi-марки.

    Без этого правка вида «убрали U+200E» выглядит в отчёте как «было X, стало X» —
    именно так и терялся id=81 «Морозные узоры».
    """
    out: List[str] = []
    for ch in text:
        if ch in _VISIBLE_ESCAPES:
            out.append(_VISIBLE_ESCAPES[ch])
            continue
        category = unicodedata.category(ch)
        if category in ("Cf", "Cc", "Zl", "Zp") or (category == "Zs" and ch != " "):
            out.append(f"\\u{ord(ch):04x}")
            continue
        out.append(ch)
    return "".join(out)


def mark_diff(before: str, after: str, context: int = 28) -> Tuple[str, str]:
    """Пара строк «было/стало» с обрамлёнными различиями и вырезанной серединой.

    Длинные совпадающие куски схлопываются в «…», иначе диффы по ``raw_history``
    (там бывает по нескольку тысяч знаков) заливают консоль текстом без правок.
    """
    left_text, right_text = _visible(before), _visible(after)
    matcher = difflib.SequenceMatcher(None, left_text, right_text, autojunk=False)
    left: List[str] = []
    right: List[str] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            chunk = left_text[i1:i2]
            if len(chunk) > 2 * context + 5:
                chunk = f"{chunk[:context]} … {chunk[-context:]}"
            left.append(chunk)
            right.append(chunk)
            continue
        if i2 > i1:
            left.append(f"{_MARK_OPEN}{left_text[i1:i2]}{_MARK_CLOSE}")
        if j2 > j1:
            right.append(f"{_MARK_OPEN}{right_text[j1:j2]}{_MARK_CLOSE}")
    return "".join(left), "".join(right)


def _changes_title(changes: Sequence[str]) -> str:
    return ", ".join(CHANGE_TITLES.get(change, change) for change in changes)


def _short(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


# ── Отчёт ───────────────────────────────────────────────────────────────────────────────────
def print_report(plan: Plan, apply: bool, max_print: int) -> None:
    print("Применение" if apply else "План (сухой прогон)")
    print(f"Каталог: {BASE}")
    print(f"Просмотрено карточек: {plan.scanned}, непустых текстовых полей: {plan.fields_seen}")

    counts = plan.change_counts()
    summary = ", ".join(f"{title}: {counts.get(key, 0)}" for key, title in CHANGE_TITLES.items())
    print(f"\nЗамен: {len(plan.replacements)} — {summary}")
    if plan.replacements:
        by_field = plan.field_counts()
        print("По полям: " + ", ".join(
            f"{FIELD_TITLES.get(name, name)} — {by_field[name]}" for name, _ in by_field.most_common()
        ))
        print(f"Карточек затронуто: {len(plan.by_exhibit())}")

    print()
    if not plan.replacements:
        print("  Замен нет: типографика каталога уже чистая.")
    for index, rep in enumerate(plan.replacements, 1):
        if index > max_print:
            print(f"  … и ещё {len(plan.replacements) - max_print} — полный список: --report-file")
            break
        before, after = mark_diff(rep.before, rep.after)
        print(f"  id={rep.exhibit_id} · {FIELD_TITLES.get(rep.field_name, rep.field_name)} · "
              f"{_changes_title(rep.changes)}")
        print(f"    − {before}")
        print(f"    + {after}")

    # Отдельная секция для заказчика: сюда попадает то, что скрипт трогать отказался.
    print(f"\nТребует глаз — кавычки не сходятся, НЕ ТРОГАЛИ ({len(plan.reviews)}):")
    if not plan.reviews:
        print("  — нет: все кавычки в каталоге парные")
    for rev in plan.reviews[:max_print]:
        print(f"  ! id={rev.exhibit_id} · {FIELD_TITLES.get(rev.field_name, rev.field_name)}: "
              f"{_short(_visible(rev.text), 140)}")
    if len(plan.reviews) > max_print:
        print(f"  … и ещё {len(plan.reviews) - max_print}")

    if not apply:
        print("\nЭто сухой прогон. Повторите с --apply.")


def write_report(plan: Plan, path: str) -> None:
    """Список замен файлом — заказчик просил «приложить список замен».

    ``.csv`` — для Excel музея: разделитель «;» и BOM, иначе кириллица приезжает
    кракозябрами, а строки с запятыми в названиях разъезжаются по колонкам.
    Всё остальное — JSON с полными значениями (без обрезки и без подсветки).
    """
    rows = [
        {
            "exhibit_id": rep.exhibit_id,
            "exhibit_name": rep.exhibit_name,
            "field": rep.field_name,
            "field_title": FIELD_TITLES.get(rep.field_name, rep.field_name),
            "changes": list(rep.changes),
            "changes_title": _changes_title(rep.changes),
            "before": rep.before,
            "after": rep.after,
        }
        for rep in plan.replacements
    ]
    reviews = [
        {
            "exhibit_id": rev.exhibit_id,
            "exhibit_name": rev.exhibit_name,
            "field": rev.field_name,
            "field_title": FIELD_TITLES.get(rev.field_name, rev.field_name),
            "text": rev.text,
        }
        for rev in plan.reviews
    ]

    if path.lower().endswith(".csv"):
        with open(path, "w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.writer(fh, delimiter=";")
            writer.writerow(["статус", "id", "экспонат", "поле", "правки", "было", "стало"])
            for row in rows:
                writer.writerow([
                    "замена", row["exhibit_id"], row["exhibit_name"], row["field_title"],
                    row["changes_title"], row["before"], row["after"],
                ])
            for row in reviews:
                writer.writerow([
                    "требует глаз", row["exhibit_id"], row["exhibit_name"], row["field_title"],
                    "непарная кавычка", row["text"], "",
                ])
    else:
        doc = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "base_url": BASE,
            "summary": {
                "scanned": plan.scanned,
                "fields_seen": plan.fields_seen,
                "replacements": len(plan.replacements),
                "needs_review": len(plan.reviews),
                "by_change": {CHANGE_TITLES.get(k, k): v for k, v in plan.change_counts().items()},
                "by_field": {FIELD_TITLES.get(k, k): v for k, v in plan.field_counts().items()},
            },
            "replacements": rows,
            "needs_review": reviews,
        }
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False, indent=2)
    print(f"\nСписок замен: {os.path.abspath(path)}")


# ── Применение и откат ──────────────────────────────────────────────────────────────────────
def _default_rollback_path() -> str:
    return f"catalog_typography_rollback_{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"


def apply_plan(plan: Plan, rollback_path: str) -> int:
    """Применить план: один PATCH на карточку, только изменившимися полями.

    Файл отката пишется в ``finally`` — даже если прогон свалился на середине, откатывать
    уже применённые правки чем-то надо.
    """
    log = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "base_url": BASE,
        "items": [],
    }
    errors = 0
    try:
        for exhibit_id, items in plan.by_exhibit().items():
            spoken = plan.spoken.get(exhibit_id)
            patch = _with_spoken_guard({rep.field_name: rep.after for rep in items}, spoken)
            before = _with_spoken_guard({rep.field_name: rep.before for rep in items}, spoken)
            status, body = api("PATCH", f"/admin/exhibits/{exhibit_id}", patch)
            if status != 200:
                print(f"  ОШИБКА id={exhibit_id}: {status} {body}")
                errors += 1
                continue
            log["items"].append({
                "exhibit_id": exhibit_id,
                "exhibit_name": items[0].exhibit_name,
                "before": before,
                "after": patch,
            })
            fields = ", ".join(FIELD_TITLES.get(rep.field_name, rep.field_name) for rep in items)
            print(f"  ~ id={exhibit_id}: {fields}")
    finally:
        with open(rollback_path, "w", encoding="utf-8") as fh:
            json.dump(log, fh, ensure_ascii=False, indent=2)
        print(f"\nИсправлено карточек: {len(log['items'])}")
        print(f"Файл отката: {os.path.abspath(rollback_path)}")
    return errors


def run_rollback(path: str, apply: bool) -> int:
    """Вернуть исходные значения по файлу отката.

    Поле, которое после прогона правили руками (текущее значение не совпадает с тем, что
    записал скрипт), не трогаем: чужую правку молча затирать нельзя. Если поле уже равно
    исходному — пропускаем без шума, поэтому откат можно повторять.
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
        patch: Dict[str, Optional[str]] = {}
        for field_name, original in item.get("before", {}).items():
            current = card.get(field_name)
            if current == original:
                continue                                   # уже как было — откат идемпотентен
            written = item.get("after", {}).get(field_name)
            if current != written:
                print(f"  ПРОПУСК id={exhibit_id} · {FIELD_TITLES.get(field_name, field_name)}: "
                      "значение правили после прогона — разбирайтесь руками")
                skipped += 1
                continue
            patch[field_name] = original
        if not patch:
            continue
        fields = ", ".join(FIELD_TITLES.get(name, name) for name in patch)
        print(f"  ← id={exhibit_id}: {fields}")
        restored += 1
        if not apply:
            continue
        # Тот же приём, что и при применении: озвучку передаём явно, чтобы откат правки
        # описания не отправил бэкенд за новой генерацией в LLM. Значение берём ТЕКУЩЕЕ —
        # если озвучку правили руками (её мы выше пропустили), откат обязан её сохранить.
        patch = _with_spoken_guard(patch, card.get(SPOKEN_FIELD))
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

    fields = LIST_FIELDS if args.names_only else tuple(FIELD_TITLES)
    plan = build_plan(fetch_records(_parse_ids(args.ids), args.limit, args.names_only), fields)
    print_report(plan, args.apply, args.max_print)
    if args.report_file:
        write_report(plan, args.report_file)
    if not args.apply or not plan.replacements:
        return 0
    print()
    return 1 if apply_plan(plan, args.rollback_file) else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="применить изменения (без ключа — сухой прогон)")
    parser.add_argument(
        "--names-only", action="store_true",
        help="чистить только поля из списочной выдачи (название, мастер) — без запроса карточки "
             "на каждый экспонат; описания при этом не проверяются",
    )
    parser.add_argument("--ids", help="разобрать только эти экспонаты: 25,358,901")
    parser.add_argument("--limit", type=int, help="ограничить разбор первыми N карточками каталога")
    parser.add_argument(
        "--report-file", metavar="FILE",
        help="выгрузить список замен: .csv — для Excel музея, иначе JSON",
    )
    parser.add_argument(
        "--rollback-file", default=_default_rollback_path(),
        help="куда писать файл отката при --apply (по умолчанию — с датой в имени, в текущем каталоге)",
    )
    parser.add_argument("--rollback", metavar="FILE", help="вернуть исходные значения по файлу отката")
    parser.add_argument(
        "--max-print", type=int, default=200,
        help="сколько замен печатать в консоль (по умолчанию 200; полный список — в --report-file)",
    )
    args = parser.parse_args()
    if args.rollback and (args.ids or args.limit or args.names_only or args.report_file):
        parser.error("--rollback несовместим с ключами разбора каталога")
    if args.ids and args.limit:
        parser.error("--ids и --limit вместе не имеют смысла: --ids уже задаёт точный список")
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
