#!/usr/bin/env python3
"""Восстановить карточки, обнулённые PUT-ом админки (баг-репорт 31.08.2026, п. IV-1).

Что случилось
-------------
Музей: «не получилось внести правки в карточку предмета (яйцо "Ренессанс").
Попробовали внести информацию по техникам, после чего из карточки пропало
изображение и описание». Причина — ``crud.replace_exhibit`` брал
``data.model_dump()`` без ``exclude_unset``: поля, которых не было в теле PUT,
приезжали как ``None`` и затирали содержимое БД. Сам PUT уже починен (тем же
``model_fields_set``, что и в ``admin._autofill_spoken``), но данные, потерянные
до починки, back-end сам не вернёт — их и восстанавливает этот скрипт.

Две стадии, обе идемпотентны, обе по умолчанию — сухой прогон
-------------------------------------------------------------
``--stage image`` — БЕЗОПАСНАЯ, применять первой. Догадок ноль: ``exhibits.image_url``
    это зеркало строки ``exhibit_images`` с ``is_primary`` (их вместе пишет
    ``crud.add_exhibit_image`` и вместе снимает ``crud.delete_exhibit_image``), поэтому
    значение берётся из той же строки галереи, которая его туда и положила. Карточки,
    где первичных фото ноль или больше одного, в план не попадают — они уходят в секцию
    «требует глаз».

``--stage description`` — ТОЛЬКО ПОСЛЕ СОГЛАСОВАНИЯ СПИСКА С МУЗЕЕМ. Здесь источник уже
    не сама БД, и текст может разойтись по стилю с тем, что лежало в карточке 31.07.
    Источники в порядке убывания достоверности, ни один не сочиняется:
      1. проза из ``raw_history`` этой же карточки (``restore_descriptions.full_prose`` —
         механизм не дублируем, он там уже отлажен на проде);
      2. карточка «Шедевров коллекции» на fabergemuseum.ru по ``source_url``/``label_slug``
         (разбор — ``scrape_faberge.parse``);
      3. ``db/seed_fabergemuseum.sql`` по ``label_slug``.
    Чего нет ни в одном источнике — печатаем списком музею, а не заполняем.

Чего скрипт не делает
---------------------
* **Не перезаписывает непустое поле. Никогда.** Правило ``backfill_catalog_fields.py``:
  восстанавливаем только пустое. Отсюда же идемпотентность — после ``--apply`` поля
  непусты, второй прогон печатает пустой план и не шлёт ни одного PATCH.
* **Не сочиняет текст.** Нет источника — карточка идёт в отчёт музею.
* **Не трогает прод сам по себе.** Без ``--apply`` не отправляется ни одного PATCH.

Почему отдельный файл, а не ``restore_descriptions.py``: тот чинит ДРУГУЮ поломку — обрезку
импортёром по «…», и его классификатор на обнулённом поле молчит (``short_description = null``
на «…» не заканчивается). Общее — только извлечение прозы, и оно переиспользуется импортом.

Печать плана — ПОИМЁННАЯ: музею нужно увидеть список карточек до прогона, а не количество.

    BASE_URL=… ADMIN_TOKEN=… python scripts/restore_wiped_cards_20260831.py                    # сухой, стадия image
    ... --stage image --apply                                                                   # вернуть фото
    ... --stage description --report-file restore_descriptions_20260831.csv                     # список музею
    ... --stage description --apply                                                             # после согласования
    ... --ids 7,13,28            # точечно      ... --no-site   # без запросов к fabergemuseum.ru
    ... --stage description --sleep 1.0                          # мягче к сайту музея (дефолт 0.3)
    ... --rollback restore_wiped_rollback_20260831-120000.json --apply                          # откат

Требует зависимостей проекта: ``scrape_faberge`` в корне репозитория и
``scripts/restore_descriptions.py`` рядом.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections import Counter, OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import scrape_faberge  # noqa: E402
from restore_descriptions import full_prose  # noqa: E402  — прозу из raw_history уже умеют доставать

BASE = os.environ.get("BASE_URL", "http://localhost:8000").rstrip("/")
TOKEN = os.environ.get("ADMIN_TOKEN", os.environ.get("ADMIN_API_TOKEN", "dev-admin-token"))
UA = "faberge-restore-wiped/1.0"

STAGE_IMAGE = "image"
STAGE_DESCRIPTION = "description"

# Источники значения — в порядке убывания достоверности; порядок важен и для отчёта.
SOURCE_GALLERY = "gallery"
SOURCE_RAW_HISTORY = "raw_history"
SOURCE_SITE = "site"
SOURCE_SEED = "seed"

SOURCE_TITLES: "OrderedDict[str, str]" = OrderedDict((
    (SOURCE_GALLERY, "галерея карточки (is_primary)"),
    (SOURCE_RAW_HISTORY, "проза из raw_history этой карточки"),
    (SOURCE_SITE, "карточка на fabergemuseum.ru"),
    (SOURCE_SEED, "db/seed_fabergemuseum.sql"),
))

FIELD_TITLES: "OrderedDict[str, str]" = OrderedDict((
    ("image_url", "изображение"),
    ("short_description", "описание"),
    ("material", "материалы"),
))

DESCRIPTION_FIELD = "short_description"
SPOKEN_FIELD = "short_description_spoken"

SEED_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "db", "seed_fabergemuseum.sql")

# Слаг сайта из label_slug: в БД он с префиксом импорта и подчёркиваниями
# (`faberge_pasxalnoe_yajczo_shkatulka_renessans`), на сайте — через дефис
# (`.../pasxalnoe-yajczo-shkatulka-renessans`). Сверено по source_url в сиде.
SLUG_PREFIX = "faberge_"


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


# ── Чистое ядро: план восстановления (без сети) ─────────────────────────────────────────────
@dataclass
class Restore:
    """Одно восстановление поля: что вернём и откуда взяли."""
    exhibit_id: int
    exhibit_name: str
    field_name: str
    value: str
    source: str


@dataclass
class Review:
    """Карточка, которую машина трогать не должна: разбирается человек."""
    exhibit_id: int
    exhibit_name: str
    reason: str


@dataclass
class Plan:
    stage: str
    restores: List[Restore] = field(default_factory=list)
    reviews: List[Review] = field(default_factory=list)

    def by_exhibit(self) -> "OrderedDict[int, List[Restore]]":
        grouped: "OrderedDict[int, List[Restore]]" = OrderedDict()
        for item in self.restores:
            grouped.setdefault(item.exhibit_id, []).append(item)
        return grouped

    def source_counts(self) -> "Counter[str]":
        return Counter(item.source for item in self.restores)


def is_blank(value: object) -> bool:
    """Пустое поле карточки: None или строка из одних пробелов (пустая строка — тоже потеря)."""
    return value is None or (isinstance(value, str) and not value.strip())


def primary_image_url(record: dict) -> Tuple[Optional[str], Optional[str]]:
    """URL первичного фото галереи. Второй элемент — причина, по которой брать нельзя.

    Требование «ровно одна строка с is_primary» — не перестраховка: при двух первичных
    неизвестно, какая из них лежала в ``image_url``, а угадывать главное фото за музей
    мы не будем. При нуле восстанавливать попросту не из чего — карточка потеряла и галерею.
    """
    images = [img for img in record.get("images") or [] if isinstance(img, dict)]
    primary = [img for img in images if img.get("is_primary") and not is_blank(img.get("url"))]
    if len(primary) == 1:
        return primary[0]["url"], None
    if not primary:
        return None, f"в галерее {len(images)} фото, но ни одно не первичное"
    return None, f"в галерее {len(primary)} первичных фото — какое лежало в image_url, неизвестно"


def plan_image_stage(records: Iterable[dict]) -> Plan:
    """Стадия image: вернуть ``image_url`` из галереи там, где он пуст, а галерея цела.

    Карточку с ПУСТОЙ галереей молча пропускаем, а не отправляем в «требует глаз»: пустой
    ``image_url`` при пустой галерее — это не след PUT-а, а просто карточка, которой ещё не
    загрузили фото; таких в каталоге много, и они утопили бы отчёт. Отпечаток поломки —
    именно расхождение «image_url пуст, а галерея есть».
    """
    plan = Plan(stage=STAGE_IMAGE)
    for record in records:
        if not is_blank(record.get("image_url")):
            continue                                   # непустое не трогаем — отсюда идемпотентность
        if not (record.get("images") or []):
            continue
        url, reason = primary_image_url(record)
        if url is None:
            plan.reviews.append(Review(record["id"], record.get("name") or "", reason or "нечего восстанавливать"))
            continue
        plan.restores.append(Restore(record["id"], record.get("name") or "", "image_url", url, SOURCE_GALLERY))
    return plan


def site_slug(record: dict) -> Optional[str]:
    """Слаг страницы на fabergemuseum.ru: из ``source_url``, иначе из ``label_slug``."""
    source_url = record.get("source_url") or ""
    if "fabergemuseum.ru" in source_url:
        tail = source_url.rstrip("/").rsplit("/", 1)[-1]
        if tail:
            return tail
    slug = record.get("label_slug") or ""
    if not slug:
        return None
    if slug.startswith(SLUG_PREFIX):
        slug = slug[len(SLUG_PREFIX):]
    return slug.replace("_", "-") or None


def first_paragraph(text: Optional[str]) -> Optional[str]:
    """Первый абзац текста — то, что лежит в ``short_description`` у здоровых карточек.

    Обрезку по длине НЕ делаем сознательно: ``scrape_faberge.parse`` режет свой
    ``short_description`` до 400 символов с «…», а это ровно та поломка, которую чинит
    ``scripts/restore_descriptions.py``. Восстанавливать одну потерю, создавая другую, глупо.
    """
    if not text or not text.strip():
        return None
    return text.strip().split("\n\n")[0].strip() or None


def choose_description(
    record: dict,
    site: Optional[dict] = None,
    seed: Optional[dict] = None,
) -> Tuple[Optional[str], Optional[str]]:
    """Текст описания и его источник — по убыванию достоверности; ничего не выдумываем."""
    prose = full_prose(record.get("raw_history"))
    if prose:
        return prose, SOURCE_RAW_HISTORY
    if site:
        text = first_paragraph(site.get("raw_history")) or (site.get(DESCRIPTION_FIELD) or None)
        if text:
            return text, SOURCE_SITE
    if seed:
        text = seed.get(DESCRIPTION_FIELD) or None
        if text:
            return text, SOURCE_SEED
    return None, None


def choose_material(record: dict, site: Optional[dict] = None, seed: Optional[dict] = None) -> Tuple[Optional[str], Optional[str]]:
    """Материалы: в ``raw_history`` их нет отдельным полем, поэтому только сайт и сид."""
    if site and not is_blank(site.get("material")):
        return site["material"], SOURCE_SITE
    if seed and not is_blank(seed.get("material")):
        return seed["material"], SOURCE_SEED
    return None, None


# Две разные причины, по которым карточка осталась без текста. Различать их обязательно:
# первую разбирает музей руками, вторую лечит повторный прогон.
REVIEW_NO_SOURCE = "текста нет ни в raw_history, ни на сайте музея, ни в сиде — заполнять нечем"
REVIEW_SITE_DOWN = ("текста нет в raw_history и в сиде, а страница на fabergemuseum.ru НЕ ОТВЕТИЛА — "
                    "источник может существовать, повторите прогон")


def plan_description_stage(
    records: Iterable[dict],
    site_lookup: Optional[Dict[str, dict]] = None,
    seed_lookup: Optional[Dict[str, dict]] = None,
    unreachable: Optional[Set[str]] = None,
) -> Plan:
    """Стадия description: вернуть пустые ``short_description`` и ``material``.

    ``site_lookup`` / ``seed_lookup`` — уже собранные справочники (слаг → разобранная
    карточка), чтобы ядро оставалось без сети и проверялось юнит-тестом.

    ``unreachable`` — слаги, страницы которых сайт музея не отдал. Отчёт по этой стадии
    музей разбирает РУКАМИ, и «источника не было» от «источник не ответил» отличаться
    обязано: если сайт начал резать запросы, карточка с живым текстом иначе попадёт в
    список с формулировкой «заполнять нечем» — то есть отчёт будет врать человеку.
    """
    site_lookup = site_lookup or {}
    seed_lookup = seed_lookup or {}
    unreachable = unreachable or set()
    plan = Plan(stage=STAGE_DESCRIPTION)
    for record in records:
        if not is_blank(record.get(DESCRIPTION_FIELD)) and not is_blank(record.get("material")):
            continue
        slug = site_slug(record) or ""
        site = site_lookup.get(slug)
        seed = seed_lookup.get(record.get("label_slug") or "")
        found = False
        if is_blank(record.get(DESCRIPTION_FIELD)):
            text, source = choose_description(record, site, seed)
            if text:
                plan.restores.append(Restore(record["id"], record.get("name") or "", DESCRIPTION_FIELD, text, source or ""))
                found = True
        if is_blank(record.get("material")):
            material, source = choose_material(record, site, seed)
            if material:
                plan.restores.append(Restore(record["id"], record.get("name") or "", "material", material, source or ""))
                found = True
        if not found:
            reason = REVIEW_SITE_DOWN if slug in unreachable else REVIEW_NO_SOURCE
            plan.reviews.append(Review(record["id"], record.get("name") or "", reason))
    return plan


def patch_body(items: Sequence[Restore], record: dict) -> Dict[str, Optional[str]]:
    """Тело PATCH для одной карточки — с гардом против перегенерации озвучки.

    ``admin.patch_exhibit`` зовёт ``_autofill_spoken``: если в запросе меняется
    ``short_description`` и НЕ передан ``short_description_spoken``, бэкенд идёт в LLM и
    переписывает озвучку (E15). Приём — из ``scripts/fix_catalog_typography.py``: текущее
    значение озвучки передаём явно (в том числе ``null``), тогда автогенерация не срабатывает.
    Здесь это важнее обычного: восстановление трогает десятки карточек разом, а ответ LLM
    недетерминирован и файл отката ручную озвучку музея уже не вернёт.
    """
    body: Dict[str, Optional[str]] = {item.field_name: item.value for item in items}
    if DESCRIPTION_FIELD in body and SPOKEN_FIELD not in body:
        body[SPOKEN_FIELD] = record.get(SPOKEN_FIELD)
    return body


# ── Источник 3: сид fabergemuseum ───────────────────────────────────────────────────────────
_INSERT_RE = re.compile(r"INSERT INTO exhibits \((?P<cols>[^)]*)\) VALUES\s*(?P<body>.*?);\s*\n", re.S)


def _split_sql_tuples(body: str) -> List[List[Optional[str]]]:
    """Разобрать список VALUES-кортежей в значения.

    Свой мини-разбор, а не split по запятым: описания в сиде многострочные и содержат и
    запятые, и скобки, и удвоенные апострофы. Состояние всего два — «внутри строки» и «вне».
    """
    rows: List[List[Optional[str]]] = []
    row: List[Optional[str]] = []
    token = ""          # накопитель НЕстрокового литерала: число или NULL
    depth = 0           # 0 — между кортежами, 1 — внутри кортежа
    i = 0
    while i < len(body):
        ch = body[i]
        if ch == "'":                                   # строковый литерал — читаем целиком
            i += 1
            value = ""
            while i < len(body):
                if body[i] == "'":
                    if i + 1 < len(body) and body[i + 1] == "'":   # '' — экранированный апостроф
                        value += "'"
                        i += 2
                        continue
                    i += 1
                    break
                value += body[i]
                i += 1
            row.append(value)
            token = ""
            continue
        if ch == "(" and depth == 0:
            depth, row, token = 1, [], ""
            i += 1
            continue
        if depth == 1 and ch in ",)":
            literal = token.strip()
            if literal:                                 # пусто — значит значение уже добавила строковая ветка
                row.append(None if literal.upper() == "NULL" else literal)
            token = ""
            if ch == ")":
                depth = 0
                rows.append(row)
                row = []
            i += 1
            continue
        token += ch
        i += 1
    return rows


def parse_seed_exhibits(sql_text: str) -> Dict[str, dict]:
    """Слаг → {short_description, material, raw_history} из ``db/seed_fabergemuseum.sql``."""
    match = _INSERT_RE.search(sql_text)
    if not match:
        return {}
    columns = [c.strip() for c in match.group("cols").split(",")]
    out: Dict[str, dict] = {}
    for values in _split_sql_tuples(match.group("body")):
        if len(values) != len(columns):
            continue
        rec = dict(zip(columns, values))
        slug = rec.get("label_slug")
        if slug:
            out[slug] = rec
    return out


def load_seed(path: str) -> Dict[str, dict]:
    if not os.path.exists(path):
        print(f"  сид не найден: {path} — источник 3 пропущен")
        return {}
    with open(path, encoding="utf-8") as fh:
        return parse_seed_exhibits(fh.read())


def load_site(records: Sequence[dict], sleep: float = 0.3, retries: int = 1) -> Tuple[Dict[str, dict], Set[str]]:
    """Скачать и разобрать карточки сайта. Возвращает (справочник, слаги, которые не ответили).

    Сайт музея — чужой и живой: прогон ``--stage description`` без ``--ids`` это сотни
    страниц подряд. Поэтому пауза между запросами (``--sleep``, дефолт 0.3 — как в
    ``scrape_faberge.py``, который эти же страницы уже качал) и один повтор на случай
    разовой сетевой ошибки.

    Неудачи не глотаем построчно, а СОБИРАЕМ: их число печатается итогом («сайт: не
    скачалось N из M»), а сами слаги уезжают в ``plan_description_stage`` — карточка, чей
    источник просто не ответил, обязана попасть в отчёт с другой формулировкой, чем
    карточка, у которой источника нет вовсе.
    """
    out: Dict[str, dict] = {}
    failed: Set[str] = set()
    slugs: List[str] = []
    for record in records:
        slug = site_slug(record)
        if slug and slug not in slugs:
            slugs.append(slug)
    for index, slug in enumerate(slugs):
        if index and sleep > 0:
            time.sleep(sleep)                    # пауза ПЕРЕД запросом, кроме самого первого
        for attempt in range(retries + 1):
            try:
                out[slug] = scrape_faberge.parse(slug, scrape_faberge.fetch(slug))
                break
            except Exception as exc:  # noqa: BLE001 — сайт музея не наш, недоступность не должна ронять прогон
                if attempt < retries:
                    if sleep > 0:
                        time.sleep(sleep)
                    continue
                failed.add(slug)
                print(f"  сайт: {slug} — не скачался ({exc})")
    if slugs:
        print(f"  сайт: не скачалось {len(failed)} из {len(slugs)}")
    return out, failed


# ── Сбор каталога ───────────────────────────────────────────────────────────────────────────
def fetch_records(ids: Sequence[int], limit: Optional[int]) -> List[dict]:
    """Карточки для разбора. Списочная выдача не отдаёт ни описания, ни галереи — идём в карточки.

    Без ``--ids`` берём весь каталог через публичный ``GET /exhibits``, затем читаем каждую
    карточку админской ручкой: нам нужны ``raw_history`` и ``short_description_spoken``,
    которых в публичном представлении нет.
    """
    if not ids:
        ids = [item["id"] for item in get_all("/exhibits")]
        if limit:
            ids = ids[:limit]
    records: List[dict] = []
    for exhibit_id in ids:
        status, card = api("GET", f"/admin/exhibits/{exhibit_id}")
        if status != 200 or not isinstance(card, dict):
            print(f"  ПРОПУСК id={exhibit_id}: карточка недоступна ({status} {card})")
            continue
        records.append(card)
    return records


# ── Отчёт ───────────────────────────────────────────────────────────────────────────────────
def _short(value: object, width: int = 96) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= width else text[: width - 1] + "…"


def print_report(plan: Plan, apply: bool, max_print: int = 500) -> None:
    """Поимённый список карточек: музею нужны названия, а не количество."""
    stage_title = "изображения из галереи" if plan.stage == STAGE_IMAGE else "описания и материалы"
    print(f"\n=== Восстановление: {stage_title} ===")
    grouped = plan.by_exhibit()
    print(f"Карточек к восстановлению: {len(grouped)} (правок полей: {len(plan.restores)})")

    for exhibit_id, items in list(grouped.items())[:max_print]:
        print(f"  id={exhibit_id:<5} «{items[0].exhibit_name}»")
        for item in items:
            print(f"      {FIELD_TITLES.get(item.field_name, item.field_name):<12} ← "
                  f"{SOURCE_TITLES.get(item.source, item.source)}: {_short(item.value)}")
    if len(grouped) > max_print:
        print(f"  … и ещё {len(grouped) - max_print} карточек (полный список — в --report-file)")

    if plan.restores:
        print("\n  Источники:")
        for source, count in plan.source_counts().most_common():
            print(f"    {SOURCE_TITLES.get(source, source):<34} {count}")

    if plan.reviews:
        print(f"\n=== Требует глаз: {len(plan.reviews)} ===")
        for review in plan.reviews[:max_print]:
            print(f"  id={review.exhibit_id:<5} «{review.exhibit_name}» — {review.reason}")
        if len(plan.reviews) > max_print:
            print(f"  … и ещё {len(plan.reviews) - max_print}")

    if not apply:
        print("\nЭто сухой прогон — на прод не отправлено ничего. Повторите с --apply.")
    if plan.stage == STAGE_DESCRIPTION and not apply:
        print("Стадию описаний применяем ТОЛЬКО после того, как музей посмотрел этот список:")
        print("источник текста — не резервная копия БД, а raw_history / сайт музея / сид.")
        print("Пустое описание бывает и у карточек, которых поломка не касалась: чтобы разбирать")
        print("только пострадавшие, передайте --ids со списком из отчёта стадии image.")


def write_report(plan: Plan, path: str) -> None:
    rows = [
        {
            "exhibit_id": item.exhibit_id,
            "exhibit_name": item.exhibit_name,
            "field": FIELD_TITLES.get(item.field_name, item.field_name),
            "source": SOURCE_TITLES.get(item.source, item.source),
            "value": item.value,
        }
        for item in plan.restores
    ]
    if path.lower().endswith(".csv"):
        with open(path, "w", encoding="utf-8-sig", newline="") as fh:   # utf-8-sig — чтобы Excel не ломал кириллицу
            writer = csv.DictWriter(fh, fieldnames=["exhibit_id", "exhibit_name", "field", "source", "value"])
            writer.writeheader()
            writer.writerows(rows)
    else:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"stage": plan.stage, "restores": rows,
                       "reviews": [vars(r) for r in plan.reviews]}, fh, ensure_ascii=False, indent=2)
    print(f"\nСписок восстановлений: {os.path.abspath(path)}")


# ── Применение и откат ──────────────────────────────────────────────────────────────────────
def _default_rollback_path() -> str:
    return f"restore_wiped_rollback_{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"


def apply_plan(plan: Plan, records: Sequence[dict], rollback_path: str) -> int:
    """Применить план: один PATCH на карточку. Файл отката пишется даже при обрыве."""
    by_id = {record["id"]: record for record in records}
    log = {"generated_at": datetime.now().isoformat(timespec="seconds"), "base_url": BASE,
           "stage": plan.stage, "items": []}
    errors = 0
    try:
        for exhibit_id, items in plan.by_exhibit().items():
            record = by_id.get(exhibit_id, {})
            body = patch_body(items, record)
            before = {item.field_name: record.get(item.field_name) for item in items}
            status, response = api("PATCH", f"/admin/exhibits/{exhibit_id}", body)
            if status != 200:
                print(f"  ОШИБКА id={exhibit_id}: {status} {response}")
                errors += 1
                continue
            log["items"].append({"exhibit_id": exhibit_id, "exhibit_name": items[0].exhibit_name,
                                 "before": before, "after": {i.field_name: i.value for i in items}})
            print(f"  ~ id={exhibit_id}: " + ", ".join(FIELD_TITLES.get(i.field_name, i.field_name) for i in items))
    finally:
        with open(rollback_path, "w", encoding="utf-8") as fh:
            json.dump(log, fh, ensure_ascii=False, indent=2)
        print(f"\nВосстановлено карточек: {len(log['items'])}")
        print(f"Файл отката: {os.path.abspath(rollback_path)}")
    return errors


_MISSING = object()      # «поля нет в ответе» — не то же самое, что «поле равно None»


def rollback_body(item: dict, card: dict) -> Tuple[Dict[str, Optional[str]], List[str]]:
    """Что вернуть у одной карточки и какие поля пришлось пропустить.

    Поле, которое после прогона правили руками (текущее значение не совпадает с тем, что
    записал скрипт), не трогаем: чужую правку молча затирать нельзя — то же правило, по
    которому мы не перезаписываем непустое. Поле, уже равное исходному, пропускаем без
    шума — поэтому откат можно повторять.

    Вынесено из ``run_rollback`` отдельной чистой функцией: решение «что вернём» проверяется
    юнит-тестом без сети, а в ``run_rollback`` остаются только запросы и счётчики.
    """
    body: Dict[str, Optional[str]] = {}
    skipped: List[str] = []
    for field_name, original in item.get("before", {}).items():
        current = card.get(field_name)
        if current == original:
            continue                                    # уже как было — откат идемпотентен
        if current != item.get("after", {}).get(field_name):
            skipped.append(field_name)
            continue
        body[field_name] = original
    return body, skipped


def unrolled_fields(wanted: Dict[str, Optional[str]], response: object) -> List[str]:
    """Поля, которые сервер НЕ откатил, — по телу ответа, а не по коду 200.

    Проверять код бесполезно: бэкенд имеет право поправить присланное. Ровно так и было
    у стадии ``image`` — в файле отката ``before={"image_url": null}``, откат слал
    ``PATCH {"image_url": null}``, а сервер подставлял URL из галереи и отвечал 200. В БД
    не менялось ничего, а скрипт рапортовал успех. Единственный надёжный признак — что
    лежит в карточке ПОСЛЕ запроса.

    Поле, которого в ответе нет вовсе, тоже считаем неоткаченным: «не смогли проверить» и
    «откатилось» — разные вещи, и молча выдавать первое за второе мы больше не будем.
    """
    if not isinstance(response, dict):
        return sorted(wanted)
    return [name for name, value in wanted.items() if response.get(name, _MISSING) != value]


def run_rollback(path: str, apply: bool) -> int:
    """Вернуть исходные значения по файлу отката.

    Печать и счётчик идут ПОСЛЕ успешного PATCH и проверки ответа: раньше «Возвращено
    карточек: N» печаталось и в сухом прогоне, и при провале запроса, и при молчаливом
    неоткате. Итог печатается тремя числами — возвращено / пропущено / ошибок: одно число
    «возвращено» не даёт отличить чистый прогон от прогона, где половина запросов упала.
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
        body, skipped_fields = rollback_body(item, card)
        for field_name in skipped_fields:
            print(f"  ПРОПУСК id={exhibit_id} · {FIELD_TITLES.get(field_name, field_name)}: "
                  "значение правили после прогона — разбирайтесь руками")
        skipped += len(skipped_fields)
        if not body:
            continue
        names = ", ".join(FIELD_TITLES.get(n, n) for n in body)
        planned += 1
        if not apply:
            print(f"  ← id={exhibit_id}: будет возвращено — {names}")
            continue
        wanted = dict(body)                                 # что именно ждём в ответе
        if DESCRIPTION_FIELD in body:
            body[SPOKEN_FIELD] = card.get(SPOKEN_FIELD)     # тот же гард, что при применении
        status, response = api("PATCH", f"/admin/exhibits/{exhibit_id}", body)
        if status != 200:
            print(f"    ОШИБКА отката id={exhibit_id}: {status} {response}")
            errors += 1
            continue
        stuck = unrolled_fields(wanted, response)
        if stuck:
            print(f"  НЕ ОТКАТИЛОСЬ id={exhibit_id}: "
                  + ", ".join(FIELD_TITLES.get(n, n) for n in stuck)
                  + " — сервер вернул не то, что просили; разбирайтесь руками")
            errors += 1
            continue
        print(f"  ← id={exhibit_id}: {names}")
        restored += 1

    if apply:
        print(f"\nИтог отката: возвращено карточек {restored}, пропущено полей {skipped}, ошибок {errors}")
    else:
        print(f"\nИтог сухого прогона: будет возвращено карточек {planned}, "
              f"пропущено полей {skipped}, ошибок {errors}")
        print("Это сухой прогон отката. Повторите с --apply.")
    return errors


