#!/usr/bin/env python3
"""Подготовка данных для нагрузочного тестирования (loadtest/).

Собирает с живого API реальные id залов и экспонатов и кладёт их в
``loadtest/fixtures.json``. Сценарии k6 ходят по существующему каталогу — иначе
прогон мерил бы скорость выдачи 404, а не рабочий путь.

Ничего не пишет в БД: только GET-запросы к каталогу и /health.

    BASE_URL=http://localhost:8000 python scripts/loadtest_prepare.py

Опции:
    --photo PATH   фото для POST /recognition (по умолчанию — из media/)
    --out DIR      куда сложить (по умолчанию loadtest/)

Предполётный отчёт показывает, какие внешние сервисы у стенда включены:
YandexGPT/SpeechKit/YOLO в режиме `up` означают, что прогон будет их дёргать —
это деньги и квоты. Для «бесплатного» стенда снимите YANDEX_API_KEY,
SPEECHKIT_API_KEY и YOLO_ENDPOINT — сервисы уйдут в стабы (llm.py, tts.py,
recognizer.py), а профиль нагрузки на наш код останется тем же.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE_URL = os.environ.get("BASE_URL", "http://localhost:8000").rstrip("/")
ROOT = Path(__file__).resolve().parent.parent

# Вопросы для POST /guide/chat. Смесь по типам, которые различает гид
# (guide_intel.py): навигационные, про залы, про конкретный экспонат, общие —
# чтобы прогон задевал разные ветки, а не только самую дешёвую.
QUESTIONS = [
    "Расскажи об этом экспонате",
    "Кто был мастером этого яйца?",
    "В каком году это создано?",
    "Где находится Коронационное яйцо?",
    "Какие залы есть в музее?",
    "Как пройти в Рыцарский зал?",
    "Что подарил Николай II императрице?",
    "Чем знаменит Фаберже?",
    "Из чего сделан этот предмет?",
    "Покажи самые известные экспонаты",
    "Сколько всего пасхальных яиц Фаберже?",
    "Что такое гильошированная эмаль?",
]


def fetch(path: str) -> dict:
    url = f"{BASE_URL}{path}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        sys.exit(f"ОШИБКА: {url} → {exc.code} {exc.reason}")
    except urllib.error.URLError as exc:
        sys.exit(f"ОШИБКА: {url} недоступен ({exc.reason}). Поднят ли API на BASE_URL?")


def fetch_all(path: str, page: int = 100, cap: int = 2000) -> list:
    """Выгрести список постранично. Максимум limit у API — 100 (dependencies.py)."""
    items: list = []
    offset = 0
    while offset < cap:
        sep = "&" if "?" in path else "?"
        data = fetch(f"{path}{sep}limit={page}&offset={offset}")
        batch = data.get("items") or []
        items.extend(batch)
        total = data.get("total", len(items))
        offset += page
        if len(batch) < page or len(items) >= total:
            break
    return items


def find_photo() -> Path | None:
    """Найти реальное фото экспоната в media/."""
    media = ROOT / "media"
    if not media.is_dir():
        return None
    candidates = sorted(media.rglob("*.jpg")) + sorted(media.rglob("*.jpeg"))
    if not candidates:
        return None
    # Берём файл среднего размера: самый маленький нереалистично лёгок для
    # снимка с телефона, самый большой упрётся в MAX_UPLOAD_MB.
    by_size = sorted(candidates, key=lambda p: p.stat().st_size)
    return by_size[len(by_size) // 2]


def preflight() -> dict:
    health = fetch("/health")
    deps = health.get("dependencies", {})
    print(f"Стенд:   {BASE_URL}")
    print(f"Версия:  {health.get('version')}   статус: {health.get('status')}")
    print("Зависимости:")
    for name in ("postgres", "object_storage", "yolo", "yandexgpt", "speechkit"):
        state = deps.get(name, "?")
        mark = "живой" if state == "up" else "стаб/выключен"
        print(f"  {name:<15} {state:<5} — {mark}")

    if deps.get("postgres") != "up":
        sys.exit("ОШИБКА: postgres down — мерить нечего.")

    paid = [n for n in ("yolo", "yandexgpt", "speechkit") if deps.get(n) == "up"]
    if paid:
        print()
        print(f"ВНИМАНИЕ: включены платные сервисы: {', '.join(paid)}.")
        print("  Прогон будет тратить квоты и деньги. Варианты:")
        print("    • SKIP_EXTERNAL=true у k6 — сценарий не трогает эти пути;")
        print("    • снять ключи на стенде — сервисы уйдут в стабы.")
    return deps


def main() -> None:
    parser = argparse.ArgumentParser(description="Собрать fixtures для нагрузочного теста.")
    parser.add_argument("--photo", type=Path, help="Фото для POST /recognition (JPEG).")
    parser.add_argument("--out", type=Path, default=ROOT / "loadtest", help="Каталог оснастки.")
    args = parser.parse_args()

    deps = preflight()
    print()

    halls = fetch_all("/halls")
    exhibits = fetch_all("/exhibits")
    if not halls:
        sys.exit("ОШИБКА: /halls пуст. Применён ли db/seed.sql?")
    if not exhibits:
        sys.exit("ОШИБКА: /exhibits пуст. Применён ли db/seed.sql?")

    # Поисковые запросы — из реальных названий: так полнотекстовый поиск что-то
    # находит, и мы меряем путь с выдачей, а не пустой ответ.
    queries: list[str] = []
    for ex in exhibits[:40]:
        name = (ex.get("name") or "").strip()
        if name:
            queries.append(name.split()[0] if len(name.split()) > 2 else name)
    for hall in halls[:10]:
        name = (hall.get("name") or "").strip()
        if name:
            queries.append(name)
    queries = sorted(set(q for q in queries if len(q) >= 3))

    fixtures = {
        "base_url": BASE_URL,
        "dependencies": deps,
        "halls": [{"id": h["id"], "hall_number": h.get("hall_number"), "name": h.get("name")} for h in halls],
        "exhibits": [
            {
                "id": e["id"],
                "hall_id": e.get("hall_id"),
                "label_slug": e.get("label_slug"),
                "name": e.get("name"),
            }
            for e in exhibits
        ],
        "queries": queries,
        "questions": QUESTIONS,
    }

    out = args.out
    (out / "fixtures").mkdir(parents=True, exist_ok=True)
    (out / "fixtures.json").write_text(
        json.dumps(fixtures, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    photo = args.photo or find_photo()
    if photo is None or not photo.is_file():
        sys.exit(
            "ОШИБКА: нет фото для /recognition. Укажите --photo PATH "
            "(JPEG до MAX_UPLOAD_MB = 10 МБ)."
        )
    dest = out / "fixtures" / "photo.jpg"
    shutil.copyfile(photo, dest)

    size_kb = dest.stat().st_size / 1024
    print(f"Залов:     {len(fixtures['halls'])}")
    print(f"Экспонатов:{len(fixtures['exhibits']):>4}")
    print(f"Запросов:  {len(queries)}")
    print(f"Вопросов:  {len(QUESTIONS)}")
    print(f"Фото:      {dest.relative_to(ROOT)} ({size_kb:.0f} КБ, из {photo.name})")
    if size_kb < 200:
        print("  ЗАМЕТКА: снимок с телефона обычно 1–4 МБ. Лёгкое фото занижает")
        print("  время загрузки тела — для честной цифры подложите --photo побольше.")
    print()
    print(f"Готово: {(out / 'fixtures.json').relative_to(ROOT)}")
    print("Дальше — проверочный проход:")
    print(f"  k6 run -e BASE_URL={BASE_URL} -e TIME_SCALE=1000 loadtest/scenarios/smoke.js")


if __name__ == "__main__":
    main()
