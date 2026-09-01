#!/usr/bin/env python3
"""Покрытие каталога классами распознавания: «зал → сколько экспонатов, у скольких есть label_slug».

Зачем это вообще считается (вопрос музея 31.08.2026, п. III). Музей написал: «Предметы
распознаются только в Синей гостиной… В остальных залах предметы не распознаются… Фотографии
были сделаны только в Синей гостиной?» Ответить на вопрос про ФОТОСЪЁМКУ бэкенд не может —
индекс принадлежит внешнему ML-сервису и его состав нам не виден. Но у нас есть свой,
отдельный потолок, и вот он измерим: распознавание физически не может привести посетителя
на карточку без ``label_slug``.

Почему именно ``label_slug``, а не фотографии карточки:
  • ``app/crud.py::slug_by_name`` (карта «название ML-сервиса → наша карточка») строится
    с ``where(Exhibit.label_slug.isnot(None))``;
  • ``app/crud.py::all_label_slugs`` (whitelist идентификаторов) — тот же фильтр;
  • аварийный фолбэк в ``app/routers/recognition.py`` («добрать кандидатов поиском»)
    заканчивается на ``[... for e in found if e.label_slug]``.
Карточка без ``label_slug`` не попадёт в ответ ``POST /recognition`` ни при каком качестве
модели и ни при какой съёмке. Поэтому доля карточек со слагом — ПОТОЛОК покрытия, верхняя
граница, а не факт распознавания: чтобы предмет распознался, нужно ещё, чтобы его снимки
лежали в индексе ML-сервиса под сшиваемым названием. Фактическую успешность показывает
``GET /admin/analytics/recognition`` (метрика ``recognition_success_rate``), а не этот скрипт.

Скрипт ТОЛЬКО ЧИТАЕТ. Ключей ``--apply``/``--dry-run`` у него нет и не нужно: он не делает
ни одного запроса на запись, админ-токен ему не требуется — ``label_slug`` входит в публичную
``ExhibitSummary`` (``app/schemas.py``). Это сознательно: ответ музею нужен сейчас, а
прод-БД и админ-токен прода нам недоступны.

Три источника данных, таблица во всех трёх считается одним и тем же кодом:

    # 1. Живой API (по умолчанию — то, чем музей меряет себя сам)
    BASE_URL=https://<gateway> python scripts/recognition_coverage.py
    ... --save snapshot.json         # сохранить сырую выгрузку рядом с отчётом

    # 2. Ранее сохранённый снимок — цифры в документе воспроизводятся без сети
    python scripts/recognition_coverage.py --from-file snapshot.json

    # 3. Сид репозитория — «геном» перекоса, виден без всякого прода
    python scripts/recognition_coverage.py --from-seed db/seed_fabergemuseum.sql

Дополнительно:
    ... --hall-id 4                  # поимённый список карточек БЕЗ label_slug в зале
    ... --find Курочка               # найти карточки по подстроке названия и увидеть,
                                     #   есть ли у них класс распознавания
    ... --collisions                 # бесслаговые карточки, чьё имя уже занято карточкой
                                     #   СО слагом (снимок уводит на одноимённый предмет)
    ... --format json                # то же машиночитаемо

Снимок (``--save``) стоит прикладывать к отчёту музею: цифры каталога дрейфуют с каждой
правкой, и «18,6 % на 31.08.2026» без снимка через месяц не перепроверить.

Устойчивость к 502. Замер 31.08.2026 через гейтвей дважды ловил 502 на середине выгрузки.
Молчаливо оборванная страница даёт ЗАНИЖЕННУЮ таблицу и ложный вывод «в этом зале ничего не
покрыто», поэтому 5xx и таймауты ретраятся, а не-восстановимая ошибка роняет прогон целиком.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Нормализация названий берётся у самого распознавания, а не пишется здесь заново: отчёт
# про сшивку по названию, посчитанный ДРУГИМ приведением строк, показывал бы не то, что
# происходит в проде (так же поступает scripts/import_guide_showcases.py).
from app.services.recognizer import normalize_name  # noqa: E402

BASE = os.environ.get("BASE_URL", "http://localhost:8000").rstrip("/")
UA = "faberge-recognition-coverage/1.0"

# Строка-предупреждение печатается под каждой таблицей. Без неё «18,6 %» уезжает в переписку
# как оценка работы ML-команды, хотя это метрика НАШЕГО каталога.
CEILING_NOTE = (
    "label_slug — необходимое, но НЕ достаточное условие: карточка без него не может быть\n"
    "возвращена распознаванием вовсе, но и наличие слага не гарантирует распознавание —\n"
    "снимки предмета должны лежать в индексе внешнего ML-сервиса, его состав бэкенду не виден.\n"
    "Фактическая успешность — GET /admin/analytics/recognition (recognition_success_rate)."
)

# Подпись строки для карточек, не привязанных ни к какой витрине (а значит и ни к какому залу).
# Такие в проде есть, и терять их в отчёте нельзя: они тоже участвуют в распознавании.
NO_HALL_LABEL = "Вне залов (карточка без витрины)"


# ── Сетевой слой (тонкий, один в один с scripts/backfill_catalog_fields.py) ──────────────────
def api(method: str, path: str, retries: int = 3, pause: float = 2.0) -> Tuple[int, object]:
    """GET публичного API с ретраями на 5xx и сетевых сбоях.

    Ретраим только то, что бывает разовым (5xx гейтвея, таймаут): 4xx повторять
    бессмысленно, а тихо проглотить его нельзя — таблица станет неполной.
    """
    req = urllib.request.Request(
        f"{BASE}{path}", data=None, method=method,
        headers={"Content-Type": "application/json", "User-Agent": UA},
    )
    last: Tuple[int, object] = (0, "")
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=45) as r:
                raw = r.read().decode("utf-8", "replace")
                return r.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            try:
                body: object = json.loads(raw)
            except json.JSONDecodeError:
                body = raw
            if exc.code < 500:
                return exc.code, body
            last = (exc.code, body)
        except Exception as exc:  # noqa: BLE001 — таймаут, DNS, обрыв соединения
            last = (0, str(exc))
        if attempt < retries:
            print(f"  … {path} → {last[0] or 'сетевая ошибка'}, повтор {attempt}/{retries - 1}",
                  file=sys.stderr)
            time.sleep(pause * attempt)
    return last


def get_all(path: str, key: str = "items", page: int = 100) -> List[dict]:
    """Собрать все страницы списочного эндпоинта (page ≤ limit публичного API)."""
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


def fetch_snapshot() -> dict:
    """Снять каталог с живого API. Только GET, только публичные ручки."""
    halls = get_all("/halls?include_service=true")
    exhibits = get_all("/exhibits")
    return {
        "source": BASE,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "halls": halls,
        "exhibits": exhibits,
    }


# ── Чтение сида (ровно те колонки, что нужны таблице) ────────────────────────────────────────
# Имя таблицы берём с самой строки INSERT и НЕ требуем на ней же список колонок: в
# db/seed.sql список колонок перенесён на следующую строку («INSERT INTO exhibits» /
# «(id, showcase_id, …) VALUES»). Со старым выражением такая строка не считалась началом
# нового INSERT'а, разбор оставался на предыдущей таблице и падал на `int('id')`.
_INSERT_RE = re.compile(r"^INSERT INTO (\w+)\b")
_TUPLE_RE = re.compile(r"^\s*\(")


def parse_sql_tuple_prefix(line: str, count: int) -> Optional[List[object]]:
    """Разобрать НАЧАЛО SQL-кортежа: первые `count` значений.

    Намеренно частичный разбор, а не полноценный парсер SQL. Все колонки, нужные отчёту
    (id, ссылка на родителя, номер, label_slug, название), в сиде стоят первыми и целиком
    помещаются на первой строке кортежа; длинные описания с переносами идут дальше и до них
    разбор не доходит. Так парсер не зависит от того, как отформатированы тексты карточек.

    Возвращает None, если строка кортежем не является или оборвалась раньше `count` значений.
    """
    if not _TUPLE_RE.match(line):
        return None
    i = line.index("(") + 1
    values: List[object] = []
    n = len(line)
    while len(values) < count:
        while i < n and line[i] in " \t":
            i += 1
        if i >= n:
            return None
        if line[i] == "'":
            i += 1
            buf: List[str] = []
            while i < n:
                if line[i] == "'":
                    if i + 1 < n and line[i + 1] == "'":  # экранированная кавычка ''
                        buf.append("'")
                        i += 2
                        continue
                    i += 1
                    break
                buf.append(line[i])
                i += 1
            else:
                return None  # строка не закрылась на этой строке файла
            values.append("".join(buf))
        else:
            j = i
            while j < n and line[j] not in ",)":
                j += 1
            token = line[i:j].strip()
            if not token:
                return None
            values.append(None if token.upper() == "NULL" else _maybe_int(token))
            i = j
        while i < n and line[i] in " \t":
            i += 1
        if i < n and line[i] == ",":
            i += 1
        elif len(values) < count:
            return None  # кортеж кончился раньше, чем нужные колонки
    return values


def _maybe_int(token: str) -> object:
    try:
        return int(token)
    except ValueError:
        return token


def load_seed(text: str) -> dict:
    """Собрать снимок каталога из сид-файла ``db/seed_fabergemuseum.sql``.

    Нужен, чтобы таблицу покрытия можно было получить вообще без сети и без прода: сид —
    исходное состояние репозитория, и на нём перекос в сторону Синей гостиной виден в чистом
    виде. Разбираются три INSERT'а (halls / showcases / exhibits) и только их первые колонки.

    ВАЖНО про то, что получится: сид — фикстура наливки, а НЕ срез прода. Совпадение его
    цифр с жалобой музея («распознаётся только Синяя гостиная») — это происхождение перекоса,
    а не его подтверждение; подтверждение снимается только с живого каталога (``--save``).
    """
    halls: List[dict] = []
    showcases: Dict[int, dict] = {}
    exhibits: List[dict] = []
    current: Optional[str] = None
    for line in text.splitlines():
        head = _INSERT_RE.match(line)
        if head:
            current = head.group(1)
            continue
        if current is None or not _TUPLE_RE.match(line):
            continue
        # Строка со списком колонок («(id, showcase_id, label_slug, …) VALUES») выглядит как
        # кортеж, но первым значением у неё стоит ИМЯ колонки, а не число. Отличаем по этому:
        # у настоящих строк данных первым идёт id. Иначе разбор молча считает заголовок
        # записью каталога и портит таблицу.
        if current == "halls":  # (id, hall_number, name, …)
            row = parse_sql_tuple_prefix(line, 3)
            if row and isinstance(row[0], int):
                halls.append({"id": row[0], "hall_number": row[1], "name": row[2]})
        elif current == "showcases":  # (id, hall_id, showcase_number, name)
            row = parse_sql_tuple_prefix(line, 3)
            if row and isinstance(row[0], int):
                showcases[int(row[0])] = {"hall_id": row[1], "showcase_number": row[2]}
        elif current == "exhibits":  # (id, showcase_id, label_slug, name, …)
            row = parse_sql_tuple_prefix(line, 4)
            if row and isinstance(row[0], int):
                exhibits.append({
                    "id": row[0], "showcase_id": row[1],
                    "label_slug": row[2], "name": row[3],
                })
    halls_by_id = {h["id"]: h for h in halls}
    for ex in exhibits:
        sc = showcases.get(ex["showcase_id"]) if ex["showcase_id"] is not None else None
        hall = halls_by_id.get(sc["hall_id"]) if sc else None
        ex["hall_id"] = hall["id"] if hall else None
        ex["showcase_number"] = sc["showcase_number"] if sc else None
    return {"source": "seed", "generated_at": None, "halls": halls, "exhibits": exhibits}


# ── Чистое ядро: нормализация и агрегация (ни сети, ни БД) ───────────────────────────────────
def normalize_exhibit(raw: dict) -> dict:
    """Привести карточку к плоскому виду, понимая обе формы ответа API.

    До 31.08.2026 ``ExhibitSummary`` отдавала расположение только плоскими полями
    (``hall_id``/``showcase_number``), с 31.08.2026 рядом появился объект ``location``
    с названием зала. Скрипт обязан читать оба варианта: снимок, снятый до релиза, должен
    оставаться сравнимым со снимком после него.
    """
    loc = raw.get("location") or {}
    return {
        "id": raw.get("id"),
        "name": raw.get("name") or "",
        "label_slug": raw.get("label_slug"),
        "hall_id": loc.get("hall_id", raw.get("hall_id")),
        "hall_number": loc.get("hall_number", raw.get("hall_number")),
        "hall_name": loc.get("hall_name"),
        "showcase_number": loc.get("showcase_number", raw.get("showcase_number")),
        "exhibit_number": raw.get("exhibit_number"),
    }


@dataclass
class HallCoverage:
    """Строка таблицы: один зал (или «вне залов», когда hall_id пуст)."""

    hall_id: Optional[int]
    hall_number: Optional[int]
    hall_name: str
    exhibits: int = 0
    with_label_slug: int = 0

    @property
    def missing(self) -> int:
        return self.exhibits - self.with_label_slug

    @property
    def coverage_rate(self) -> float:
        # Зал без экспонатов — 0.0, а не деление на ноль и не «100 %»: пустой зал не покрыт.
        return round(self.with_label_slug / self.exhibits, 4) if self.exhibits else 0.0


@dataclass
class Coverage:
    """Готовый отчёт: строки по залам плюс итог по всему каталогу."""

    rows: List[HallCoverage] = field(default_factory=list)
    source: Optional[str] = None
    generated_at: Optional[str] = None

    @property
    def exhibits(self) -> int:
        return sum(r.exhibits for r in self.rows)

    @property
    def with_label_slug(self) -> int:
        return sum(r.with_label_slug for r in self.rows)

    @property
    def missing(self) -> int:
        return self.exhibits - self.with_label_slug

    @property
    def coverage_rate(self) -> float:
        return round(self.with_label_slug / self.exhibits, 4) if self.exhibits else 0.0


def _sort_key(row: HallCoverage) -> Tuple[int, int, int]:
    """Порядок строк: по номеру зала, залы без номера — следом, «вне залов» — последней.

    Ключ полностью числовой: сортировка не должна зависеть от языка и регистра названий.
    """
    if row.hall_id is None:
        return (2, 0, 0)
    if row.hall_number is None:
        return (1, 0, row.hall_id)
    return (0, row.hall_number, row.hall_id)


def build_coverage(snapshot: dict) -> Coverage:
    """Свести снимок каталога в таблицу «зал → экспонатов → с label_slug».

    Знаменатель — ВЕСЬ каталог, включая служебные и временные залы: whitelist распознавания
    (``crud.all_label_slugs``) тоже не смотрит ни на ``halls.is_service``, ни на
    ``is_temporary``, и сужать здесь означало бы считать не тот потолок, который работает
    в проде. Залы без экспонатов из списка залов не выбрасываются: «в зале 0 из 0» —
    осмысленный ответ на вопрос «какие залы покрыты», а молчание — нет.
    """
    halls_by_id: Dict[int, dict] = {}
    for hall in snapshot.get("halls") or []:
        if hall.get("id") is not None:
            halls_by_id[int(hall["id"])] = hall

    rows: Dict[Optional[int], HallCoverage] = {}
    for hall_id, hall in halls_by_id.items():
        rows[hall_id] = HallCoverage(
            hall_id=hall_id,
            hall_number=hall.get("hall_number"),
            hall_name=hall.get("name") or f"зал {hall_id}",
        )

    for raw in snapshot.get("exhibits") or []:
        ex = normalize_exhibit(raw)
        hall_id = ex["hall_id"]
        hall_id = int(hall_id) if hall_id is not None else None
        row = rows.get(hall_id)
        if row is None:
            # Зала нет в списке залов (например, снимок залов снят без include_service):
            # строку всё равно заводим, иначе экспонаты потеряются из знаменателя.
            row = HallCoverage(
                hall_id=hall_id,
                hall_number=ex["hall_number"],
                hall_name=(ex["hall_name"] or (NO_HALL_LABEL if hall_id is None else f"зал {hall_id}")),
            )
            rows[hall_id] = row
        row.exhibits += 1
        if ex["label_slug"]:
            row.with_label_slug += 1

    return Coverage(
        rows=sorted(rows.values(), key=_sort_key),
        source=snapshot.get("source"),
        generated_at=snapshot.get("generated_at"),
    )


def exhibits_without_slug(snapshot: dict, hall_id: Optional[int]) -> List[dict]:
    """Поимённый список карточек без ``label_slug`` — план съёмки для музея по одному залу."""
    out = [normalize_exhibit(raw) for raw in snapshot.get("exhibits") or []]
    out = [e for e in out if not e["label_slug"]]
    if hall_id is not None:
        out = [e for e in out if e["hall_id"] == hall_id]
    return sorted(out, key=lambda e: (e["hall_id"] or 0, e["id"] or 0))


def find_exhibits(snapshot: dict, needle: str) -> List[dict]:
    """Карточки, чьё название содержит подстроку (без учёта регистра и «ё»).

    Нужен для прямой проверки исключений, названных музеем («Курочка», «подвески»,
    «трилистник»): по каждой находке сразу видно, есть ли у неё класс распознавания.
    """
    key = _fold(needle)
    if not key:
        return []
    out = [normalize_exhibit(raw) for raw in snapshot.get("exhibits") or []]
    return [e for e in out if key in _fold(e["name"])]


def _fold(value: Optional[str]) -> str:
    return (value or "").casefold().replace("ё", "е")


def slug_index(snapshot: dict) -> Dict[str, dict]:
    """Карта «нормализованное имя → карточка со слагом», как её строит прод.

    Повторяет цепочку ``crud.slug_by_name`` → ``recognizer.build_name_index``: только
    карточки с ``label_slug``, среди одноимённых — ПЕРВАЯ ПО id. Считать иначе нельзя:
    отчёт должен показывать ту карточку, на которую посетителя уводит реальный код.
    """
    index: Dict[str, dict] = {}
    for raw in sorted(snapshot.get("exhibits") or [], key=lambda e: e.get("id") or 0):
        ex = normalize_exhibit(raw)
        if not ex["label_slug"]:
            continue
        key = normalize_name(ex["name"])
        if key and key not in index:
            index[key] = ex
    return index


def name_collisions(snapshot: dict) -> List[Tuple[dict, dict]]:
    """Пары «карточка без слага → одноимённая карточка со слагом, на которую уведёт снимок».

    Зачем считается. Сшивка с ML-сервисом идёт ПО НАЗВАНИЮ, и ``hall_id``, который принимает
    ``POST /recognition``, на выбор кандидата сегодня не влияет (``recognizer.recognize``
    параметр не использует). Поэтому в чужом зале посетитель не просто «не распознаётся» —
    его может увести на одноимённый предмет в другом зале. Здесь только точные совпадения
    нормализованных имён: нечёткая ветка ``match_title`` зависит от того, какие названия
    лежат в индексе ML-сервиса, а их мы не видим, — гадать в отчёте нельзя.
    """
    index = slug_index(snapshot)
    out: List[Tuple[dict, dict]] = []
    for raw in snapshot.get("exhibits") or []:
        ex = normalize_exhibit(raw)
        if ex["label_slug"]:
            continue
        target = index.get(normalize_name(ex["name"]))
        if target is not None:
            out.append((ex, target))
    return sorted(out, key=lambda pair: (pair[0]["hall_id"] or 0, pair[0]["id"] or 0))


# ── Печать ───────────────────────────────────────────────────────────────────────────────────
def format_table(cov: Coverage) -> str:
    """Текстовая таблица «зал → экспонатов → с label_slug → доля»."""
    header = f"{'№':>3}  {'зал':<32} {'экспонатов':>10} {'с label_slug':>13} {'доля':>7}"
    lines = [header, "-" * len(header)]
    for row in cov.rows:
        number = str(row.hall_number) if row.hall_number is not None else "—"
        name = row.hall_name if len(row.hall_name) <= 32 else row.hall_name[:31] + "…"
        lines.append(
            f"{number:>3}  {name:<32} {row.exhibits:>10} {row.with_label_slug:>13} "
            f"{row.coverage_rate * 100:>6.1f}%"
        )
    lines.append("-" * len(header))
    lines.append(
        f"{'':>3}  {'ИТОГО':<32} {cov.exhibits:>10} {cov.with_label_slug:>13} "
        f"{cov.coverage_rate * 100:>6.1f}%"
    )
    return "\n".join(lines)


def format_exhibits(rows: Sequence[dict], labels: Dict[Optional[int], str]) -> str:
    """Список карточек с пометкой, есть ли у них класс распознавания."""
    if not rows:
        return "  (ничего не найдено)"
    out = []
    for e in rows:
        slug = e["label_slug"] or "— НЕТ label_slug —"
        place = labels.get(e["hall_id"], f"зал id={e['hall_id']}")
        if e["showcase_number"] is not None:
            place += f", вит. {e['showcase_number']}"
        elif e["hall_id"] is not None:
            place += ", вне витрин"
        out.append(f"  id={e['id']:<5} {place:<34} {e['name'][:48]:<48} {slug}")
    return "\n".join(out)


def hall_labels(snapshot: dict) -> Dict[Optional[int], str]:
    """``hall_id → «зал 8 „Голубая гостиная“»``.

    Отдельная карта нужна потому, что id зала и его НОМЕР для музея — разные числа
    (на проде «Голубая гостиная» — это id 14, зал № 8). Печатать в отчёте id и называть
    его залом значило бы вводить музей в заблуждение.
    """
    out: Dict[Optional[int], str] = {None: NO_HALL_LABEL}
    for hall in snapshot.get("halls") or []:
        hid = hall.get("id")
        if hid is None:
            continue
        number = hall.get("hall_number")
        name = hall.get("name") or f"id {hid}"
        out[int(hid)] = f"зал {number} «{name}»" if number is not None else f"«{name}»"
    return out


def format_collisions(
    pairs: Sequence[Tuple[dict, dict]], labels: Dict[Optional[int], str], examples: int = 10
) -> str:
    """Сводка по залам плюс несколько примеров: куда именно уведёт снимок."""
    if not pairs:
        return "  (одноимённых пар нет)"

    def label(hall_id: Optional[int]) -> str:
        return labels.get(hall_id, f"зал id={hall_id}")

    per_hall: Dict[Optional[int], int] = {}
    other_hall = 0
    for victim, target in pairs:
        per_hall[victim["hall_id"]] = per_hall.get(victim["hall_id"], 0) + 1
        if victim["hall_id"] != target["hall_id"]:
            other_hall += 1
    lines = [
        f"  всего одноимённых пар: {len(pairs)}; из них уводят В ДРУГОЙ ЗАЛ: {other_hall}",
        "  по залам:",
    ]
    # Порядок строк — как в списке залов (там он музейный), а НЕ по hall_id: id и номер
    # зала на проде разошлись, и сортировка по id поставила бы зал 12 перед залом 8.
    order = {hid: i for i, hid in enumerate(labels)}
    for hall_id, count in sorted(
        per_hall.items(), key=lambda kv: (kv[0] is None, order.get(kv[0], len(order)))
    ):
        lines.append(f"    {label(hall_id):<34} {count}")
    lines.append("  примеры:")
    for victim, target in pairs[:examples]:
        lines.append(
            f"    id={victim['id']:<5} {label(victim['hall_id']):<28} «{victim['name'][:38]}»"
            f"\n          → id={target['id']:<5} {label(target['hall_id']):<28} {target['label_slug']}"
        )
    if len(pairs) > examples:
        lines.append(f"    … ещё {len(pairs) - examples}")
    return "\n".join(lines)


def as_json(cov: Coverage) -> dict:
    return {
        "source": cov.source,
        "generated_at": cov.generated_at,
        "exhibits": cov.exhibits,
        "with_label_slug": cov.with_label_slug,
        "missing": cov.missing,
        "coverage_rate": cov.coverage_rate,
        "halls": [
            {
                "hall_id": r.hall_id, "hall_number": r.hall_number, "hall_name": r.hall_name,
                "exhibits": r.exhibits, "with_label_slug": r.with_label_slug,
                "missing": r.missing, "coverage_rate": r.coverage_rate,
            }
            for r in cov.rows
        ],
        "index_owner_note": CEILING_NOTE.replace("\n", " "),
    }


# ── CLI ──────────────────────────────────────────────────────────────────────────────────────
def load_snapshot(args: argparse.Namespace) -> dict:
    if args.from_file:
        with open(args.from_file, encoding="utf-8") as fh:
            return json.load(fh)
    if args.from_seed:
        with open(args.from_seed, encoding="utf-8") as fh:
            return load_seed(fh.read())
    return fetch_snapshot()


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Покрытие каталога классами распознавания (label_slug). Только чтение.",
    )
    src = parser.add_mutually_exclusive_group()
    src.add_argument("--from-file", help="ранее сохранённый снимок (--save), без сети")
    src.add_argument("--from-seed", help="сид-файл db/seed_fabergemuseum.sql, без сети")
    parser.add_argument("--save", help="куда сохранить сырой снимок каталога (только для API)")
    parser.add_argument("--hall-id", type=int, help="показать карточки без label_slug в этом зале")
    parser.add_argument("--find", help="найти карточки по подстроке названия")
    parser.add_argument("--collisions", action="store_true",
                        help="бесслаговые карточки, чьё имя уже занято карточкой со слагом")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    args = parser.parse_args(argv)

    snapshot = load_snapshot(args)
    if args.save and not (args.from_file or args.from_seed):
        with open(args.save, "w", encoding="utf-8") as fh:
            json.dump(snapshot, fh, ensure_ascii=False, indent=1)

    cov = build_coverage(snapshot)
    if args.format == "json":
        print(json.dumps(as_json(cov), ensure_ascii=False, indent=1))
        return 0

    labels = hall_labels(snapshot)
    where = snapshot.get("source") or "?"
    when = snapshot.get("generated_at") or "—"
    print(f"Источник: {where}   снято: {when}")
    print()
    print(format_table(cov))
    print()
    print(CEILING_NOTE)

    if args.find:
        print()
        print(f"Карточки по подстроке {args.find!r}:")
        print(format_exhibits(find_exhibits(snapshot, args.find), labels))

    if args.collisions:
        print()
        print("Сшивка по названию: карточки без label_slug, чьё имя занято карточкой СО слагом")
        print(format_collisions(name_collisions(snapshot), labels))

    if args.hall_id is not None:
        rows = exhibits_without_slug(snapshot, args.hall_id)
        print()
        print(f"Без label_slug — {labels.get(args.hall_id, f'зал id={args.hall_id}')}: {len(rows)}")
        print(format_exhibits(rows, labels))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