# ── CLI ─────────────────────────────────────────────────────────────────────────────────────
def _parse_ids(raw: Optional[str]) -> List[int]:
    if not raw:
        return []
    ids: List[int] = []
    for chunk in raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if not chunk.isdigit():
            raise SystemExit(f"--ids: «{chunk}» не похоже на id экспоната")
        ids.append(int(chunk))
    return ids


def run(args: argparse.Namespace) -> int:
    if args.rollback:
        return 1 if run_rollback(args.rollback, args.apply) else 0

    records = fetch_records(_parse_ids(args.ids), args.limit)
    if args.stage == STAGE_IMAGE:
        plan = plan_image_stage(records)
    else:
        damaged = [r for r in records if is_blank(r.get(DESCRIPTION_FIELD)) or is_blank(r.get("material"))]
        site: Dict[str, dict] = {}
        unreachable: Set[str] = set()
        if not args.no_site:
            site, unreachable = load_site(damaged, sleep=args.sleep)
        plan = plan_description_stage(
            damaged, site_lookup=site, seed_lookup=load_seed(args.seed_file), unreachable=unreachable,
        )
    print_report(plan, args.apply, args.max_print)
    if args.report_file:
        write_report(plan, args.report_file)
    if not args.apply or not plan.restores:
        return 0
    print()
    return 1 if apply_plan(plan, records, args.rollback_file or _default_rollback_path()) else 0


