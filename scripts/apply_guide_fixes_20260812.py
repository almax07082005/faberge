#!/usr/bin/env python3
"""Точечные правки каталога по путеводителю (баг-репорт 12.08.2026, п.4, п.6 и п.7.1).

Заказчик: «карточка стоит на одном номере, а описывает другой предмет» (п.4), «карточка
должна соответствовать своей строке указателя» (п.6, Definition of Done) и «датировки
диапазоном, а не выдуманным годом» (п.7.1). Массовый разбор каталожных строк едет отдельно —
здесь только 47 адресных правок, каждая с цитатой из путеводителя и обоснованием. Сам список
лежит рядом декларативно, в ``db/guide_fixes_20260812.json``; скрипт его не сочиняет, а
исполняет, поэтому спорить с музеем можно по JSON, не читая код.

ЧТО ЗДЕСЬ ГЛАВНОЕ — СВЕРКА ``expect_current`` ПЕРЕД ЗАПИСЬЮ.
У каждой правки записано значение, снятое с прода 12.08.2026. Перед PATCH скрипт сравнивает
его с тем, что на карточке сейчас, и пишет ТОЛЬКО при совпадении. Три исхода:
  • совпало  → правка уходит в PATCH;
  • на карточке уже целевое значение → «уже применено», запроса нет. Часть правок фронт внёс
    руками 12.08 (id 163 material, id 134 год, id 124 год), и в файле у них ``expect_current``
    равен ``value`` — такие строки не исчезают из списка, а документируют проверенное
    состояние. На этом же держится идемпотентность: повторный прогон даёт ноль PATCH;
  • не совпало ни с тем, ни с другим → секция «состояние изменилось, требуется решение».
    Карточку правили после снятия слепка, и молча затирать чужой ввод нельзя.

ЧЕГО СКРИПТ НЕ ДЕЛАЕТ.
Не трогает ``label_slug`` — это класс распознавания: recognition берёт whitelist из
``crud.all_label_slugs``, и подмена slug'а перенаправит сканирование этикетки на другой
предмет. Прецедент — scripts/fix_showcase_orphans.py, где стирание label_slug при удалении
сироты признали дефектом. Не трогает фотографии. Поэтому у двух карточек (id 72 и id 144)
поля после правки описывают СВОЙ предмет, а снимок и slug остались от прежнего — они, вместе
с id 67 и id 73, вынесены в секцию «ТРЕБУЕТ ПРОВЕРКИ ГЛАЗАМИ» с указанием, что именно должно
быть на снимке. Это прямой пункт DoD «Фото и распознавание не указывают на чужой предмет»:
машина о содержимом фотографии судить не может, а человек — может. Поля из ``FORBIDDEN_FIELDS``
в файле правок считаются ошибкой конфигурации, скрипт на них падает, а не «пропускает».

ОЗВУЧКА ОПИСАНИЯ. Семь правок меняют ``short_description``, и меняют его ПО СМЫСЛУ, а не
косметически. ``admin.patch_exhibit`` в таком случае перегенерирует ``short_description_spoken``
через LLM (E15) — и это здесь правильно: озвучка, которая произносит «Алексей Иванов, 1911»
поверх исправленной карточки, и есть дефект из баг-репорта. Поэтому, в отличие от
scripts/fix_catalog_typography.py, озвучку мы НЕ прикрываем — но исходное значение кладём в
файл отката, и откат возвращает его явно, чтобы ручной текст музея не потерялся.
По той же причине ``--apply`` требует, чтобы все карточки читались админской ручкой: без
полной карточки озвучку не снять, а «откат возможен» — требование ТЗ.

    BASE_URL=http://localhost:8000 ADMIN_TOKEN=dev-admin-token \\
        python scripts/apply_guide_fixes_20260812.py                  # сухой прогон + отчёт
    ... --ids 72,144                                                  # точечно
    ... --report-file guide_fixes_20260812.csv                        # список замен заказчику
    ... --apply                                                       # применить
    ... --rollback guide_fixes_rollback_20260812-120000.json --apply

Сухой прогон работает и без админ-токена: карточки дочитываются публичным ``GET /exhibits/{id}``
(админ-токена прода у нас нет). ``--apply`` без токена не пройдёт и не должен.

Код возврата: 1, если есть расхождения (``expect_current`` не сошёлся), недоступные карточки
или ошибки PATCH — то есть ровно тогда, когда прогон требует человека.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import urllib.error
import urllib.request
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BASE = os.environ.get("BASE_URL", "http://localhost:8000").rstrip("/")
TOKEN = os.environ.get("ADMIN_TOKEN", os.environ.get("ADMIN_API_TOKEN", "dev-admin-token"))
UA = "faberge-guide-fixes/1.0"

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_FIXES = os.path.join(REPO_ROOT, "db", "guide_fixes_20260812.json")

# Поля карточки, которые правка вправе трогать. Список закрытый: точечная правка по
# путеводителю — это каталожные данные, и ничего кроме.
FIELD_TITLES = OrderedDict((
    ("name", "название"),
    ("exhibit_number", "номер по путеводителю"),
    ("year_created", "год"),
    ("dating", "датировка"),
    ("master_name", "мастер"),
    ("material", "материал"),
    ("short_description", "краткое описание"),
))

# Поля, попадание которых в файл правок — ошибка, а не повод «пропустить строку».
# label_slug: класс распознавания (см. шапку). Медиа и привязка: фотографии переносит человек,
# витрины — ревизия витрин (scripts/fix_showcase_orphans.py), у неё свой файл отката.
FORBIDDEN_FIELDS = ("label_slug", "image_url", "thumbnail_url", "images", "showcase_id", "audio_url")

SPOKEN_FIELD = "short_description_spoken"
DESCRIPTION_FIELD = "short_description"

WRITE = "write"          # expect_current сошёлся, значение отличается — пишем
DONE = "done"            # на карточке уже целевое значение — не шлём ничего
CONFLICT = "conflict"    # текущее значение не совпало ни с ожидаемым, ни с целевым
MISSING = "missing"      # карточка недоступна

STATUS_TITLES = {
    WRITE: "к записи",
    DONE: "уже применено",
    CONFLICT: "состояние изменилось, требуется решение",
    MISSING: "карточка недоступна",
}


# ── Сетевой слой (тонкий, один в один с scripts/fix_catalog_typography.py) ───────────────────
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


# ── Чистое ядро: разбор файла правок и план (без сети — tests/test_guide_fixes.py) ───────────
@dataclass
class Fix:
    """Одна правка из файла плюс то, что скрипт увидел на карточке."""

    exhibit_id: int
    exhibit: str
    where: dict
    field_name: str
    expect_current: object
    value: object
    printed_page: Optional[int]
    quote: str
    verdict: str
    reason: str
    status: str = ""
    current: object = None       # что реально лежит на карточке (после нормализации)

    @property
    def place(self) -> str:
        """«Зал · витрина NN · №N» — как правку ищут в путеводителе и в зале."""
        parts = [str(self.where.get("hall") or "")]
        showcase = self.where.get("showcase")
        parts.append(f"витрина {showcase}" if showcase else "без витрины")
        number = self.where.get("exhibit_number")
        parts.append(f"№{number}" if number else "без номера")
        return " · ".join(p for p in parts if p)


@dataclass
class Plan:
    fixes: List[Fix] = field(default_factory=list)
    rejected: List[dict] = field(default_factory=list)      # правки, забракованные при разборе
    eye_checks: List[dict] = field(default_factory=list)    # «требует проверки глазами»
    partial_read: List[int] = field(default_factory=list)   # карточки, прочитанные без админки

    def by_status(self, status: str) -> List[Fix]:
        return [f for f in self.fixes if f.status == status]

    @property
    def to_write(self) -> List[Fix]:
        return self.by_status(WRITE)

    def patches(self) -> "OrderedDict[int, List[Fix]]":
        """Правки к записи, сгруппированные по экспонату: один PATCH на карточку, а не на поле."""
        grouped: "OrderedDict[int, List[Fix]]" = OrderedDict()
        for fix in self.to_write:
            grouped.setdefault(fix.exhibit_id, []).append(fix)
        return grouped


def normalize(value: object) -> object:
    """Пустая строка и NULL — одно и то же «значения нет».

    API отдаёт незаполненное поле как ``null``, а админка после ручной правки умеет оставить
    пустую строку; сравнивать их как разные — значит поднимать ложные расхождения на ровном месте.
    """
    if isinstance(value, str) and not value.strip():
        return None
    return value


def load_fixes(path: str) -> Tuple[List[Fix], List[dict], List[dict]]:
    """Прочитать декларативный файл правок. Валидация строгая: молчаливых пропусков нет.

    Правку с полем из ``FORBIDDEN_FIELDS`` не «пропускаем», а падаем: такая строка означает, что
    кто-то дописал в файл перенос фото или подмену label_slug, а это ровно то, что в этой задаче
    делать запрещено. Тихий пропуск создал бы иллюзию применённой правки.
    """
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)

    fixes: List[Fix] = []
    for index, raw in enumerate(doc.get("fixes", []), 1):
        field_name = raw.get("field")
        if field_name in FORBIDDEN_FIELDS:
            raise SystemExit(
                f"{path}: правка #{index} (id={raw.get('exhibit_id')}) трогает поле "
                f"«{field_name}» — это запрещено (см. шапку скрипта)."
            )
        if field_name not in FIELD_TITLES:
            raise SystemExit(
                f"{path}: правка #{index} (id={raw.get('exhibit_id')}) — неизвестное поле "
                f"«{field_name}». Разрешены: {', '.join(FIELD_TITLES)}."
            )
        if not raw.get("quote") or not raw.get("reason"):
            raise SystemExit(
                f"{path}: правка #{index} (id={raw.get('exhibit_id')}, поле {field_name}) без "
                "цитаты или обоснования — заказчик получает список замен, а не список догадок."
            )
        fixes.append(Fix(
            exhibit_id=int(raw["exhibit_id"]),
            exhibit=raw.get("exhibit") or f"id={raw.get('exhibit_id')}",
            where=raw.get("where") or {},
            field_name=field_name,
            expect_current=normalize(raw.get("expect_current")),
            value=normalize(raw.get("value")),
            printed_page=raw.get("printed_page"),
            quote=raw.get("quote") or "",
            verdict=raw.get("verdict") or "",
            reason=raw.get("reason") or "",
        ))

    # Одно и то же поле одной карточки дважды — это молча потерянная правка: в PATCH попадёт
    # последняя, а отчёт покажет обе применёнными.
    seen: Dict[Tuple[int, str], int] = {}
    for index, fix in enumerate(fixes, 1):
        key = (fix.exhibit_id, fix.field_name)
        if key in seen:
            raise SystemExit(
                f"{path}: поле «{fix.field_name}» карточки id={fix.exhibit_id} правится дважды "
                f"(строки #{seen[key]} и #{index}) — оставьте одну правку."
            )
        seen[key] = index

    return fixes, list(doc.get("rejected", [])), list(doc.get("needs_eye_check", []))


def build_plan(
    fixes: Sequence[Fix],
    cards: Dict[int, Optional[dict]],
    rejected: Sequence[dict] = (),
    eye_checks: Sequence[dict] = (),
    partial_read: Sequence[int] = (),
) -> Plan:
    """Разложить правки по статусам поверх снимка карточек. Ни сети, ни БД — только словари.

    ``cards`` — ``{exhibit_id: карточка или None}``; None означает «прочитать не удалось».
    Порядок проверок важен: сначала «уже применено», потом «сошлось ожидаемое». Иначе правка,
    у которой ``expect_current == value`` (их внёс фронт 12.08), попала бы в план записи и
    отправляла бы PATCH с тем же значением при каждом прогоне — идемпотентности конец.
    """
    plan = Plan(rejected=list(rejected), eye_checks=list(eye_checks), partial_read=list(partial_read))
    for fix in fixes:
        card = cards.get(fix.exhibit_id)
        if not card:
            fix.status = MISSING
            plan.fixes.append(fix)
            continue
        fix.current = normalize(card.get(fix.field_name))
        if fix.current == fix.value:
            fix.status = DONE
        elif fix.current == fix.expect_current:
            fix.status = WRITE
        else:
            fix.status = CONFLICT
        plan.fixes.append(fix)
    return plan


# ── Сбор карточек ───────────────────────────────────────────────────────────────────────────
def fetch_cards(ids: Iterable[int]) -> Tuple[Dict[int, Optional[dict]], List[int]]:
    """Карточки правимых экспонатов: сначала админской ручкой, при отказе — публичной.

    Админская отдаёт ``short_description_spoken`` и ``raw_history``, публичная — нет. Озвучка
    нужна файлу отката (см. шапку), поэтому публичное чтение помечается «неполным»: сухой прогон
    на нём работает (нам самим прод доступен только на чтение), а ``--apply`` на нём запрещён.
    """
    cards: Dict[int, Optional[dict]] = {}
    partial: List[int] = []
    warned = False
    for exhibit_id in ids:
        status, body = api("GET", f"/admin/exhibits/{exhibit_id}")
        if status == 200 and isinstance(body, dict):
            cards[exhibit_id] = body
            continue
        if status in (401, 403) and not warned:
            warned = True
            print(
                f"  ! админ-доступ к карточкам не принят ({status}): читаю публичным "
                "GET /exhibits/{id}. Для --apply задайте ADMIN_TOKEN.",
                file=sys.stderr,
            )
        status, body = api("GET", f"/exhibits/{exhibit_id}")
        if status == 200 and isinstance(body, dict):
            cards[exhibit_id] = body
            partial.append(exhibit_id)
            continue
        print(f"  ! карточка id={exhibit_id} недоступна ({status} {body})", file=sys.stderr)
        cards[exhibit_id] = None
    return cards, partial


# ── Отчёт ───────────────────────────────────────────────────────────────────────────────────
def _short(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


def _show(value: object) -> str:
    return "— (пусто)" if value is None else repr(value)


def _title(field_name: str) -> str:
    return FIELD_TITLES.get(field_name, field_name)


def print_report(plan: Plan, apply: bool, max_print: int) -> None:
    print("Применение" if apply else "План (сухой прогон)")
    print(f"Каталог: {BASE}")
    counts = {status: len(plan.by_status(status)) for status in (WRITE, DONE, CONFLICT, MISSING)}
    print(f"Правок в файле: {len(plan.fixes)} — " + ", ".join(
        f"{STATUS_TITLES[s]}: {counts[s]}" for s in (WRITE, DONE, CONFLICT, MISSING)
    ))
    print(f"Карточек затронуто: {len(plan.patches())}")

    print(f"\nК записи ({counts[WRITE]}):")
    if not plan.to_write:
        print("  — нечего писать: каталог уже приведён к путеводителю")
    for index, fix in enumerate(plan.to_write, 1):
        if index > max_print:
            print(f"  … и ещё {counts[WRITE] - max_print} — полный список: --report-file")
            break
        page = f"стр. {fix.printed_page}" if fix.printed_page else "стр. ?"
        print(f"  id={fix.exhibit_id} · {_title(fix.field_name)} · {fix.place} · {page}")
        print(f"    − {_show(fix.current)}")
        print(f"    + {_show(fix.value)}")
        print(f"    путеводитель: {_short(fix.quote, 150)}")
        print(f"    почему: {_short(fix.reason, 200)}")

    print(f"\nУже применено, PATCH не шлём ({counts[DONE]}):")
    if not plan.by_status(DONE):
        print("  —")
    for fix in plan.by_status(DONE):
        print(f"  = id={fix.exhibit_id} · {_title(fix.field_name)} · {fix.place}: {_show(fix.value)}")

    # Секция, ради которой скрипт и сверяет expect_current: карточку правили после слепка.
    print(f"\nСОСТОЯНИЕ ИЗМЕНИЛОСЬ, ТРЕБУЕТСЯ РЕШЕНИЕ ({counts[CONFLICT]}):")
    if not plan.by_status(CONFLICT):
        print("  — нет: все карточки в том же состоянии, что и при разборе 12.08.2026")
    for fix in plan.by_status(CONFLICT):
        print(f"  ! id={fix.exhibit_id} · {_title(fix.field_name)} · {fix.place}")
        print(f"      ожидали : {_show(fix.expect_current)}")
        print(f"      на проде: {_show(fix.current)}")
        print(f"      хотели  : {_show(fix.value)}")
        print("      правку НЕ применяю: значение меняли после разбора — решает музей")

    if plan.by_status(MISSING):
        print(f"\nКарточки недоступны ({counts[MISSING]}):")
        for fix in plan.by_status(MISSING):
            print(f"  ! id={fix.exhibit_id} · {_title(fix.field_name)}")

    if plan.rejected:
        print(f"\nОтклонено при разборе, в каталог НЕ вносится ({len(plan.rejected)}):")
        for item in plan.rejected:
            print(f"  − id={item.get('exhibit_id')} · {_title(item.get('field', ''))}: "
                  f"предлагалось {_show(item.get('proposed'))}")
            print(f"      {_short(item.get('reason') or '', 220)}")

    # Прямой пункт DoD: «Фото и распознавание не указывают на чужой предмет».
    print(f"\nТРЕБУЕТ ПРОВЕРКИ ГЛАЗАМИ: фото и label_slug относятся к прежнему предмету "
          f"({len(plan.eye_checks)}):")
    if not plan.eye_checks:
        print("  —")
    for item in plan.eye_checks:
        media = item.get("media") or {}
        marks = []
        if media.get("image_url"):
            marks.append("основное фото")
        if media.get("images"):
            marks.append(f"галерея: {media['images']}")
        if media.get("label_slug"):
            marks.append(f"label_slug={media['label_slug']}")
        print(f"  ! id={item.get('exhibit_id')} — {item.get('exhibit_after_fix')}"
              + (f" [{', '.join(marks)}]" if marks else ""))
        print(f"      на снимке ожидается: {item.get('expected_subject')}")
        print(f"      риск: {item.get('risk')}")
    if plan.eye_checks:
        print("  Ни фото, ни label_slug скрипт не трогал — судить о содержимом снимка машина не может.")

    if plan.partial_read:
        ids = ", ".join(str(i) for i in plan.partial_read)
        print(f"\n! Карточки прочитаны публичной ручкой (без озвучки и raw_history): {ids}")
        print("  Для --apply нужен ADMIN_TOKEN: без полной карточки не собрать файл отката.")

    if not apply:
        print("\nЭто сухой прогон. Повторите с --apply.")


def write_report(plan: Plan, path: str) -> None:
    """Список замен файлом — заказчик просил «список применённых замен сохранён».

    ``.csv`` — для Excel музея: разделитель «;» и BOM, иначе кириллица приезжает кракозябрами.
    Всё остальное — JSON целиком, включая отклонённые правки и секцию «требует глаз»: заказчик
    несёт этот файл в музей, и решения «почему не применили» ему нужны не меньше применённых.
    """
    rows = [
        {
            "status": fix.status,
            "status_title": STATUS_TITLES[fix.status],
            "exhibit_id": fix.exhibit_id,
            "exhibit": fix.exhibit,
            "place": fix.place,
            "field": fix.field_name,
            "field_title": _title(fix.field_name),
            "printed_page": fix.printed_page,
            "expect_current": fix.expect_current,
            "current": fix.current,
            "value": fix.value,
            "quote": fix.quote,
            "verdict": fix.verdict,
            "reason": fix.reason,
        }
        for fix in plan.fixes
    ]

    if path.lower().endswith(".csv"):
        with open(path, "w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.writer(fh, delimiter=";")
            writer.writerow(["статус", "id", "экспонат", "где", "поле", "стр.",
                             "ожидали", "на проде", "станет", "путеводитель", "почему"])
            for row in rows:
                writer.writerow([
                    row["status_title"], row["exhibit_id"], row["exhibit"], row["place"],
                    row["field_title"], row["printed_page"],
                    "" if row["expect_current"] is None else row["expect_current"],
                    "" if row["current"] is None else row["current"],
                    "" if row["value"] is None else row["value"],
                    row["quote"], row["reason"],
                ])
            for item in plan.rejected:
                writer.writerow([
                    "отклонено при разборе", item.get("exhibit_id"), item.get("exhibit"), "",
                    _title(item.get("field", "")), item.get("printed_page"), "", "",
                    item.get("proposed") or "", "", item.get("reason") or "",
                ])
            for item in plan.eye_checks:
                writer.writerow([
                    "требует проверки глазами", item.get("exhibit_id"),
                    item.get("exhibit_after_fix"), "", "фото и label_slug", "", "", "",
                    item.get("expected_subject") or "", "", item.get("risk") or "",
                ])
    else:
        doc = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "base_url": BASE,
            "summary": {
                STATUS_TITLES[s]: len(plan.by_status(s)) for s in (WRITE, DONE, CONFLICT, MISSING)
            },
            "fixes": rows,
            "rejected": list(plan.rejected),
            "needs_eye_check": list(plan.eye_checks),
        }
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False, indent=2)
    print(f"\nСписок замен: {os.path.abspath(path)}")


# ── Применение и откат ──────────────────────────────────────────────────────────────────────
def _default_rollback_path() -> str:
    return f"guide_fixes_rollback_{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"


def apply_plan(plan: Plan, cards: Dict[int, Optional[dict]], rollback_path: str) -> int:
    """Применить план: один PATCH на карточку, только изменившимися полями.

    Файл отката пишется в ``finally`` — даже если прогон свалился на середине, откатывать уже
    применённые правки чем-то надо. В снимок «было» дополнительно кладём озвучку описания:
    бэкенд перегенерирует её сам (E15), и без этой строки откат вернул бы текст описания, а
    озвучку оставил машинной.
    """
    log = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "base_url": BASE,
        "items": [],
    }
    errors = 0
    try:
        for exhibit_id, items in plan.patches().items():
            patch = {fix.field_name: fix.value for fix in items}
            before = {fix.field_name: fix.current for fix in items}
            if DESCRIPTION_FIELD in patch:
                before[SPOKEN_FIELD] = (cards.get(exhibit_id) or {}).get(SPOKEN_FIELD)
            status, body = api("PATCH", f"/admin/exhibits/{exhibit_id}", patch)
            if status != 200:
                print(f"  ОШИБКА id={exhibit_id}: {status} {body}")
                errors += 1
                continue
            log["items"].append({
                "exhibit_id": exhibit_id,
                "exhibit_name": items[0].exhibit,
                "before": before,
                "after": patch,
            })
            fields = ", ".join(_title(fix.field_name) for fix in items)
            print(f"  ~ id={exhibit_id} «{_short(items[0].exhibit, 60)}»: {fields}")
    finally:
        with open(rollback_path, "w", encoding="utf-8") as fh:
            json.dump(log, fh, ensure_ascii=False, indent=2)
        print(f"\nИсправлено карточек: {len(log['items'])}")
        print(f"Файл отката: {os.path.abspath(rollback_path)}")
    return errors


def run_rollback(path: str, apply: bool) -> int:
    """Вернуть исходные значения по файлу отката.

    Поле, которое после прогона правили руками (текущее значение не совпадает с тем, что записал
    скрипт), не трогаем: чужую правку молча затирать нельзя — та же логика, что и при применении.
    Если поле уже равно исходному, пропускаем без шума, поэтому откат можно повторять.
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
        original = dict(item.get("before") or {})
        spoken = original.pop(SPOKEN_FIELD, None)     # не поле правки, а снимок озвучки
        patch: Dict[str, object] = {}
        for field_name, was in original.items():
            current = normalize(card.get(field_name))
            if current == normalize(was):
                continue                                   # уже как было — откат идемпотентен
            written = normalize(item.get("after", {}).get(field_name))
            if current != written:
                print(f"  ПРОПУСК id={exhibit_id} · {_title(field_name)}: значение правили после "
                      "прогона — разбирайтесь руками")
                skipped += 1
                continue
            patch[field_name] = was
        if not patch:
            continue
        # Озвучку возвращаем явно и вместе с описанием: иначе бэкенд снова сходит в LLM и
        # положит машинный текст поверх исходного (в том числе поверх пустого значения).
        if DESCRIPTION_FIELD in patch:
            patch[SPOKEN_FIELD] = spoken
        print(f"  ← id={exhibit_id}: {', '.join(_title(name) for name in patch)}")
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

    fixes, rejected, eye_checks = load_fixes(args.fixes_file)
    wanted = _parse_ids(args.ids)
    if wanted:
        known = {fix.exhibit_id for fix in fixes}
        unknown = [i for i in wanted if i not in known]
        if unknown:
            raise SystemExit(f"В файле правок нет экспонатов: {', '.join(str(i) for i in unknown)}")
        fixes = [fix for fix in fixes if fix.exhibit_id in set(wanted)]
        eye_checks = [e for e in eye_checks if e.get("exhibit_id") in set(wanted)]
        rejected = [r for r in rejected if r.get("exhibit_id") in set(wanted)]

    cards, partial = fetch_cards(sorted({fix.exhibit_id for fix in fixes}))
    plan = build_plan(fixes, cards, rejected, eye_checks, partial)
    print_report(plan, args.apply, args.max_print)
    if args.report_file:
        write_report(plan, args.report_file)

    unresolved = len(plan.by_status(CONFLICT)) + len(plan.by_status(MISSING))
    if not args.apply:
        return 1 if unresolved else 0
    if plan.partial_read:
        # Без админского чтения не снять озвучку — а значит, откат будет неполным.
        print("\nОШИБКА: карточки прочитаны без админ-доступа, применение отменено. "
              "Задайте ADMIN_TOKEN и повторите.", file=sys.stderr)
        return 1
    if not plan.to_write:
        return 1 if unresolved else 0
    print()
    return 1 if (apply_plan(plan, cards, args.rollback_file) or unresolved) else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="применить изменения (без ключа — сухой прогон)")
    parser.add_argument(
        "--fixes-file", default=DEFAULT_FIXES,
        help="файл правок (по умолчанию db/guide_fixes_20260812.json)",
    )
    parser.add_argument("--ids", help="разобрать только эти экспонаты: 72,144")
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
        help="сколько правок печатать в консоль (по умолчанию 200; полный список — в --report-file)",
    )
    args = parser.parse_args()
    if args.rollback and (args.ids or args.report_file):
        parser.error("--rollback несовместим с ключами разбора файла правок")
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
