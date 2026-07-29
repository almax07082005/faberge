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

Идемпотентен: повторный запуск на том же файле ничего не меняет.
По умолчанию — сухой прогон, печатает план.

    BASE_URL=http://localhost:8000 ADMIN_TOKEN=dev-admin-token \\
        python scripts/import_guide_showcases.py db/guide_showcases.json
    ... --apply     # применить

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
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.recognizer import normalize_name  # noqa: E402

BASE = os.environ.get("BASE_URL", "http://localhost:8000").rstrip("/")
TOKEN = os.environ.get("ADMIN_TOKEN", os.environ.get("ADMIN_API_TOKEN", "dev-admin-token"))
UA = "faberge-guide-import/1.0"
PLACEHOLDER_MARKERS = ("<", ">")


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


def _index_exhibits(exhibits: List[dict]) -> Tuple[Dict[str, dict], Dict[str, dict]]:
    by_slug = {e["label_slug"]: e for e in exhibits if e.get("label_slug")}
    by_name: Dict[str, dict] = {}
    for e in exhibits:
        key = normalize_name(e.get("name") or "")
        if key and key not in by_name:   # дубли имён — берём первый (тот же принцип, что в crud.slug_by_name)
            by_name.setdefault(key, e)
    return by_slug, by_name


def run(path: str, apply: bool) -> int:
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
    by_slug, by_name = _index_exhibits(exhibits)

    created_showcases = moved = created_exhibits = renumbered = 0
    plan: List[str] = []

    for hall_spec in doc.get("halls", []):
        hall = _find_hall(halls, hall_spec)
        title = hall_spec.get("hall") or f"№{hall_spec.get('hall_number')}"
        if hall is None:
            plan.append(f"! зал «{title}» не найден в каталоге — пропущен")
            continue
        existing = {s["showcase_number"]: s for s in get_all(f"/halls/{hall['id']}/showcases")}
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
                        continue
                    showcase = body
                    existing[number] = showcase
                else:
                    showcase = {"id": None}

            for ex_spec in sc_spec.get("exhibits", []):
                name = ex_spec.get("name") or ""
                found = by_slug.get(ex_spec.get("label_slug") or "") or by_name.get(normalize_name(name))
                if found is None:
                    created_exhibits += 1
                    plan.append(f"  + завести экспонат №{ex_spec.get('number')} «{name}» в {label}")
                    if apply and showcase.get("id"):
                        payload = {
                            "showcase_id": showcase["id"],
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

                patch = {}
                if showcase.get("id") and found.get("showcase_id") != showcase["id"]:
                    patch["showcase_id"] = showcase["id"]
                    moved += 1
                if ex_spec.get("number") and found.get("exhibit_number") != ex_spec["number"]:
                    patch["exhibit_number"] = ex_spec["number"]
                    renumbered += 1
                if not patch:
                    continue
                plan.append(
                    f"  ~ экспонат «{found['name']}» (id={found['id']}): "
                    + ", ".join(f"{k} → {v}" for k, v in patch.items())
                )
                if apply:
                    status, body = api("PATCH", f"/admin/exhibits/{found['id']}", patch)
                    if status != 200:
                        plan.append(f"    ОШИБКА обновления: {status} {body}")

    print("План" if not apply else "Применено")
    for line in plan:
        print(" ", line)
    print(
        f"\nВитрин создать: {created_showcases}; экспонатов завести: {created_exhibits}; "
        f"перепривязать: {moved}; проставить номер: {renumbered}"
    )
    if not apply:
        print("\nЭто сухой прогон. Повторите с --apply.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("file", help="JSON с выпиской из путеводителя (формат: db/guide_showcases.example.json)")
    parser.add_argument("--apply", action="store_true", help="применить изменения (без ключа — сухой прогон)")
    args = parser.parse_args()
    return run(args.file, args.apply)


if __name__ == "__main__":
    sys.exit(main())