def build_parser() -> argparse.ArgumentParser:
    """Разбор ключей отдельно от прогона — чтобы умолчания проверялись тестом, а не глазами."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--stage", choices=(STAGE_IMAGE, STAGE_DESCRIPTION), default=STAGE_IMAGE,
                        help="что восстанавливаем: image — фото из галереи (безопасно, по умолчанию); "
                             "description — описания и материалы из внешних источников")
    parser.add_argument("--apply", action="store_true", help="применить изменения (без ключа — сухой прогон)")
    parser.add_argument("--ids", help="разобрать только эти экспонаты: 7,13,28")
    parser.add_argument("--limit", type=int, help="ограничить разбор первыми N карточками каталога")
    parser.add_argument("--no-site", action="store_true",
                        help="не ходить на fabergemuseum.ru (останутся raw_history и сид)")
    parser.add_argument("--sleep", type=float, default=0.3,
                        help="пауза между запросами к fabergemuseum.ru, сек (как в scrape_faberge.py); "
                             "0 — без паузы, но сайт музея чужой и живой")
    parser.add_argument("--seed-file", default=SEED_FILE, help="путь к db/seed_fabergemuseum.sql")
    parser.add_argument("--report-file", metavar="FILE",
                        help="выгрузить список: .csv — для Excel музея, иначе JSON")
    parser.add_argument("--rollback-file", default=None,
                        help="куда писать файл отката при --apply (по умолчанию — с датой в имени)")
    parser.add_argument("--rollback", metavar="FILE", help="вернуть исходные значения по файлу отката")
    parser.add_argument("--max-print", type=int, default=500,
                        help="сколько карточек печатать в каждой секции (полный список — в --report-file)")
    return parser


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.rollback and (args.ids or args.limit or args.report_file):
        parser.error("--rollback несовместим с ключами разбора каталога")
    if args.ids and args.limit:
        parser.error("--ids и --limit вместе не имеют смысла: --ids уже задаёт точный список")
    return args


def main() -> int:
    return run(parse_args())


if __name__ == "__main__":
    sys.exit(main())
