#!/usr/bin/env python3
"""Ревизия витрин: записи без номера, повисшие в пронумерованных витринах.

Баг-репорт заказчика 06.08.2026, п.1 (P0) «42 записи без номера висят в витринах — это и есть
лишние экспонаты» и п.4 (P1) «Верхняя буфетная: витрина №1, которой нет в путеводителе».

Откуда взялись эти записи: scripts/import_guide_showcases.py переносит и нумерует только те
экспонаты, которые нашлись в выписке из путеводителя, а несопоставленные остаются там, где лежали
со старого импорта — в первой витрине зала. На проде 06.08.2026 это ровно 42 записи с пустым
``exhibit_number`` в витринах С номером (плюс 4 в служебном зале «Вне постоянной экспозиции»,
их заказчик просил не трогать).

Что делает скрипт:
  • собирает каталог через API (залы → витрины → экспонаты), как и импорт путеводителя;
  • ищет «сирот» — экспонат с пустым ``exhibit_number`` в витрине с ``showcase_number``;
  • делит их на две кучи: «вероятный дубль» (нормализованное название похоже на уже
    пронумерованный экспонат ТОГО ЖЕ зала) и «вне путеводителя» (всё остальное);
  • ОБЕ кучи переносит в группу «Не в витринах» своего зала (создаёт её, если в зале такой нет).
    Из витрин записи уходят, из каталога — нет.

Почему перенос, а не удаление — даже для «вероятных дублей».
Заказчик прямо просил принимать решение об удалении ВМЕСТЕ С МУЗЕЕМ, а нечёткое сходство названий
даёт ложные срабатывания. Живой пример с прода: id=65 «Икона "Богоматерь ТИХВИНСКАЯ..."» похожа на
id=1150 №5 «Икона. Богоматерь ИВЕРСКАЯ...» на 0.94 — а это разные иконы, и автоудаление по сходству
стёрло бы карточку с фото и описанием. Поэтому «вероятный дубль» — это только пометка в отчёте,
который заказчик несёт в музей; удаление выполняется отдельным прогоном по явному списку
подтверждённых музеем id (``--delete-ids``) и блокируется, если на карточке висят фото, описание,
озвучка, каталожные поля ИЛИ label_slug — класс распознавания («сначала слейте данные»).
На проде 06.08.2026 label_slug есть у всех 46 сирот, так что в текущем каталоге удаление
блокируется для каждой из них — и это правильный ответ: сперва слияние, потом удаление.

Перенос обратим: при ``--apply`` пишется файл отката, обратный прогон — ``--rollback <file>``.

    BASE_URL=http://localhost:8000 ADMIN_TOKEN=dev-admin-token \\
        python scripts/fix_showcase_orphans.py                       # сухой прогон + отчёт
    ... --apply                                                      # перенести сирот (п.1)
    ... --apply --drop-empty-showcases                               # + удалить опустевшие витрины (п.4)
    ... --apply --delete-ids 122,157,161                             # удалить подтверждённые музеем
    ... --rollback showcase_orphans_rollback_20260806-120000.json --apply

Идемпотентен: после переноса сироты лежат в группе без номера и в план больше не попадают,
опустевшие витрины уже удалены — повторный прогон печатает пустой план.

Требует зависимостей проекта: нормализация названий берётся из app/services/recognizer.py — та же,
что в импорте путеводителя и в распознавании, чтобы сшивка везде была одна.
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.recognizer import normalize_name  # noqa: E402

BASE = os.environ.get("BASE_URL", "http://localhost:8000").rstrip("/")
TOKEN = os.environ.get("ADMIN_TOKEN", os.environ.get("ADMIN_API_TOKEN", "dev-admin-token"))
UA = "faberge-showcase-orphans/1.0"

DEFAULT_SIMILARITY = 0.80
UNNUMBERED_NAME = "Не в витринах"      # так группа названа в проде и в cleanup_hall_catalog.py
# Зал «Вне постоянной экспозиции» не помечен is_service, но заказчик просил его не трогать
# (там лежат 4 такие же записи без номера — это витрина склада, а не экспозиция).
OUTSIDE_EXPO_KEY = normalize_name("Вне постоянной экспозиции")

DUPLICATE = "duplicate"                # вероятный дубль уже сшитого экспоната
OFF_GUIDE = "off_guide"                # предмета нет в путеводителе 2014 года
MOVE = "move"
DELETE = "delete"

# Поля карточки, по которым видно, что на сироте висят данные: их нельзя потерять ни при слиянии,
# ни при удалении. Метки печатаются в отчёте (музею — что именно сливать) и блокируют --delete-ids.
#
# ПОЧЕМУ label_slug здесь. Это класс распознавания: recognition.py берёт из карточек whitelist
# (crud.all_label_slugs), и recognizer отдаёт посетителю только те slug'и, что в нём есть. Удалив
# карточку со slug'ом, мы выключаем распознавание этого предмета навсегда — а на проде 06.08.2026
# label_slug есть у ВСЕХ 46 сирот (при 232 на весь каталог из 1253), причём у 14 из них нет ни
# фото, ни описания, то есть без этой строки они проходили как «чистые» и удалялись молча. В файле
# отката slug тоже не лежал, восстановить было нечем. Теперь любая сирота требует явного слияния.
MEDIA_FIELDS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("фото", ("image_url", "thumbnail_url", "images")),
    ("описание", ("short_description", "raw_history")),
    ("озвучка", ("audio_url", "short_description_spoken")),
    ("видео", ("video_url",)),
    ("3D", ("model_3d_url", "model_3d_embed")),
    ("распознавание", ("label_slug",)),
    # techniques — такие же каталожные данные, как material и year_created (12.08.2026, п.5;
    # с 17.08.2026 year_created — датировка строкой, бывший дубль dating выпилен). Без них
    # карточка, у которой разбор импорта заполнил ТОЛЬКО такие поля, считалась пустой
    # и удалялась по --delete-ids молча (например, material пуст — в строке одни техники).
    # Оговорка: techniques отдаётся только админской карточкой (в ExhibitSummary его нет),
    # так что без ADMIN_TOKEN эта метка не посчитается — ровно тот случай, ради которого
    # load_orphan_details возвращает признак «прочитано не всё» и отменяет удаление.
    ("каталожные поля", ("material", "master_name", "year_created", "techniques", "source_url")),
)

# Что кладём в файл отката по удаляемой карточке. Раньше писались только id/название/витрина —
# по такой записи не восстановить ни класс распознавания, ни описание, а «удалённое откатом не
# восстанавливается» превращалось в «данные потеряны совсем». Теперь снимок несёт всё, из чего
# карточку можно завести заново руками (POST /admin/exhibits принимает ровно эти поля).
# Список обязан идти нога в ногу с MEDIA_FIELDS выше: поле, которое блокирует удаление, но не
# попало в снимок, — это ровно те данные, ради сохранности которых удаление и блокировалось.
# Поэтому techniques здесь тоже (12.08.2026, п.5).
SNAPSHOT_FIELDS: Tuple[str, ...] = (
    "id", "name", "exhibit_number", "label_slug", "short_description", "short_description_spoken",
    "raw_history", "image_url", "thumbnail_url", "images", "audio_url", "video_url",
    "model_3d_url", "model_3d_embed", "material", "master_name", "year_created",
    "techniques", "source_url",
)


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


# ── Чистое ядро: классификация и планирование (тестируется без сети — tests/test_showcase_orphans.py)
@dataclass
class DuplicateMatch:
    """Уже пронумерованный экспонат того же зала, на который похожа сирота."""

    exhibit_id: int
    name: str
    exhibit_number: Optional[str]
    similarity: float
    # Метки данных ОРИГИНАЛА. Печатаются рядом с метками сироты, потому что в 10 парах из 11
    # на проде фото и label_slug висят на СИРОТЕ, а пронумерованная карточка из путеводителя
    # пустая. Без этой строки отчёт подталкивал музей «удалить дубль» — то есть выбросить
    # богатую карточку и оставить заглушку. Сливать надо в обратную сторону.
    marks: Tuple[str, ...] = ()


@dataclass
class OrphanAction:
    """Что делаем с одной сиротой. По умолчанию — MOVE, DELETE только по явному списку музея."""

    exhibit_id: int
    exhibit_name: str
    hall_id: int
    hall_name: str
    from_showcase_id: int
    from_showcase_number: Optional[int]
    to_showcase_id: Optional[int]      # None — группу «Не в витринах» ещё предстоит создать
    kind: str                          # DUPLICATE | OFF_GUIDE
    action: str = MOVE                 # MOVE | DELETE
    marks: Tuple[str, ...] = ()        # «фото», «описание», «озвучка» — что нельзя потерять
    match: Optional[DuplicateMatch] = None
    delete_blocked: bool = False       # id был в --delete-ids, но на карточке есть данные
    snapshot: Dict[str, object] = field(default_factory=dict)   # содержимое карточки для отката


@dataclass
class ShowcaseRef:
    showcase_id: int
    showcase_number: Optional[int]
    name: Optional[str]
    hall_id: int
    hall_name: str
    exhibit_count: int = 0


@dataclass
class HallStat:
    """Строка итоговой таблицы: сколько экспонатов лежит в пронумерованных витринах зала."""

    hall_id: int
    hall_name: str
    before: int
    moved: int
    deleted: int
    emptied: int

    @property
    def after(self) -> int:
        return self.before - self.moved - self.deleted


@dataclass
class Plan:
    actions: List[OrphanAction] = field(default_factory=list)
    halls: List[HallStat] = field(default_factory=list)
    empty_showcases: List[ShowcaseRef] = field(default_factory=list)
    groups_to_create: List[int] = field(default_factory=list)   # id залов без группы «Не в витринах»
    skipped_halls: List[Tuple[int, str, int]] = field(default_factory=list)  # (id, название, сирот)
    unknown_delete_ids: List[int] = field(default_factory=list)

    @property
    def moves(self) -> List[OrphanAction]:
        return [a for a in self.actions if a.action == MOVE]

    @property
    def deletions(self) -> List[OrphanAction]:
        return [a for a in self.actions if a.action == DELETE]

    @property
    def duplicates(self) -> List[OrphanAction]:
        return [a for a in self.actions if a.kind == DUPLICATE]

    @property
    def off_guide(self) -> List[OrphanAction]:
        return [a for a in self.actions if a.kind == OFF_GUIDE]

    @property
    def blocked(self) -> List[OrphanAction]:
        return [a for a in self.actions if a.delete_blocked]


def is_orphan(exhibit: dict, showcase: dict) -> bool:
    """Сирота — запись без номера по путеводителю в витрине С номером (п.1 баг-репорта).

    Запись без номера в группе «Не в витринах» (``showcase_number IS NULL``) сиротой не считается:
    она уже там, где надо. На этом и держится идемпотентность повторного прогона.
    """
    if showcase.get("showcase_number") is None:
        return False
    return not str(exhibit.get("exhibit_number") or "").strip()


def media_marks(exhibit: dict) -> Tuple[str, ...]:
    """Какие данные висят на карточке: их нельзя потерять при слиянии/удалении."""
    marks = []
    for label, fields in MEDIA_FIELDS:
        if any(exhibit.get(f) for f in fields):
            marks.append(label)
    return tuple(marks)


def hall_is_skipped(hall: dict, include_service: bool) -> bool:
    """Служебные залы и «Вне постоянной экспозиции» по умолчанию не трогаем (просьба заказчика)."""
    if include_service:
        return False
    return bool(hall.get("is_service")) or normalize_name(hall.get("name") or "") == OUTSIDE_EXPO_KEY


def best_duplicate(
    orphan_key: str, numbered: Sequence[Tuple[dict, str]], threshold: float
) -> Optional[DuplicateMatch]:
    """Ближайший по названию пронумерованный экспонат того же зала, если сходство ≥ порога.

    difflib, а не точное сравнение: точных дублей по нормализованному имени в проде нет ни одного —
    расхождения всегда в мелочах («по картинам Ф. Ваувермана» против «по живописному оригиналу
    Ф. Ваувермана»). autojunk выключен: на длинных названиях эвристика «частых символов» смещает
    оценку и делает результат зависимым от длины строки.
    """
    if not orphan_key or threshold > 1:
        return None
    matcher = difflib.SequenceMatcher(None, autojunk=False)
    matcher.set_seq2(orphan_key)
    best: Optional[DuplicateMatch] = None
    for candidate, key in numbered:
        if not key:
            continue
        matcher.set_seq1(key)
        # Дешёвые верхние оценки: отсекают заведомо непохожие названия без полного сравнения.
        if matcher.real_quick_ratio() < threshold or matcher.quick_ratio() < threshold:
            continue
        ratio = matcher.ratio()
        if ratio < threshold or (best is not None and ratio <= best.similarity):
            continue
        best = DuplicateMatch(
            exhibit_id=candidate["id"],
            name=candidate.get("name") or "",
            exhibit_number=candidate.get("exhibit_number"),
            similarity=round(ratio, 3),
            marks=media_marks(candidate),
        )
    return best


def classify_orphans(
    halls_data: Iterable[dict],
    similarity: float = DEFAULT_SIMILARITY,
    include_service: bool = False,
    delete_ids: Iterable[int] = (),
) -> Plan:
    """Построить план по снимку каталога. Сети не касается — весь ввод приходит аргументом.

    ``halls_data`` — список ``{"hall": {...}, "showcases": [{"showcase": {...}, "exhibits": [...]}]}``
    (ровно то, что собирает fetch_catalog). Ничего не меняет: возвращает план, который печатает
    отчёт и исполняет apply_plan.
    """
    wanted_deletes: Set[int] = {int(i) for i in delete_ids}
    seen_deletes: Set[int] = set()
    plan = Plan()

    for entry in halls_data:
        hall = entry["hall"]
        groups = entry.get("showcases") or []
        orphans_here = [
            ex for g in groups for ex in (g.get("exhibits") or []) if is_orphan(ex, g["showcase"])
        ]
        if hall_is_skipped(hall, include_service):
            plan.skipped_halls.append((hall["id"], hall.get("name") or "", len(orphans_here)))
            continue

        # Целевая группа «Не в витринах» зала; в зале она одна (частичный уникальный индекс).
        target = next(
            (g["showcase"]["id"] for g in groups if g["showcase"].get("showcase_number") is None), None
        )
        # Кандидаты на «дубля» — пронумерованные экспонаты ТОГО ЖЕ зала, включая те, что уже
        # лежат в группе «Не в витринах»: номер по путеводителю у них есть, значит они сшиты.
        numbered = [
            (ex, normalize_name(ex.get("name") or ""))
            for g in groups
            for ex in (g.get("exhibits") or [])
            if str(ex.get("exhibit_number") or "").strip()
        ]

        before = sum(
            len(g.get("exhibits") or []) for g in groups if g["showcase"].get("showcase_number") is not None
        )
        leaving: Dict[int, int] = {}
        moved = deleted = 0

        for group in groups:
            showcase = group["showcase"]
            for exhibit in group.get("exhibits") or []:
                if not is_orphan(exhibit, showcase):
                    continue
                marks = media_marks(exhibit)
                match = best_duplicate(normalize_name(exhibit.get("name") or ""), numbered, similarity)
                action, blocked = MOVE, False
                if exhibit["id"] in wanted_deletes:
                    seen_deletes.add(exhibit["id"])
                    # Музей подтвердил удаление, но на карточке есть данные — сначала слияние.
                    blocked = bool(marks)
                    action = MOVE if blocked else DELETE
                plan.actions.append(
                    OrphanAction(
                        exhibit_id=exhibit["id"],
                        exhibit_name=exhibit.get("name") or "",
                        hall_id=hall["id"],
                        hall_name=hall.get("name") or "",
                        from_showcase_id=showcase["id"],
                        from_showcase_number=showcase.get("showcase_number"),
                        to_showcase_id=target,
                        kind=DUPLICATE if match else OFF_GUIDE,
                        action=action,
                        marks=marks,
                        match=match,
                        delete_blocked=blocked,
                        snapshot={
                            f: exhibit[f] for f in SNAPSHOT_FIELDS if exhibit.get(f) is not None
                        },
                    )
                )
                leaving[showcase["id"]] = leaving.get(showcase["id"], 0) + 1
                if action == DELETE:
                    deleted += 1
                else:
                    moved += 1

        # Витрина пустеет, только если из неё уезжают ВСЕ её экспонаты. Витрины, которые были пусты
        # и до нас, не трогаем: пустая пронумерованная витрина может быть заведена музеем заранее.
        emptied = [
            ShowcaseRef(
                showcase_id=g["showcase"]["id"],
                showcase_number=g["showcase"].get("showcase_number"),
                name=g["showcase"].get("name"),
                hall_id=hall["id"],
                hall_name=hall.get("name") or "",
                exhibit_count=len(g.get("exhibits") or []),
            )
            for g in groups
            if g["showcase"].get("showcase_number") is not None
            and g.get("exhibits")
            and leaving.get(g["showcase"]["id"], 0) == len(g["exhibits"])
        ]
        plan.empty_showcases += emptied
        if target is None and moved:
            plan.groups_to_create.append(hall["id"])
        if moved or deleted:
            plan.halls.append(
                HallStat(hall["id"], hall.get("name") or "", before, moved, deleted, len(emptied))
            )

    # id из --delete-ids, которых нет среди сирот: опечатка, чужой зал или уже удалённая запись.
    # Молча удалять такое нельзя — можно снести живой пронумерованный экспонат.
    plan.unknown_delete_ids = sorted(wanted_deletes - seen_deletes)
    return plan


# ── Сбор каталога и применение плана ────────────────────────────────────────────────────────
def fetch_catalog() -> List[dict]:
    """Снимок каталога: зал → витрины → экспонаты. Служебные залы тянем всегда, фильтрует классификатор."""
    data: List[dict] = []
    for hall in get_all("/halls?include_service=true"):
        groups = []
        for showcase in get_all(f"/halls/{hall['id']}/showcases"):
            groups.append(
                {"showcase": showcase, "exhibits": get_all(f"/showcases/{showcase['id']}/exhibits")}
            )
        data.append({"hall": hall, "showcases": groups})
    return data


def load_orphan_details(halls_data: Iterable[dict]) -> bool:
    """Дочитать карточки сирот: в списочной выдаче нет описаний, озвучки и галереи.

    Без этого метки «фото/описание/озвучка» в отчёте были бы неполными, а защита --delete-ids —
    дырявой: экспонат с описанием, но без миниатюры, прошёл бы как «данных нет». Поэтому
    возвращаем признак «все карточки прочитаны»: на нём run() отменяет удаление.
    (Метка «распознавание» — единственная, которая считается и без админ-доступа: label_slug
    отдаётся прямо в списочной выдаче. Но полагаться на это как на всю защиту нельзя.)

    Ручка админская, и сухой прогон часто делают без токена (у нас самих его нет для прода). Тогда
    первый же ответ 401/403 повторится на всех 42 сиротах — печатаем одну строку и выходим, а не
    сорок две одинаковых.
    """
    complete = True
    for entry in halls_data:
        for group in entry.get("showcases") or []:
            for exhibit in group.get("exhibits") or []:
                if not is_orphan(exhibit, group["showcase"]):
                    continue
                status, body = api("GET", f"/admin/exhibits/{exhibit['id']}")
                if status == 200 and isinstance(body, dict):
                    exhibit.update(body)
                    continue
                complete = False
                if status in (401, 403):
                    print(
                        f"  ! админ-доступ к карточкам не принят ({status}): метки «описание/"
                        "озвучка/3D» считаются по списочной выдаче и могут быть неполными. "
                        "Задайте ADMIN_TOKEN.",
                        file=sys.stderr,
                    )
                    return False
                print(
                    f"  ! карточку экспоната id={exhibit['id']} прочитать не удалось "
                    f"({status}) — метки данных могут быть неполными",
                    file=sys.stderr,
                )
    return complete


def _default_rollback_path() -> str:
    return f"showcase_orphans_rollback_{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"


def apply_plan(plan: Plan, drop_empty: bool, rollback_path: str) -> int:
    """Применить план. Пишет файл отката даже при частичной неудаче — иначе нечем откатываться."""
    log: Dict[str, list] = {"moved": [], "created_showcases": [], "deleted_showcases": [],
                            "deleted_exhibits": []}
    errors = 0
    targets: Dict[int, int] = {}
    try:
        # 1. Группы «Не в витринах» там, где их нет (в проде это Белая гостиная).
        for hall_id in plan.groups_to_create:
            status, body = api(
                "POST", "/admin/showcases",
                {"hall_id": hall_id, "showcase_number": None, "name": UNNUMBERED_NAME},
            )
            if status != 201 or not isinstance(body, dict):
                print(f"  ОШИБКА создания группы «{UNNUMBERED_NAME}» в зале id={hall_id}: {status} {body}")
                errors += 1
                continue
            targets[hall_id] = body["id"]
            log["created_showcases"].append(
                {"id": body["id"], "hall_id": hall_id, "showcase_number": None, "name": UNNUMBERED_NAME}
            )
            print(f"  + группа «{UNNUMBERED_NAME}» в зале id={hall_id} → витрина id={body['id']}")

        # 2. Удаления по подтверждённому музеем списку (только карточки без данных).
        for act in plan.deletions:
            status, body = api("DELETE", f"/admin/exhibits/{act.exhibit_id}")
            if status not in (204, 404):
                print(f"  ОШИБКА удаления экспоната id={act.exhibit_id}: {status} {body}")
                errors += 1
                continue
            log["deleted_exhibits"].append(
                {"exhibit_id": act.exhibit_id, "name": act.exhibit_name,
                 "showcase_id": act.from_showcase_id, "card": act.snapshot}
            )
            print(f"  - удалён экспонат id={act.exhibit_id} «{act.exhibit_name}»")

        # 3. Переносы в группу «Не в витринах» своего зала.
        for act in plan.moves:
            target = act.to_showcase_id or targets.get(act.hall_id)
            if target is None:
                print(f"  ПРОПУСК id={act.exhibit_id}: группа «{UNNUMBERED_NAME}» в зале не создана")
                errors += 1
                continue
            status, body = api("PATCH", f"/admin/exhibits/{act.exhibit_id}", {"showcase_id": target})
            if status != 200:
                print(f"  ОШИБКА переноса id={act.exhibit_id}: {status} {body}")
                errors += 1
                continue
            log["moved"].append(
                {"exhibit_id": act.exhibit_id, "from_showcase_id": act.from_showcase_id,
                 "to_showcase_id": target}
            )

        print(f"  перенесено экспонатов: {len(log['moved'])}")

        # 4. Опустевшие витрины (п.4 — Верхняя буфетная). Удаляем БЕЗ force: force=true уносит
        #    экспонаты каскадом, а нам нужно ровно обратное — витрина уходит, содержимое остаётся.
        #    Поэтому же перед удалением перечитываем витрину: если в неё что-то положили парал-
        #    лельно, API ответит 409 и мы просто не тронем её.
        if drop_empty:
            for ref in plan.empty_showcases:
                status, body = api("GET", f"/showcases/{ref.showcase_id}/exhibits?limit=1")
                if status != 200 or not isinstance(body, dict):
                    print(f"  ОШИБКА проверки витрины id={ref.showcase_id}: {status} {body}")
                    errors += 1
                    continue
                if body.get("total", 0) != 0:
                    print(f"  ПРОПУСК витрины id={ref.showcase_id}: в ней {body['total']} экспонат(ов)")
                    errors += 1
                    continue
                status, body = api("DELETE", f"/admin/showcases/{ref.showcase_id}")
                if status not in (204, 404):
                    print(f"  ОШИБКА удаления витрины id={ref.showcase_id}: {status} {body}")
                    errors += 1
                    continue
                log["deleted_showcases"].append(
                    {"id": ref.showcase_id, "hall_id": ref.hall_id,
                     "showcase_number": ref.showcase_number, "name": ref.name}
                )
                print(
                    f"  - удалена пустая витрина №{ref.showcase_number} (id={ref.showcase_id}) "
                    f"в зале «{ref.hall_name}»"
                )
    finally:
        with open(rollback_path, "w", encoding="utf-8") as fh:
            json.dump(log, fh, ensure_ascii=False, indent=2)
        print(f"\nФайл отката: {os.path.abspath(rollback_path)}")
        if log["deleted_exhibits"]:
            print("  ВНИМАНИЕ: удалённые экспонаты откат обратно не заводит, но их карточки целиком "
                  "(включая label_slug) сохранены в файле — завести заново можно по нему.")
    return errors


def run_rollback(path: str, apply: bool) -> int:
    """Вернуть экспонаты в исходные витрины по файлу отката.

    Удалённые витрины пересоздаются по паре (зал, номер) — id при этом новый, поэтому исходные
    showcase_id из файла переотображаются на свежие. Экспонат, который после прогона успели
    переложить руками, не трогаем: возвращаем только тех, кто лежит там, куда его положил скрипт.
    """
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    remap: Dict[int, int] = {}
    errors = 0

    for sc in doc.get("deleted_showcases", []):
        existing = next(
            (s for s in get_all(f"/halls/{sc['hall_id']}/showcases")
             if s.get("showcase_number") == sc.get("showcase_number")), None
        )
        if existing is not None:
            remap[sc["id"]] = existing["id"]
            print(f"  = витрина №{sc['showcase_number']} в зале id={sc['hall_id']} уже есть "
                  f"(id={existing['id']})")
            continue
        print(f"  + восстановить витрину №{sc['showcase_number']} в зале id={sc['hall_id']}")
        if not apply:
            continue
        status, body = api(
            "POST", "/admin/showcases",
            {"hall_id": sc["hall_id"], "showcase_number": sc.get("showcase_number"),
             "name": sc.get("name")},
        )
        if status != 201 or not isinstance(body, dict):
            print(f"    ОШИБКА: {status} {body}")
            errors += 1
            continue
        remap[sc["id"]] = body["id"]

    returned = 0
    for mv in reversed(doc.get("moved", [])):
        dest = remap.get(mv["from_showcase_id"], mv["from_showcase_id"])
        status, body = api("GET", f"/exhibits/{mv['exhibit_id']}")
        if status != 200 or not isinstance(body, dict):
            print(f"  ПРОПУСК id={mv['exhibit_id']}: карточка недоступна ({status})")
            errors += 1
            continue
        current = (body.get("showcase") or {}).get("id")
        if current == dest:
            continue                                   # уже на месте — откат идемпотентен
        if current != mv.get("to_showcase_id"):
            print(f"  ПРОПУСК id={mv['exhibit_id']}: лежит в витрине id={current}, "
                  f"а скрипт клал в id={mv.get('to_showcase_id')} — разбирайтесь руками")
            errors += 1
            continue
        returned += 1
        if not apply:
            continue
        status, body = api("PATCH", f"/admin/exhibits/{mv['exhibit_id']}", {"showcase_id": dest})
        if status != 200:
            print(f"    ОШИБКА возврата id={mv['exhibit_id']}: {status} {body}")
            errors += 1

    for sc in doc.get("created_showcases", []):
        status, body = api("GET", f"/showcases/{sc['id']}/exhibits?limit=1")
        if status == 404:
            continue
        if status != 200 or not isinstance(body, dict):
            print(f"  ПРОПУСК витрины id={sc['id']}: {status} {body}")
            errors += 1
            continue
        if body.get("total", 0) != 0:
            print(f"  ПРОПУСК витрины id={sc['id']}: в ней {body['total']} экспонат(ов), не удаляю")
            continue
        print(f"  - убрать созданную группу «{sc.get('name')}» (id={sc['id']})")
        if apply:
            status, body = api("DELETE", f"/admin/showcases/{sc['id']}")
            if status not in (204, 404):
                print(f"    ОШИБКА: {status} {body}")
                errors += 1

    print(f"\nВозвращено в исходные витрины: {returned}")
    if doc.get("deleted_exhibits"):
        ids = ", ".join(str(d["exhibit_id"]) for d in doc["deleted_exhibits"])
        print(f"Удалённые экспонаты откатом не восстановить, заводите заново: {ids}")
    if not apply:
        print("\nЭто сухой прогон отката. Повторите с --apply.")
    return errors


# ── Отчёт ───────────────────────────────────────────────────────────────────────────────────
def _short(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


def _describe(act: OrphanAction) -> str:
    marks = f" [{', '.join(act.marks)}]" if act.marks else ""
    place = f"витрина {act.from_showcase_number}" if act.from_showcase_number is not None else "витрина ?"
    return f"{act.hall_name} · {place} · id={act.exhibit_id} «{_short(act.exhibit_name, 70)}»{marks}"


def print_report(plan: Plan, similarity: float, apply: bool, drop_empty: bool) -> None:
    print("Применение" if apply else "План (сухой прогон)")
    print(f"Порог сходства названий: {similarity}\n")

    if not plan.actions:
        print("Сирот в пронумерованных витринах не найдено — каталог уже разобран.")
    else:
        name_w = max(len(f"{h.hall_name} (id={h.hall_id})") for h in plan.halls) if plan.halls else 20
        name_w = min(max(name_w, 12), 46)
        print(f"{'Зал':<{name_w}}  {'было':>5} {'стало':>6} {'перенос':>8} {'удалить':>8} {'витрин -':>9}")
        print("─" * (name_w + 40))
        for h in plan.halls:
            title = _short(f"{h.hall_name} (id={h.hall_id})", name_w)
            print(f"{title:<{name_w}}  {h.before:>5} {h.after:>6} {h.moved:>8} {h.deleted:>8} {h.emptied:>9}")
        print("─" * (name_w + 40))
        totals = (
            sum(h.before for h in plan.halls), sum(h.after for h in plan.halls),
            sum(h.moved for h in plan.halls), sum(h.deleted for h in plan.halls),
            sum(h.emptied for h in plan.halls),
        )
        print(f"{'ИТОГО':<{name_w}}  {totals[0]:>5} {totals[1]:>6} {totals[2]:>8} {totals[3]:>8} "
              f"{totals[4]:>9}")

    # Этот список заказчик несёт в музей: удалять или сливать — решает музей, не скрипт.
    dups = plan.duplicates
    print(f"\nВероятные дубли — НА РЕШЕНИЕ МУЗЕЯ ({len(dups)}):")
    if not dups:
        print("  —")
    for act in dups:
        m = act.match
        # Метки ОБЕИХ карточек: чаще всего данные лежат на сироте, а пронумерованный «оригинал»
        # из путеводителя пустой — тогда сливать надо сироту В НОМЕР, а не наоборот.
        theirs = f" [{', '.join(m.marks)}]" if m.marks else " [данных нет]"
        print(f"  • {_describe(act)}")
        print(f"      ≈{m.similarity} ←→ id={m.exhibit_id} №{m.exhibit_number or '?'} "
              f"«{_short(m.name, 70)}»{theirs}")
        if act.marks and not m.marks:
            print("      → данные на СИРОТЕ, у номера их нет: переносить содержимое в номер, "
                  "а не удалять сироту вслепую")
    if dups:
        print("  Сходство названий — подсказка, а не приговор: «Богоматерь Тихвинская» и «Богоматерь")
        print("  Иверская» похожи на 0.94, но это разные иконы. Удаление — только по списку от музея.")

    off = plan.off_guide
    print(f"\nВне путеводителя 2014 ({len(off)}) — переносятся в «{UNNUMBERED_NAME}» своего зала:")
    if not off:
        print("  —")
    for act in off:
        print(f"  • {_describe(act)}")

    if plan.groups_to_create:
        ids = ", ".join(f"id={h}" for h in plan.groups_to_create)
        print(f"\nСоздать группу «{UNNUMBERED_NAME}» в залах: {ids}")

    print(f"\nВитрины, которые опустеют ({len(plan.empty_showcases)}):")
    if not plan.empty_showcases:
        print("  —")
    for ref in plan.empty_showcases:
        tail = "будет удалена" if drop_empty else "останется пустой (удалить: --drop-empty-showcases)"
        print(f"  • {ref.hall_name}: витрина №{ref.showcase_number} (id={ref.showcase_id}, "
              f"экспонатов {ref.exhibit_count}) — {tail}")

    if plan.blocked:
        print("\nУдаление ЗАБЛОКИРОВАНО — сначала слейте данные в оставляемую карточку:")
        for act in plan.blocked:
            print(f"  ! id={act.exhibit_id} «{_short(act.exhibit_name, 60)}»: {', '.join(act.marks)}")
        print("  Эти записи перенесены в «Не в витринах», а не удалены.")
    if plan.deletions:
        print(f"\nУдаляются по списку музея ({len(plan.deletions)}):")
        for act in plan.deletions:
            print(f"  - id={act.exhibit_id} «{_short(act.exhibit_name, 70)}»")
    if plan.unknown_delete_ids:
        ids = ", ".join(str(i) for i in plan.unknown_delete_ids)
        print(f"\n! id из --delete-ids, которых нет среди сирот (не трогаю): {ids}")

    if plan.skipped_halls:
        print("\nПропущены служебные залы (ключ --include-service включает их в разбор):")
        for hall_id, name, orphans in plan.skipped_halls:
            print(f"  · {name} (id={hall_id}): записей без номера в витринах — {orphans}")

    if not apply:
        print("\nЭто сухой прогон. Повторите с --apply.")


# ── CLI ─────────────────────────────────────────────────────────────────────────────────────
def _parse_delete_ids(raw: Optional[str], path: Optional[str]) -> List[int]:
    """id из --delete-ids и/или --delete-ids-file (по одному в строке, `#` — комментарий)."""
    tokens: List[str] = []
    if raw:
        tokens += [t for t in raw.replace(";", ",").split(",")]
    if path:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.split("#", 1)[0]
                tokens += line.replace(",", " ").split()
    ids = []
    for token in tokens:
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

    delete_ids = _parse_delete_ids(args.delete_ids, args.delete_ids_file)
    catalog = fetch_catalog()
    details_ok = load_orphan_details(catalog)
    if delete_ids and not details_ok:
        # Удаление разрешено только когда видно ВСЮ карточку: иначе «данных нет» может означать
        # «данные не прочитались», и мы снесём запись с фото и описанием.
        print(
            "ОШИБКА: карточки сирот прочитаны не полностью — удаление по --delete-ids отменено. "
            "Проверьте ADMIN_TOKEN и повторите.",
            file=sys.stderr,
        )
        return 1
    plan = classify_orphans(
        catalog, similarity=args.similarity, include_service=args.include_service, delete_ids=delete_ids
    )
    print_report(plan, args.similarity, args.apply, args.drop_empty_showcases)
    if not args.apply or not plan.actions:
        return 0
    print()
    return 1 if apply_plan(plan, args.drop_empty_showcases, args.rollback_file) else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="применить изменения (без ключа — сухой прогон)")
    parser.add_argument(
        "--similarity", type=float, default=DEFAULT_SIMILARITY,
        help=f"порог сходства названий для метки «вероятный дубль» (по умолчанию {DEFAULT_SIMILARITY})",
    )
    parser.add_argument(
        "--include-service", action="store_true",
        help="разбирать и служебные залы, включая «Вне постоянной экспозиции» (по умолчанию пропускаются)",
    )
    parser.add_argument("--delete-ids", help="удалить экспонаты по подтверждённому музеем списку id: 122,157")
    parser.add_argument("--delete-ids-file", help="то же списком из файла (id в строке, `#` — комментарий)")
    parser.add_argument(
        "--drop-empty-showcases", action="store_true",
        help="удалить витрины, опустевшие после переноса (п.4 — Верхняя буфетная)",
    )
    parser.add_argument(
        "--rollback-file", default=_default_rollback_path(),
        help="куда писать файл отката при --apply (по умолчанию — с датой в имени, в текущем каталоге)",
    )
    parser.add_argument("--rollback", metavar="FILE", help="откатить прогон по его файлу отката")
    args = parser.parse_args()
    if args.rollback and (args.delete_ids or args.delete_ids_file or args.drop_empty_showcases):
        parser.error("--rollback несовместим с ключами разбора каталога")
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
