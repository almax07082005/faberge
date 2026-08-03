#!/usr/bin/env python3
"""Интеграционный smoke по ТЗ «Аналитика посетителей» от 03.08.2026.

Проверяет пункты DoD на живом API: контракт телеметрии, включающую границу
периода, смысловую группировку вопросов, вопросы без ответа гида, разбиение
сессии на визиты, метрики визита, точки выхода и повторные визиты, статистику
по экспонатам, качество распознавания, выгрузку в .xlsx/.pdf и ночной пересчёт.

Требует запущенного API с применённой схемой (миграция 2026-08-03_analytics.sql)
и загруженным db/seed.sql.

    BASE_URL=http://localhost:8000 ADMIN_TOKEN=dev-admin-token \\
        python scripts/smoke_analytics_20260803.py

Скрипт ПИШЕТ данные: телеметрию за отдельный период (см. PERIOD_*) и несколько
реплик в /guide/chat. На боевой БД не запускать.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone

BASE_URL = os.environ.get("BASE_URL", "http://localhost:8000").rstrip("/")
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "dev-admin-token")

# Отдельное «окно» в прошлом, чтобы smoke не смешивался с реальной телеметрией.
# Последний день окна намеренно совпадает с границей `to` — так проверяется, что
# граница включающая (§2).
PERIOD_FROM = "2026-02-01"
PERIOD_TO = "2026-02-28"
BASE_TS = datetime(2026, 2, 28, 11, 0, tzinfo=timezone.utc)

passed = failed = 0


def check(condition: bool, title: str, detail: str = "") -> None:
    global passed, failed
    if condition:
        passed += 1
        print(f"PASS {title}")
    else:
        failed += 1
        print(f"FAIL {title}" + (f" — {detail}" if detail else ""))


def request(method: str, path: str, body=None, admin: bool = False, raw: bool = False):
    url = BASE_URL + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if admin:
        req.add_header("Authorization", f"Bearer {ADMIN_TOKEN}")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            payload = resp.read()
            if raw:
                return resp.status, payload, dict(resp.headers)
            return resp.status, json.loads(payload or b"null"), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        payload = exc.read()
        if raw:
            return exc.code, payload, dict(exc.headers)
        try:
            return exc.code, json.loads(payload or b"null"), dict(exc.headers)
        except json.JSONDecodeError:
            return exc.code, None, dict(exc.headers)


def analytics(name: str, **params):
    query = {"from": PERIOD_FROM, "to": PERIOD_TO, **params}
    status, body, _ = request("GET", f"/admin/analytics/{name}?" + urllib.parse.urlencode(query), admin=True)
    assert status == 200, f"{name} → {status}: {body}"
    return body


def ts(minutes: int) -> str:
    return (BASE_TS + timedelta(minutes=minutes)).isoformat()


def main() -> int:
    # Экспонаты и залы из каталога — на них вешаем события.
    _s, halls, _h = request("GET", "/halls?limit=3")
    _s, exhibits, _h = request("GET", "/exhibits?limit=3")
    hall_ids = [h["id"] for h in halls["items"]]
    exhibit_ids = [e["id"] for e in exhibits["items"]]
    if len(hall_ids) < 3 or len(exhibit_ids) < 3:
        print("НЕТ ДАННЫХ: нужен применённый db/seed.sql (≥3 зала и ≥3 экспоната)")
        return 1

    device_a, device_b = str(uuid.uuid4()), str(uuid.uuid4())
    sess = [str(uuid.uuid4()) for _ in range(4)]

    # ── §1 Словарь типов и валидация приёма ──────────────────────────────────
    status, body, _ = request("POST", "/telemetry/events", {
        "session_id": sess[0], "device_id": device_a,
        "events": [
            {"type": "app_open", "ts": ts(0),
             "props": {"entry": "entrance", "user_agent": "Mozilla/5.0", "ip": "203.0.113.7"}},
            {"type": "exhibitView", "ts": ts(0)},          # опечатка — должна быть отброшена
            {"type": "hall-view", "ts": ts(0)},            # опечатка — должна быть отброшена
            {"type": "hall_view", "ts": ts(1), "hall_id": hall_ids[0]},
            {"type": "exhibit_view", "ts": ts(2), "exhibit_id": exhibit_ids[0],
             "props": {"source": "hall"}},
            {"type": "audio_play", "ts": ts(3), "exhibit_id": exhibit_ids[0]},  # → tts_play
            {"type": "chat_open", "ts": ts(4), "exhibit_id": exhibit_ids[0]},
            {"type": "chat_message", "ts": ts(5), "exhibit_id": exhibit_ids[0],
             "props": {"text": "сколько стоит яйцо"}},
            {"type": "hall_view", "ts": ts(6), "hall_id": hall_ids[1]},
            {"type": "exhibit_view", "ts": ts(7), "exhibit_id": exhibit_ids[1],
             "props": {"source": "hall"}},
            # …та же сессия спустя 4 часа — по §5 это отдельный визит
            {"type": "hall_view", "ts": ts(247), "hall_id": hall_ids[2]},
            {"type": "exhibit_view", "ts": ts(250), "exhibit_id": exhibit_ids[2],
             "props": {"source": "hall"}},
        ],
    })
    check(status == 202, "§1 батч принят (202)", f"{status}")
    check(body == {"accepted": 10, "rejected": 2},
          "§1 неизвестный тип отброшен поштучно, остальные записаны", json.dumps(body))

    status, _b, _h = request("POST", "/telemetry/events",
                             {"session_id": sess[0], "events": [{"type": "hall_view"}] * 51})
    check(status == 422, "§1 батч > 50 событий отклонён (422)", f"{status}")

    # Повторный визит того же устройства + распознавание с фолбэком и уходом.
    request("POST", "/telemetry/events", {"session_id": sess[1], "device_id": device_a, "events": [
        {"type": "app_open", "ts": ts(500)},
        {"type": "hall_view", "ts": ts(501), "hall_id": hall_ids[0]},
    ]})
    request("POST", "/telemetry/events", {"session_id": sess[2], "device_id": device_b, "events": [
        {"type": "app_open", "ts": ts(10)},
        {"type": "recognition", "ts": ts(11),
         "props": {"recognized": False, "fallback": True, "confidence": 0.42, "candidates_count": 3}},
        {"type": "exhibit_view", "ts": ts(12), "exhibit_id": exhibit_ids[0], "props": {"source": "recognition"}},
        {"type": "recognition", "ts": ts(13), "exhibit_id": exhibit_ids[0],
         "props": {"recognized": True, "confidence": 0.95, "fallback": False}},
        {"type": "session_end", "ts": ts(14), "props": {"reason": "manual", "last_screen": "exhibit"}},
    ]})
    request("POST", "/telemetry/events", {"session_id": sess[3], "events": [   # без device_id
        {"type": "app_open", "ts": ts(20)},
        {"type": "recognition", "ts": ts(21), "props": {"recognized": False, "fallback": False, "confidence": 0.2}},
        {"type": "session_end", "ts": ts(22), "props": {"reason": "timeout"}},
    ]})

    # ── §2 Включающая граница периода ────────────────────────────────────────
    overview = analytics("overview")
    check(overview["total_sessions"] >= 4,
          f"§2 события {PERIOD_TO} попадают в период from..to (граница включающая)",
          json.dumps(overview.get("total_sessions")))
    check(overview.get("updated_at") is not None, "§12 в ответе есть updated_at")
    check(overview["total_audio_plays"] >= 1, "§1 audio_play нормализован в tts_play и посчитан")

    # ── §3 Смысловая группировка вопросов ────────────────────────────────────
    chat_session = None
    for message in ("Сколько стоит это яйцо?", "какая цена яйца", "Сколько это стоит"):
        payload = {"message": message, "context": {"exhibit_id": exhibit_ids[0]}}
        if chat_session:
            payload["session_id"] = chat_session
        _s, answer, _h = request("POST", "/guide/chat", payload)
        chat_session = answer["session_id"]
    # Вопрос, на который гид заведомо не ответит (нет такого экспоната).
    request("POST", "/guide/chat", {"message": "как найти квазарный дифферинциал зюзюблик"})

    questions = analytics("questions", **{"from": "2026-02-01", "to": "2030-01-01"})
    price = [c for c in questions["frequent"] if c["count"] >= 3 and c["variants"]]
    check(bool(price), "§3 перефразировки про цену собраны в один кластер",
          json.dumps([c["question"] for c in questions["frequent"]], ensure_ascii=False))
    if price:
        check(len(price[0]["variants"]) >= 2, "§3 у кластера видны другие формулировки",
              json.dumps(price[0]["variants"], ensure_ascii=False))
    frequent = {c["question"] for c in questions["frequent"]}
    rare = {c["question"] for c in questions["rare"]}
    check(not (frequent & rare), "§3 frequent и rare не пересекаются", json.dumps(sorted(frequent & rare)))

    # ── §4 Вопросы без ответа гида ───────────────────────────────────────────
    unanswered = analytics("unanswered", **{"from": "2026-02-01", "to": "2030-01-01"})
    check(unanswered["total_unanswered"] >= 1, "§4 отказ гида записан с answered = false")
    check("not_found" in unanswered["fail_reasons"],
          "§4 причина отказа различима (not_found)", json.dumps(unanswered["fail_reasons"]))
    check(bool(unanswered["items"]), "§4 отчёт отдаёт сгруппированный список")

    # ── §5/§6 Визиты и метрики визита ────────────────────────────────────────
    engagement = analytics("engagement")
    check(engagement["total_visits"] > engagement["total_sessions"],
          "§5 сессия с разрывом 4 часа посчитана двумя визитами",
          f"визитов={engagement['total_visits']} сессий={engagement['total_sessions']}")
    check(engagement["max_duration_sec"] < 4 * 3600,
          "§5 длительность визита не растянута на разрыв", str(engagement["max_duration_sec"]))
    for field in ("avg_exhibits_per_session", "avg_questions_per_session",
                  "chat_conversion_rate", "question_conversion_rate", "sessions_with_chat"):
        check(field in engagement, f"§6 в ответе есть {field}")
    check(engagement["sessions_with_app_open"] > 0,
          "§6 конверсия считается от визитов с app_open")

    # ── §7 Точки выхода и повторные визиты ───────────────────────────────────
    routes = analytics("routes")
    check(bool(routes["top_exit_halls"]), "§7 видно, на каких залах заканчивается визит")
    check(bool(routes["top_exit_screens"]), "§7 видно, на каких экранах заканчивается визит")
    check(routes["returning_devices"] >= 1, "§7 повторные визиты считаются по device_id",
          json.dumps(routes["returning_devices"]))
    check(routes["total_devices"] >= 3,
          "§7 сессии без device_id учтены как одиночные устройства", json.dumps(routes["total_devices"]))

    # ── §8 Статистика по экспонатам ──────────────────────────────────────────
    exhibits_report = analytics("exhibits", limit=200, order="asc")
    zero = [row for row in exhibits_report["items"] if row["views"] == 0]
    check(bool(zero), "§8 экспонат без просмотров присутствует в ответе (views: 0)")
    check(exhibits_report["never_viewed"] > 0, "§8 счётчик «мёртвых» карточек заполнен")
    limited = analytics("exhibits", limit=2)
    check(len(limited["items"]) == 2, "§8 размер выдачи управляется параметром limit")
    by_questions = analytics("exhibits", limit=5, order="questions")
    check(by_questions["order"] == "questions" and by_questions["items"][0]["questions"] >= 0,
          "§8 сортировка по числу вопросов доступна отдельно от просмотров")

    # ── §9 Качество распознавания ────────────────────────────────────────────
    recognition = analytics("recognition")
    check(recognition["total"] >= 3, "§9 попытки распознавания посчитаны")
    check(recognition["fallback_shown"] >= 1 and recognition["fallback_converted"] >= 1,
          "§9 фолбэк с топ-3 и переход в карточку кандидата посчитаны",
          json.dumps({k: recognition[k] for k in ("fallback_shown", "fallback_converted")}))
    check(recognition["retry_after_fail"] >= 1, "§9 повторная съёмка не считается уходом")
    check(recognition["abandoned_after_fail"] >= 1, "§9 уход после неудачи посчитан")

    # ── §11 Выгрузка отчётов ─────────────────────────────────────────────────
    for report in ("overview", "questions", "unanswered", "exhibits", "routes", "recognition"):
        for fmt, mime in (("xlsx", "spreadsheetml"), ("pdf", "application/pdf")):
            query = urllib.parse.urlencode({"report": report, "format": fmt,
                                            "from": PERIOD_FROM, "to": PERIOD_TO})
            status, data, headers = request("GET", f"/admin/analytics/export?{query}", admin=True, raw=True)
            ok = status == 200 and mime in headers.get("content-type", "") and len(data) > 1000
            name_ok = f"faberge-{report}-{PERIOD_FROM}-{PERIOD_TO}.{fmt}" in headers.get("content-disposition", "")
            check(ok and name_ok, f"§11 выгрузка {report}.{fmt} скачивается",
                  f"{status} {headers.get('content-type')} {len(data) if isinstance(data, bytes) else '?'} байт")

    # ── §12 Ночной пересчёт ──────────────────────────────────────────────────
    query = urllib.parse.urlencode({"from": PERIOD_FROM, "to": PERIOD_TO})
    status, first, _h = request("POST", f"/admin/analytics/rebuild?{query}", admin=True)
    check(status == 200 and first["daily_rows"] > 0, "§12 ручной пересчёт доступен админу", json.dumps(first))
    status, second, _h = request("POST", f"/admin/analytics/rebuild?{query}", admin=True)
    check(first["daily_rows"] == second["daily_rows"] and first["daily_days"] == second["daily_days"],
          "§12 повторный пересчёт за ту же дату не удваивает цифры",
          f"{first['daily_rows']} → {second['daily_rows']}")
    daily = analytics("daily", metric="events_by_type")
    check(bool(daily["points"]), "§12 суточный срез заполнен")
    for name in ("overview", "questions", "unanswered", "engagement", "routes", "exhibits", "recognition"):
        report = analytics(name)
        if report.get("updated_at") is None:
            check(False, f"§12 {name}: в ответе есть updated_at")
            break
    else:
        check(True, "§12 во всех отчётах есть updated_at")

    print()
    print(f"{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
