#!/usr/bin/env python3
"""Заливает описания залов из db/hall_descriptions.json через админ-API.

Баг-репорт заказчика 12.08.2026, п.3 (P2) «У Голубой гостиной пустое описание зала»:
у зала id=14 (`hall_number=8`) описание пустое, хотя в путеводителе вводный текст есть
(печатные стр. 98–99 — эмалевые произведения фирм И. Хлебникова, А. Кузмичёва, Ф. Рюкерта).

Откуда взялась пустота
----------------------
В сиде «Белая и Голубая гостиные» были ОДНИМ залом, и описание в файле лежало одной
склейкой: дословный текст Белой (печатные стр. 86–87) + «\\n\\n» + текст Голубой
(стр. 98–99). На проде гостиные разъехались на два зала, но текст не разрезали — вся
склейка (8160 знаков) осталась у Белой (id=7, №7), а Голубая (id=14, №8) получила NULL.
Теперь в файле два отдельных зала: ключ «7» — Белая гостиная (3868 знаков), ключ «14» —
Голубая (4076). Тексты — те же самые куски склейки, из них убраны только заголовки-
префиксы («Белая гостиная. » / «Голубая гостиная. ») и две врезки-подписи с полей
путеводителя, которые слой PDF вклеил прямо в поток («Изделия с эмалью придворной
фирмы П. Овчинникова…» и «Произведения с эмалью ведущих русских фирм…»; вторая
разрывала имя «Федора Ивановича ‹врезка› Рюкерта (1840–1917)»). Сверено посимвольно
с текстовым слоем путеводителя, ничего не дописано.

ИЗВЕСТНОЕ РАСХОЖДЕНИЕ, которое эта правка НЕ трогает: тот же артефакт вклеенной врезки
живёт в описаниях ещё восьми залов (Рыцарский — «Военно-мемориальные предметы» посреди
фразы, Выставочный — «Камнерезные изделия фирмы Фаберже…», и т. д.). Это отдельное
решение по всем залам сразу, а не побочный эффект починки Голубой: чистка добавила бы
в прогон восемь незапрошенных правок продового текста.

Почему сопоставление по ИМЕНИ, а не по номеру
---------------------------------------------
Прошлая версия скрипта строила `{hall_number: …}` с обеих сторон. После разделения
гостиных нумерация прода сдвинулась на +1 начиная с №8: в файле №8 — «Выставочный зал»,
а на проде №8 — Голубая гостиная (id=14), Выставочный уехал на №9. То есть прогон
по номеру залил бы описание Выставочного зала в Голубую гостиную и сдвинул бы ещё три
зала (Готический → Выставочный, Верхняя буфетная → Готический, Бежевый → Буфетную) —
молча, с кодом возврата 0. Поэтому сопоставляем по нормализованному имени
(`recognizer.normalize_name`: регистр, «ё/е», кавычки и лишние пробелы не важны — та же
нормализация, что в импорте путеводителя и в распознавании), а номер печатаем как
подсказку и ругаемся, если он разошёлся с продом.

По id тоже нельзя: ключи файла совпадают с id залов прода на 12.08.2026, но в других
окружениях id другие — прецедент db/migrations/2026-08-06_bugreport_iter2.sql, там
специально ищут по паре «номер + название», потому что UPDATE по id молча промахнулся бы.
Ключ в файле — только опора для чтения глазами; код его не использует.

Зал, имя которого на проде не нашлось, скрипт НЕ трогает и сообщает отдельной секцией
(и возвращает код 1) — вместо того чтобы подобрать «похожий по номеру».

Служебные залы
--------------
Список читаем с `include_service=true` — но уже НЕ потому, что «Парадная лестница»
служебная: с 31.08.2026 она обычный первый зал экспозиции (баг-репорт 31.08.2026, п. I-1,
разбор — docs/staircase-hall-decision.md). Флаг нужен по более общей причине: список залов
для сопоставления обязан быть ПОЛНЫМ независимо от того, помечен ли какой-то зал служебным
сегодня. Прошлая версия читала выдачу без флага, поэтому лестницы для неё не существовало
и её описание не залилось бы никогда; повторить эту историю с любой будущей служебной
записью мы не хотим.

Запуск
------
    BASE_URL=https://api.example.ru ADMIN_TOKEN=secret \\
        python scripts/apply_hall_descriptions.py                 # сухой прогон (по умолчанию)
    ... --only "Парадная лестница"                                # только один зал (см. ниже)
    ... --report-file hall_descriptions_20260812.csv              # список замен заказчику
    ... --apply                                                   # применить
    ... --rollback hall_descriptions_rollback_20260812-120000.json --apply

Ключ --only
-----------
Без ключа заливаются все залы файла — так и было задумано. Но на 31.08.2026 п.3 от
12.08 на проде так и не применён: у «Белой гостиной» осталась склейка (8160 симв.), у
«Голубой» описание пустое. Значит прогон ради одной «Парадной лестницы» (п. I-1) молча
потянул бы за собой две чужие правки. Они верные и давно согласованные, но утверждать их
музей должен отдельно — `--only «Парадная лестница»` даёт это сделать, ничего не ломая
существующим вызовам. Ключ повторяемый, сравнение имён — по той же нормализации
(`recognizer.normalize_name`), что и сопоставление с продом.

Сухой прогон — ПО УМОЛЧАНИЮ: прошлая версия писала на прод, если не передан `--dry-run`,
и одна забытая опция стоила бы четырёх перепутанных описаний. При `--apply` пишется файл
отката с ИСХОДНЫМИ описаниями (в том числе `null` у Голубой — откат вернёт именно NULL).

Идемпотентен: зал, у которого описание уже совпадает с файлом, в план не попадает,
поэтому повторный прогон печатает ноль замен. Зависимость у скрипта одна — нормализация
имён из app/services/recognizer.py, чтобы сшивка везде была одна.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.recognizer import normalize_name  # noqa: E402

BASE = os.environ.get("BASE_URL", "http://localhost:8000").rstrip("/")
TOKEN = os.environ.get("ADMIN_TOKEN", os.environ.get("ADMIN_API_TOKEN", "dev-admin-token"))
UA = "faberge-hall-descriptions/1.0"

HERE = os.path.dirname(os.path.abspath(__file__))
DESC_FILE = os.path.join(HERE, "..", "db", "hall_descriptions.json")

# Служебные записи попадают в список только с этим флагом (см. шапку «Служебные залы»):
# сопоставлять надо с ПОЛНЫМ каталогом, иначе зал молча остаётся без описания.
HALLS_PATH = "/halls?include_service=true"


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


# ── Чистое ядро: план заливки (без сети) ────────────────────────────────────────────────────
@dataclass
class Entry:
    """Строка файла описаний: ключ (id зала в проде — только для чтения глазами), номер, имя, текст."""

    key: str
    hall_number: Optional[int]
    name: str
    description: str


@dataclass
class Update:
    """Одна заливка: какому залу прода, что было и что станет."""

    hall_id: int
    hall_name: str                    # имя, как оно записано на проде (в отчёт — его, а не файла)
    live_number: Optional[int]        # номер зала на проде
    file_number: Optional[int]        # номер зала в файле; расходится — печатаем подсказкой
    before: Optional[str]
    after: str

    @property
    def number_moved(self) -> bool:
        return self.live_number != self.file_number


@dataclass
class Plan:
    updates: List[Update] = field(default_factory=list)
    unchanged: List[Update] = field(default_factory=list)       # описание уже совпадает — идемпотентность
    unmatched: List[Entry] = field(default_factory=list)        # имени нет на проде — НЕ трогаем
    ambiguous: List[Entry] = field(default_factory=list)        # имя на проде не одно — НЕ трогаем
    extra_live: List[dict] = field(default_factory=list)        # зал прода, которого нет в файле


def load_entries(path: str = DESC_FILE) -> List[Entry]:
    """Прочитать файл описаний. Порядок — как в файле (он же порядок обхода экспозиции)."""
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)
    entries = []
    for key, value in raw.items():
        name = (value.get("name") or "").strip()
        if not name:
            raise SystemExit(f"В файле описаний у ключа {key!r} нет названия зала — сопоставлять не по чему")
        entries.append(Entry(str(key), value.get("hall_number"), name, value.get("description") or ""))
    return entries


def filter_entries(entries: Sequence[Entry], only: Optional[Sequence[str]]) -> List[Entry]:
    """Оставить залы, названные в ``--only``. Пустой/отсутствующий ключ — оставить всё.

    Имя, которого в файле нет, — это не «ничего не залилось, ну и ладно», а опечатка в
    команде: тихо вернуть пустой план значит соврать про успех. Падаем сразу и с именами.
    """
    if not only:
        return list(entries)
    wanted = {normalize_name(name): name for name in only}
    kept = [e for e in entries if normalize_name(e.name) in wanted]
    missing = wanted.keys() - {normalize_name(e.name) for e in kept}
    if missing:
        names = ", ".join(f"«{wanted[key]}»" for key in sorted(missing))
        raise SystemExit(f"--only: в файле описаний нет залов с именами {names}")
    return kept


def index_by_name(halls: Sequence[dict]) -> Dict[str, List[dict]]:
    """Карта нормализованное имя → залы прода. Список, а не зал: тёзок надо увидеть, а не потерять."""
    index: Dict[str, List[dict]] = {}
    for hall in halls:
        index.setdefault(normalize_name(hall.get("name") or ""), []).append(hall)
    return index


def build_plan(entries: Sequence[Entry], halls: Sequence[dict]) -> Plan:
    """Сопоставить файл с живым каталогом по имени и решить, что кому заливать.

    Номер зала в решении не участвует вовсе (см. шапку про сдвиг нумерации на +1) —
    он только едет в отчёт, чтобы заказчику было видно, куда именно легло описание.
    """
    plan = Plan()
    index = index_by_name(halls)
    matched_ids = set()

    for entry in entries:
        found = index.get(normalize_name(entry.name), [])
        if not found:
            plan.unmatched.append(entry)
            continue
        if len(found) > 1:
            plan.ambiguous.append(entry)
            continue
        hall = found[0]
        matched_ids.add(hall["id"])
        update = Update(
            hall_id=hall["id"],
            hall_name=hall.get("name") or entry.name,
            live_number=hall.get("hall_number"),
            file_number=entry.hall_number,
            before=hall.get("description"),
            after=entry.description,
        )
        (plan.unchanged if update.before == update.after else plan.updates).append(update)

    plan.extra_live = [h for h in halls if h["id"] not in matched_ids]
    return plan


# ── Отчёт ───────────────────────────────────────────────────────────────────────────────────
def _short(text: Optional[str], width: int = 100) -> str:
    if text is None:
        return "NULL"
    single = " ".join(text.split())
    return single if len(single) <= width else single[: width - 1] + "…"


def _number(value: Optional[int]) -> str:
    return f"#{value}" if value is not None else "#—"


def print_report(plan: Plan, apply: bool) -> None:
    print("Применение" if apply else "План (сухой прогон)")
    print(f"Каталог: {BASE}")
    print(f"Залов на проде: {len(plan.updates) + len(plan.unchanged) + len(plan.extra_live)}, "
          f"описаний в файле: {len(plan.updates) + len(plan.unchanged) + len(plan.unmatched) + len(plan.ambiguous)}")

    print(f"\nК заливке: {len(plan.updates)} (уже совпадает: {len(plan.unchanged)})")
    for upd in plan.updates:
        print(f"  ~ id={upd.hall_id} {_number(upd.live_number)} {upd.hall_name}: "
              f"{len(upd.before) if upd.before else 0} → {len(upd.after)} симв.")
        print(f"      − {_short(upd.before)}")
        print(f"      + {_short(upd.after)}")
        if upd.number_moved:
            # Не ошибка сама по себе: номера прода после разделения гостиных сдвинуты на +1.
            # Но если расхождение вылезло там, где его не ждали, это первый признак, что файл отстал.
            print(f"      ⚠ номер в файле {_number(upd.file_number)}, на проде {_number(upd.live_number)} — "
                  "сопоставили по имени")

    # Секции ниже — то, что скрипт трогать отказался. Молчать про них нельзя:
    # именно так «не залилось описание» и превращается в баг-репорт через полгода.
    if plan.unmatched:
        print(f"\n❌ НЕ НАЙДЕНЫ на проде по имени — НЕ ТРОГАЛИ ({len(plan.unmatched)}):")
        for entry in plan.unmatched:
            print(f"  ! {_number(entry.hall_number)} «{entry.name}» (ключ {entry.key}) — "
                  "зал не создан, переименован или список залов пришёл урезанным")
    if plan.ambiguous:
        print(f"\n❌ ТЁЗКИ на проде — НЕ ТРОГАЛИ ({len(plan.ambiguous)}):")
        for entry in plan.ambiguous:
            print(f"  ! {_number(entry.hall_number)} «{entry.name}» (ключ {entry.key}) — "
                  "залов с таким именем больше одного, выбирать наугад нельзя")
    if plan.extra_live:
        print(f"\nЗалы прода без описания в файле ({len(plan.extra_live)}) — это нормально, "
              "просто к сведению:")
        for hall in plan.extra_live:
            has = "есть" if hall.get("description") else "пусто"
            print(f"  · id={hall['id']} {_number(hall.get('hall_number'))} {hall.get('name')} "
                  f"(описание на проде: {has})")

    if not plan.updates:
        print("\n  Заливать нечего: описания на проде уже совпадают с файлом.")
    elif not apply:
        print("\nЭто сухой прогон. Повторите с --apply.")


def write_report(plan: Plan, path: str) -> None:
    """Список замен файлом — заказчику «что именно поедет на прод».

    ``.csv`` — для Excel музея: разделитель «;» и BOM, иначе кириллица приезжает
    кракозябрами. Всё остальное — JSON с полными текстами (без обрезки).
    """
    rows = [
        {
            "status": "заливка",
            "hall_id": upd.hall_id,
            "hall_name": upd.hall_name,
            "hall_number_live": upd.live_number,
            "hall_number_file": upd.file_number,
            "before": upd.before,
            "after": upd.after,
        }
        for upd in plan.updates
    ]
    skipped = [
        {
            "status": status,
            "hall_key": entry.key,
            "hall_name": entry.name,
            "hall_number_file": entry.hall_number,
            "after": entry.description,
        }
        for status, group in (("не найден", plan.unmatched), ("тёзки", plan.ambiguous))
        for entry in group
    ]

    if path.lower().endswith(".csv"):
        with open(path, "w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.writer(fh, delimiter=";")
            writer.writerow(["статус", "id", "зал", "номер на проде", "номер в файле", "было", "стало"])
            for row in rows:
                writer.writerow([
                    row["status"], row["hall_id"], row["hall_name"],
                    row["hall_number_live"], row["hall_number_file"],
                    row["before"] if row["before"] is not None else "", row["after"],
                ])
            for row in skipped:
                writer.writerow([
                    row["status"], "", row["hall_name"], "", row["hall_number_file"], "", row["after"],
                ])
    else:
        doc = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "base_url": BASE,
            "summary": {
                "updates": len(plan.updates),
                "unchanged": len(plan.unchanged),
                "unmatched": len(plan.unmatched),
                "ambiguous": len(plan.ambiguous),
            },
            "updates": rows,
            "skipped": skipped,
        }
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False, indent=2)
    print(f"\nСписок замен: {os.path.abspath(path)}")


# ── Применение и откат ──────────────────────────────────────────────────────────────────────
def _default_rollback_path() -> str:
    return f"hall_descriptions_rollback_{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"


def apply_plan(plan: Plan, rollback_path: str) -> int:
    """Залить описания: по одному PATCH на зал, только поле description.

    Файл отката пишется в ``finally`` — даже если прогон свалился на середине, откатывать
    уже применённые правки чем-то надо. В ``before`` кладём исходное значение как есть,
    включая ``null`` (у Голубой гостиной описания не было вовсе), — тогда откат вернёт NULL,
    а не пустую строку.
    """
    log = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "base_url": BASE,
        "items": [],
    }
    errors = 0
    try:
        for upd in plan.updates:
            status, body = api("PATCH", f"/admin/halls/{upd.hall_id}", {"description": upd.after})
            if status != 200:
                print(f"  ОШИБКА id={upd.hall_id} {upd.hall_name}: {status} {body}")
                errors += 1
                continue
            log["items"].append({
                "hall_id": upd.hall_id,
                "hall_name": upd.hall_name,
                "hall_number": upd.live_number,
                "before": upd.before,
                "after": upd.after,
            })
            print(f"  ~ id={upd.hall_id} {_number(upd.live_number)} {upd.hall_name}: {len(upd.after)} симв.")
    finally:
        with open(rollback_path, "w", encoding="utf-8") as fh:
            json.dump(log, fh, ensure_ascii=False, indent=2)
        print(f"\nОбновлено залов: {len(log['items'])}")
        print(f"Файл отката: {os.path.abspath(rollback_path)}")
    return errors


def run_rollback(path: str, apply: bool) -> int:
    """Вернуть исходные описания по файлу отката.

    Зал, описание которого после прогона правили руками (текущее значение не то, что
    записал скрипт), не трогаем: чужую правку молча затирать нельзя. Если описание уже
    равно исходному — пропускаем без шума, поэтому откат можно повторять.
    """
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
        current = hall.get("description")
        if current == item.get("before"):
            continue                                   # уже как было — откат идемпотентен
        if current != item.get("after"):
            print(f"  ПРОПУСК id={hall_id} {item.get('hall_name')}: "
                  "описание правили после прогона — разбирайтесь руками")
            skipped += 1
            continue
        print(f"  ← id={hall_id} {item.get('hall_name')}: "
              f"{len(current) if current else 0} → {len(item['before']) if item.get('before') else 0} симв.")
        restored += 1
        if not apply:
            continue
        status, body = api("PATCH", f"/admin/halls/{hall_id}", {"description": item.get("before")})
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

    entries = filter_entries(load_entries(args.file), args.only)
    if args.only:
        # Печатаем явно: иначе «залито 1 из 12» через месяц читается как сбой скрипта,
        # а не как сознательное ограничение прогона.
        print("Ограничение --only: " + ", ".join(f"«{e.name}»" for e in entries) + "\n")
    plan = build_plan(entries, fetch_halls())
    print_report(plan, args.apply)
    if args.report_file:
        write_report(plan, args.report_file)

    # Ненайденный зал — это не «предупреждение», а невыполненная работа: описание никуда
    # не залилось. Возвращаем 1, чтобы прогон в скрипте/CI не сошёл за успешный.
    unresolved = len(plan.unmatched) + len(plan.ambiguous)
    if not args.apply or not plan.updates:
        return 1 if unresolved else 0
    print()
    return 1 if apply_plan(plan, args.rollback_file) or unresolved else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="применить изменения (без ключа — сухой прогон)")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="ничего не делает: сухой прогон и так по умолчанию (ключ оставлен, чтобы старый вызов "
             "не падал на неизвестной опции)",
    )
    parser.add_argument("--file", default=DESC_FILE, help="файл описаний (по умолчанию db/hall_descriptions.json)")
    parser.add_argument(
        "--only", action="append", metavar="NAME", default=None,
        help="залить описание только названного зала (ключ можно повторять); без ключа — все залы файла",
    )
    parser.add_argument(
        "--report-file", metavar="FILE",
        help="выгрузить список замен: .csv — для Excel музея, иначе JSON",
    )
    parser.add_argument(
        "--rollback-file", default=_default_rollback_path(),
        help="куда писать файл отката при --apply (по умолчанию — с датой в имени, в текущем каталоге)",
    )
    parser.add_argument("--rollback", metavar="FILE", help="вернуть исходные описания по файлу отката")
    args = parser.parse_args()
    if args.dry_run and args.apply:
        parser.error("--dry-run и --apply вместе не имеют смысла: выберите одно")
    if args.rollback and (args.report_file or args.only or args.file != DESC_FILE):
        parser.error("--rollback несовместим с ключами разбора файла описаний")
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
