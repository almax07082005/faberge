#!/usr/bin/env python3
"""Импорт витрин и экспонатов по путеводителю музея (баг-репорт 28.07.2026, п.4).

Заказчик: «сейчас во всех залах только по одной витрине, все экспонаты смешаны,
без указания своего реального номера». Руками через админку это 11 залов × 4–6
витрин — долго и ошибкоопасно, поэтому здесь разовый импорт по выписке из
путеводителя (http://fabergemuseum.ru/image/pdf/faberge_expo.pdf).

Вход — JSON с выпиской: зал → витрины (номер из путеводителя) → экспонаты
(номер, название, краткое описание). Формат и пояснения: db/guide_showcases.example.json.
Группа «не в витринах» (в путеводителе — пустой квадрат) задаётся витриной с
``"number": null``; в зале она одна.

Что делает скрипт для каждой витрины из файла:
  • витрина с таким номером в зале есть — берёт её; нет — создаёт;
  • экспонат из файла найден в каталоге (по label_slug либо по названию, без
    учёта регистра/«ё»/кавычек) — перепривязывает к нужной витрине и проставляет
    exhibit_number;
  • не найден — заводит новую карточку с названием, номером и кратким описанием
    (без фото: image_url пустой — как и просил заказчик).
Существующие экспонаты не удаляются и не переименовываются: скрипт только
перепривязывает и дозаполняет.

Сшивка ПОТРЕБЛЯЮЩАЯ: карточка, уже отданная одной строке выписки, второй строке
не достаётся, а сшивка по одному лишь названию не выходит за пределы целевого
зала. Без этого 150 строк «Портсигар» матчились в одну и ту же карточку и таскали
её по музею — подробности и цифры в ExhibitIndex.

Хвост несшитых записей (баг-репорт 06.08.2026, п.1). Раньше скрипт нумеровал и
раскладывал только то, что нашлось в выписке, а остальное молча оставалось там,
где лежало со старого импорта, — в первой витрине зала. Заказчик увидел эти 42
записи с ``exhibit_number = null`` как «лишние экспонаты в витрине №1». Теперь:
  • отчёт «не сшито N записей» печатается ВСЕГДА, с залом, id и названием, —
    хвост больше не теряется между строк плана;
  • ключ ``--sweep-unmatched`` переносит эти записи в группу «Не в витринах»
    того же зала (создавая её при необходимости).
По умолчанию перенос ВЫКЛЮЧЕН: это необратимое действие по данным заказчика
(обратной операции у скрипта нет — вернуть запись в витрину можно только руками),
а сшивка по названию всё равно остаётся догадкой: label_slug есть лишь у 232
карточек из 1253, а 704 носят неуникальное имя («Портсигар» — 150 штук, «Ковш» —
57), и выбор среди одноимённых делается по расположению, а не по смыслу. Решение
по каждой записи (дубль удалить / предмет вне путеводителя убрать из витрины)
музей принимает сам — п.1.2 баг-репорта. Поэтому дефолт консервативный: показать.

Свип трогает только записи БЕЗ ``exhibit_number`` — п.1.1 определяет «лишние
экспонаты» ровно так, и на проде 06.08.2026 номера нет ни у одной из 46 хвостовых
записей. Запись С номером попала в витрину прошлым успешным импортом: если она
выпала из сшивки, это промах на одноимённой карточке, а не «лишнее». Такие
остаются на месте и подсвечиваются в отчёте — иначе импорт своими руками устроил
бы то, на что жалуется заказчик, только наоборот: опустошил бы витрину.

Зал, по которому сшивка оборвалась на ошибке API (не создалась витрина), целиком
снимается с разбора хвоста: по неполным данным свип вынес бы из витрин карточки,
которые выписка как раз называет своими.

Трогаются только залы, перечисленные в файле: выписка считается полной для них и
ничего не говорит про остальной каталог, иначе импорт частичной выписки разнёс бы
залы, которых в ней нет.

Идемпотентен: повторный запуск на том же файле ничего не меняет. Держится это на
потребляющей сшивке — выписка, побуквенно повторяющая каталог, даёт ноль правок
(проверено прогоном такой выписки: 0 перепривязок, 0 номеров). Перенесённые свипом
записи уже вне пронумерованных витрин, обратно в витрины они не возвращаются.
По умолчанию — сухой прогон, печатает план (в том числе план переноса).

    BASE_URL=http://localhost:8000 ADMIN_TOKEN=dev-admin-token \\
        python scripts/import_guide_showcases.py db/guide_showcases.json
    ... --apply                      # применить
    ... --sweep-unmatched            # показать план с переносом хвоста
    ... --sweep-unmatched --apply    # применить вместе с переносом

Требует зависимостей проекта (используется общая нормализация названий из
app/services/recognizer.py — чтобы сшивка здесь и в распознавании была одной).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Dict, List, Optional, Set, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.recognizer import normalize_name  # noqa: E402

BASE = os.environ.get("BASE_URL", "http://localhost:8000").rstrip("/")
TOKEN = os.environ.get("ADMIN_TOKEN", os.environ.get("ADMIN_API_TOKEN", "dev-admin-token"))
UA = "faberge-guide-import/1.0"
PLACEHOLDER_MARKERS = ("<", ">")
# Название группы «вне витрин» — то же, что у cleanup_hall_catalog.py и в выписке
# (в путеводителе это пустой квадрат). В зале такая группа одна: повтор → 409.
UNNUMBERED_NAME = "Не в витринах"


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


def _has_placeholders(node) -> bool:
    """Остались ли в файле заглушки вида <название из путеводителя>."""
    if isinstance(node, str):
        return node.startswith(PLACEHOLDER_MARKERS[0]) and node.endswith(PLACEHOLDER_MARKERS[1])
    if isinstance(node, dict):
        return any(_has_placeholders(v) for k, v in node.items() if not k.startswith("_"))
    if isinstance(node, list):
        return any(_has_placeholders(v) for v in node)
    return False


def _find_hall(halls: List[dict], spec: dict) -> Optional[dict]:
    if spec.get("hall_number") is not None:
        return next((h for h in halls if h.get("hall_number") == spec["hall_number"]), None)
    wanted = normalize_name(spec.get("hall") or "")
    return next((h for h in halls if normalize_name(h.get("name") or "") == wanted), None)


class ExhibitIndex:
    """Потребляющий индекс каталога: одна карточка достаётся ровно одной строке выписки.

    ПОЧЕМУ потребляющий. Раньше индекс по имени был ``Dict[str, dict]`` и отдавал всем
    одноимённым строкам ОДНУ И ТУ ЖЕ первую карточку. В проде 704 карточки из 1253 носят
    неуникальное нормализованное имя, и 647 пронумерованных из них не имеют label_slug,
    то есть сшиваются только по названию: «Портсигар» — 150 карточек в 33 витринах,
    «Ковш» — 57 в 24, «Часы настольные» — 33 в 8. В итоге все 150 строк «Портсигар»
    матчились в одну карточку и таскали её по залам: прогон выписки, ПОБУКВЕННО
    повторяющей текущий прод (заведомый no-op), давал «перепривязать 508, номер 552»,
    98 карточек меняли место, 44 из них уезжали В ДРУГОЙ ЗАЛ. Второй прогон подряд давал
    не ноль, а новую порцию — обещанной идемпотентности не было и близко.

    Как выбирается карточка теперь:
      • label_slug — уникальный и осознанно проставленный ключ, ему верим сразу;
      • по названию — только СВОБОДНЫЕ карточки и только ВНУТРИ ЦЕЛЕВОГО ЗАЛА
        (сшивка по одному лишь имени не повод тащить экспонат через весь музей);
      • среди них предпочитаем ту, что уже лежит в нужной витрине, затем ту, у которой
        уже стоит ожидаемый номер, дальше — по возрастанию id (устойчиво к порядку выдачи).
    Занятая карточка второй раз не отдаётся никогда: лишняя строка выписки уходит в
    «не сшито», а не перетаскивает чужую запись.
    """

    def __init__(self, exhibits: List[dict]) -> None:
        self.by_slug: Dict[str, dict] = {}
        self.by_name: Dict[str, List[dict]] = {}
        self.used: Set[int] = set()
        for e in exhibits:
            slug = e.get("label_slug")
            if slug:
                self.by_slug.setdefault(slug, e)
            key = normalize_name(e.get("name") or "")
            if key:
                self.by_name.setdefault(key, []).append(e)

    def take(
        self, slug: Optional[str], name: str, hall_id: int,
        showcase_id: Optional[int], number: Optional[str],
    ) -> Tuple[Optional[dict], str]:
        """Забрать карточку под строку выписки. Возвращает (карточка, как сшили)."""
        candidate = self.by_slug.get(slug or "")
        if candidate is not None and candidate["id"] not in self.used:
            self.used.add(candidate["id"])
            return candidate, "slug"

        pool = [
            e for e in self.by_name.get(normalize_name(name), ())
            if e["id"] not in self.used and e.get("hall_id") == hall_id
        ]
        if not pool:
            return None, ""
        wanted = str(number).strip() if number else ""
        best = min(pool, key=lambda e: (
            0 if (showcase_id is not None and e.get("showcase_id") == showcase_id) else 1,
            0 if (wanted and str(e.get("exhibit_number") or "").strip() == wanted) else 1,
            e["id"],
        ))
        self.used.add(best["id"])
        return best, "name"


def _unmatched_in_numbered(exhibits: List[dict], hall_id: int, matched: set) -> List[dict]:
    """Хвост зала: записи в ПРОНУМЕРОВАННЫХ витринах, которых нет в выписке (п.1).

    Считается по снимку каталога, снятому до прогона, минус всё, что выписка
    забрала себе, — поэтому и в сухом прогоне список совпадает с тем, что
    останется в витринах после --apply. Записи, уже лежащие в группе «не в
    витринах» (showcase_number = null), в хвост не попадают: там им и место.
    """
    left = [
        e for e in exhibits
        if e.get("hall_id") == hall_id and e.get("showcase_number") is not None and e["id"] not in matched
    ]
    return sorted(left, key=lambda e: (e.get("showcase_number") or 0, e["id"]))


def _sweepable(ex: dict) -> bool:
    """Можно ли переносить запись свипом. Только те, у кого НЕТ exhibit_number.

    П.1.1 баг-репорта определяет «лишние экспонаты» ровно так: ``exhibit_number IS
    NULL`` в пронумерованной витрине (на проде 06.08.2026 таких 46, и номера нет ни
    у одной). Запись С номером попала в витрину прошлым УСПЕШНЫМ импортом, поэтому
    её выпадение из хвоста означает не «она лишняя», а что сшивка промахнулась:
    в каталоге 1253 экспоната, label_slug есть лишь у 232, а 704 карточки носят
    неуникальное название («Портсигар» — 162 штуки, «Ковш» — 63), и by_name отдаёт
    на все одну первую. Перенеси такую запись — и импорт своими руками устроит то,
    на что жалуется заказчик, только в обратную сторону: витрина опустеет.
    Такие записи остаются на месте и попадают в отчёт как повод проверить выписку.
    """
    return not ex.get("exhibit_number")


def run(path: str, apply: bool, sweep: bool) -> int:
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    if _has_placeholders(doc):
        print(
            f"ОШИБКА: в {path} остались заглушки <...>. Заполните выписку из путеводителя "
            "перед импортом (формат — db/guide_showcases.example.json).",
            file=sys.stderr,
        )
        return 1

    halls = get_all("/halls?include_service=true")
    exhibits = get_all("/exhibits")
    index = ExhibitIndex(exhibits)

    created_showcases = moved = created_exhibits = renumbered = swept = 0
    plan: List[str] = []
    # Залы, которых коснулась выписка: только по ним считается хвост и работает
    # --sweep-unmatched (п.1 — залы вне файла не наше дело). Ключ — id зала, чтобы
    # витрины зала жили в ОДНОМ словаре existing на весь прогон: иначе созданная
    # по файлу группа «не в витринах» не видна свипу и он получит 409 на повторе.
    halls_seen: Dict[int, dict] = {}
    matched_ids: set = set()   # id экспонатов, которые выписка забрала себе (по всему файлу)

    for hall_spec in doc.get("halls", []):
        hall = _find_hall(halls, hall_spec)
        title = hall_spec.get("hall") or f"№{hall_spec.get('hall_number')}"
        if hall is None:
            plan.append(f"! зал «{title}» не найден в каталоге — пропущен")
            continue
        ctx = halls_seen.get(hall["id"])
        if ctx is None:
            ctx = halls_seen[hall["id"]] = {
                "hall": hall,
                "existing": {s["showcase_number"]: s for s in get_all(f"/halls/{hall['id']}/showcases")},
                "matched": 0,
                "broken": False,   # сшивка зала оборвалась на ошибке API — см. ниже
            }
        existing = ctx["existing"]
        plan.append(f"Зал «{hall.get('name')}» (id={hall['id']}):")

        for sc_spec in hall_spec.get("showcases", []):
            number = sc_spec.get("number")
            label = f"витрина {number}" if number is not None else "группа «не в витринах»"
            showcase = existing.get(number)
            if showcase is None:
                created_showcases += 1
                plan.append(f"  + создать {label}" + (f" «{sc_spec['name']}»" if sc_spec.get("name") else ""))
                if apply:
                    status, body = api(
                        "POST", "/admin/showcases",
                        {"hall_id": hall["id"], "showcase_number": number, "name": sc_spec.get("name")},
                    )
                    if status != 201:
                        plan.append(f"    ОШИБКА создания витрины: {status} {body}")
                        # Экспонаты этой витрины остались непросмотренными: matched_ids по
                        # ним не заполнится, и хвост зала посчитает их «лишними». Пусти
                        # после этого свип — он вынесет из ЧУЖИХ витрин ровно те карточки,
                        # ради которых импорт и затевался: витрина опустеет руками самого
                        # импорта (то, на что жалуется заказчик, только наоборот). Поэтому
                        # зал целиком снимается с разбора хвоста, а не «продолжаем как есть».
                        ctx["broken"] = True
                        continue
                    showcase = body
                    existing[number] = showcase
                else:
                    # Сухой прогон: витрины нет и не будет. Заглушку в existing кладём, чтобы
                    # --sweep-unmatched не запланировал создание той же группы второй раз, и
                    # помечаем planned — иначе переносы В ЭТУ витрину не попадали бы в план
                    # (проверка `if showcase["id"]` на None давала False), сухой прогон
                    # печатал «перепривязать: 0», а --apply молча выпускал N PATCH'ей. Сверять
                    # глазами было нечего именно для самых новых витрин.
                    showcase = {"id": None, "planned": True, "number": number}
                    existing[number] = showcase

            target_id = showcase.get("id")
            # Куда экспонат поедет: id существующей витрины либо «новая витрина N» в плане.
            target_label = str(target_id) if target_id else f"новая {label}"

            for ex_spec in sc_spec.get("exhibits", []):
                name = ex_spec.get("name") or ""
                found, how = index.take(
                    ex_spec.get("label_slug"), name,
                    hall_id=hall["id"], showcase_id=target_id, number=ex_spec.get("number"),
                )
                if found is None:
                    created_exhibits += 1
                    plan.append(f"  + завести экспонат №{ex_spec.get('number')} «{name}» в {label}")
                    if apply and target_id:
                        payload = {
                            "showcase_id": target_id,
                            "name": name,
                            "exhibit_number": ex_spec.get("number"),
                            "short_description": ex_spec.get("short_description"),
                            "label_slug": ex_spec.get("label_slug"),
                            "image_url": ex_spec.get("image_url"),
                        }
                        status, body = api("POST", "/admin/exhibits", payload)
                        if status != 201:
                            plan.append(f"    ОШИБКА создания экспоната: {status} {body}")
                    continue

                # Запись сшита с выпиской — в хвост «не сшито» она не пойдёт, даже если
                # уехала в другой зал этого же файла (набор общий на весь прогон).
                matched_ids.add(found["id"])
                ctx["matched"] += 1

                patch: Dict[str, object] = {}
                shown: List[str] = []
                if (target_id or showcase.get("planned")) and found.get("showcase_id") != target_id:
                    patch["showcase_id"] = target_id
                    shown.append(f"showcase_id → {target_label}")
                    moved += 1
                if ex_spec.get("number") and found.get("exhibit_number") != ex_spec["number"]:
                    patch["exhibit_number"] = ex_spec["number"]
                    shown.append(f"exhibit_number → {ex_spec['number']}")
                    renumbered += 1
                if not patch:
                    continue
                # Межзальный перенос возможен только по label_slug (сшивка по имени заперта
                # в своём зале) — но и его показываем отдельно: это самое дорогое действие.
                cross = "  ⚠ ДРУГОЙ ЗАЛ" if found.get("hall_id") != hall["id"] else ""
                plan.append(
                    f"  ~ экспонат «{found['name']}» (id={found['id']}, по {how}): "
                    + ", ".join(shown) + cross
                )
                if apply and target_id:
                    status, body = api("PATCH", f"/admin/exhibits/{found['id']}", patch)
                    if status != 200:
                        plan.append(f"    ОШИБКА обновления: {status} {body}")
                        continue
                    # Снимок каталога держим в актуальном состоянии: по нему ниже считается
                    # хвост и по нему же индекс выбирает кандидатов для следующих строк.
                    found.update({
                        "showcase_id": target_id, "hall_id": hall["id"], "showcase_number": number,
                        "exhibit_number": patch.get("exhibit_number", found.get("exhibit_number")),
                    })

    # ── Хвост: что осталось в пронумерованных витринах мимо выписки (п.1) ──────────
    unmatched = kept_numbered = 0
    report: List[str] = []
    for hall_id, ctx in halls_seen.items():
        hall, existing = ctx["hall"], ctx["existing"]
        if ctx["broken"]:
            # Часть выписки по залу не дошла до сшивки — считать по ней «лишнее» нельзя
            # (см. ОШИБКА создания витрины выше). Молчать тоже нельзя: без строки в отчёте
            # зал просто исчез бы из итогов и выглядел бы разобранным.
            report.append(
                f"Зал «{hall.get('name')}» (id={hall_id}): сшивка оборвалась на ошибке API — "
                "хвост по залу не считается, перенос отменён. Повторите прогон после починки."
            )
            continue
        left = _unmatched_in_numbered(exhibits, hall_id, matched_ids)
        if not left:
            continue
        unmatched += len(left)
        numbered = [e for e in left if not _sweepable(e)]
        kept_numbered += len(numbered)
        report.append(f"Зал «{hall.get('name')}» (id={hall_id}) — не сшито {len(left)}:")
        if numbered:
            # Записи с номером в хвосте — почти всегда промах сшивки на одноимённых
            # карточках, а не «лишнее» (см. _sweepable). Свип их не трогает, но молчать
            # нельзя: это сигнал, что выписку по залу надо сверить построчно.
            report.append(
                f"  ! {len(numbered)} из них с проставленным номером — их поставил прошлый импорт. "
                "Скорее всего сшивка промахнулась на одноимённых карточках; свип их не переносит, "
                "сверьте выписку по залу"
            )
        if len(left) > ctx["matched"]:
            # Красный флаг: выписка по залу покрывает меньше, чем оставляет. Обычно это
            # значит, что в файл перенесли не всю таблицу зала, — переносить хвост нельзя.
            report.append(
                f"  ! в выписке по залу {ctx['matched']} записей, в витринах остаётся {len(left)} — "
                "похоже на неполную выписку, проверьте файл перед переносом"
            )

        # Группа заводится, только если есть кого в неё нести: зал, где весь хвост —
        # записи с номером, свипу не подлежит, и пустую группу ему создавать незачем.
        want_group = sweep and any(_sweepable(e) for e in left)
        target = existing.get(None) if want_group else None
        if want_group and target is None:
            created_showcases += 1
            report.append(f"  + создать группу «{UNNUMBERED_NAME}»")
            if apply:
                status, body = api(
                    "POST", "/admin/showcases",
                    {"hall_id": hall_id, "showcase_number": None, "name": UNNUMBERED_NAME},
                )
                if status != 201:
                    report.append(f"    ОШИБКА создания группы: {status} {body} — перенос по залу отменён")
                    target = None
                else:
                    target = existing[None] = body
            else:
                target = existing[None] = {"id": None}
        # В сухом прогоне id у группы нет — это нормально, переносим только на --apply.
        movable = want_group and target is not None and (not apply or bool(target.get("id")))

        for ex in left:
            line = f"  · id={ex['id']} «{ex.get('name')}» — витрина {ex.get('showcase_number')}"
            if not _sweepable(ex):
                # Номер есть, а в выписке записи нет — остаётся в витрине, только сигнал.
                report.append(f"{line}, номер {ex['exhibit_number']} — оставлен в витрине")
                continue
            if not movable:
                report.append(line)
                continue
            swept += 1
            report.append(f"{line} → «{UNNUMBERED_NAME}»")
            if apply:
                status, body = api("PATCH", f"/admin/exhibits/{ex['id']}", {"showcase_id": target["id"]})
                if status != 200:
                    report.append(f"    ОШИБКА переноса: {status} {body}")

    print("План" if not apply else "Применено")
    for line in plan:
        print(" ", line)

    # Отчёт печатается всегда, даже когда хвоста нет: заказчику важно видеть строку,
    # а не догадываться, посчитали её или забыли (п.1.3 баг-репорта 06.08.2026).
    print("\nНе сшито с выпиской (осталось в пронумерованных витринах):")
    if not report:
        print("  — нет: в пронумерованных витринах перечисленных залов только записи из выписки")
    for line in report:
        print(" ", line)

    print(
        f"\nВитрин создать: {created_showcases}; экспонатов завести: {created_exhibits}; "
        f"перепривязать: {moved}; проставить номер: {renumbered}; не сшито: {unmatched}"
        + (f" (перенести в «{UNNUMBERED_NAME}»: {swept})" if sweep else "")
    )
    if kept_numbered:
        print(
            f"Из них {kept_numbered} с проставленным номером — оставлены в витринах, свип их не трогает: "
            "это похоже на промах сшивки, а не на лишние записи. Сверьте выписку по этим залам."
        )
    if unmatched > kept_numbered and not sweep:
        print(f"Перенести хвост в «{UNNUMBERED_NAME}» — ключ --sweep-unmatched (по умолчанию выключен).")
    if not apply:
        print("\nЭто сухой прогон. Повторите с --apply.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("file", help="JSON с выпиской из путеводителя (формат: db/guide_showcases.example.json)")
    parser.add_argument("--apply", action="store_true", help="применить изменения (без ключа — сухой прогон)")
    parser.add_argument(
        "--sweep-unmatched", action="store_true",
        help="перенести несшитые записи БЕЗ номера из пронумерованных витрин в группу "
             f"«{UNNUMBERED_NAME}» того же зала (без ключа они только перечисляются в отчёте; "
             "записи с проставленным номером не переносятся никогда — см. описание)",
    )
    args = parser.parse_args()
    return run(args.file, args.apply, args.sweep_unmatched)


if __name__ == "__main__":
    sys.exit(main())
