#!/usr/bin/env python3
"""Интеграционный smoke по ТЗ «Правки контракта телеметрии» от 04.08.2026.

Проверяет на живом API оба пункта DoD:

  §1  `props.retry` у `recognition` проходит белый список и доезжает до БД,
      а прочие ключи вне контракта по-прежнему отбрасываются;
  §2  каталог залов почищен (`scripts/cleanup_hall_catalog.py --apply`):
      «Вне постоянной экспозиции» без номера, тестового зала «Название потом
      придумаем» нет. Проверка по «Парадной лестнице» сверяется с состоянием
      ПОСЛЕ отмены решения 31.08.2026 (п. I-1): зал публичный, стоит первым и
      служебным больше не помечен. До 31.08.2026 здесь проверялось обратное —
      разбор обоих решений в docs/staircase-hall-decision.md.

Проверка §1 опирается на то, что «одинокое» успешное распознавание с
`retry: true` эвристикой не считается повтором — если `retry_after_fail`
вырос, значит поле реально сохранилось.

    BASE_URL=https://…apigw.yandexcloud.net ADMIN_TOKEN=… \\
        python scripts/smoke_telemetry_20260804.py

Скрипт ПИШЕТ телеметрию за отдельное окно в прошлом (см. PERIOD_*), чтобы не
смешиваться с реальными визитами, и вызывает пересчёт агрегатов за это окно.
Каждый прогон добавляет 3 события; в отчётах без фильтра дат они видны. Убрать:

    DELETE FROM events WHERE ts >= '2026-01-05' AND ts < '2026-01-07';
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

# Своё окно в прошлом — отдельное от окна smoke от 03.08.2026 (февраль).
PERIOD_FROM = "2026-01-05"
PERIOD_TO = "2026-01-06"
BASE_TS = datetime(2026, 1, 5, 12, 0, tzinfo=timezone.utc)

STAIRCASE = "Парадная лестница"
OUTSIDE_EXPO = "Вне постоянной экспозиции"
PLACEHOLDER = "Название потом придумаем"

passed = failed = 0


def check(condition: bool, title: str, detail: str = "") -> None:
    global passed, failed
    if condition:
        passed += 1
        print(f"PASS {title}")
    else:
        failed += 1
        print(f"FAIL {title}" + (f" — {detail}" if detail else ""))


def request(method: str, path: str, body=None, admin: bool = False):
    req = urllib.request.Request(
        BASE_URL + path,
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
    )
    if body is not None:
        req.add_header("Content-Type", "application/json")
    if admin:
        req.add_header("Authorization", f"Bearer {ADMIN_TOKEN}")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as exc:
        payload = exc.read()
        try:
            return exc.code, json.loads(payload or b"null")
        except json.JSONDecodeError:
            return exc.code, None


def ts(minutes: int) -> str:
    return (BASE_TS + timedelta(minutes=minutes)).isoformat()


def recognition_report() -> dict:
    """Отчёт по распознаванию за окно smoke.

    GET отдаёт кэш агрегатов (TTL сутки), поэтому перед чтением пересчитываем
    период через rebuild — с тем же `from`/`to`, иначе перезапишется чужой ключ.
    """
    query = urllib.parse.urlencode({"from": PERIOD_FROM, "to": PERIOD_TO})
    status, body = request("POST", f"/admin/analytics/rebuild?{query}", admin=True)
    assert status == 200, f"rebuild → {status}: {body}"
    status, body = request("GET", f"/admin/analytics/recognition?{query}", admin=True)
    assert status == 200, f"recognition → {status}: {body}"
    return body


def check_retry_contract() -> None:
    """§1: `retry` сохраняется, остальное вне контракта — нет."""
    before = recognition_report()

    session_id = str(uuid.uuid4())
    status, body = request("POST", "/telemetry/events", {
        "session_id": session_id, "device_id": str(uuid.uuid4()),
        "events": [
            # Успех с явной пометкой повтора: эвристика такое повтором не считает
            # (предыдущей неудачи в визите нет), значит рост retry_after_fail
            # возможен только если `props.retry` доехал до БД.
            {"type": "recognition", "ts": ts(0),
             "props": {"recognized": True, "confidence": 0.93, "retry": True,
                       "attempt": 2, "ip": "203.0.113.9"}},
            {"type": "session_end", "ts": ts(1), "props": {"reason": "manual", "last_screen": "exhibit"}},
        ],
    })
    check(status == 202 and body == {"accepted": 2, "rejected": 0},
          "§1 батч с props.retry принят целиком", f"{status} {json.dumps(body)}")

    after = recognition_report()
    check(after["retry_after_fail"] == before["retry_after_fail"] + 1,
          "§1 props.retry сохранён в events.props и посчитан в retry_after_fail",
          f"{before['retry_after_fail']} → {after['retry_after_fail']}")
    check(after["total"] == before["total"] + 1 and after["success"] == before["success"] + 1,
          "§1 событие учтено как обычное распознавание",
          f"total {before['total']}→{after['total']}, success {before['success']}→{after['success']}")

    # Ключи вне контракта (`attempt`, `ip`) отброшены — иначе бы визит не сошёлся
    # по остальным метрикам; отдельная проверка §10 ТЗ от 03.08.2026 не ломается.
    status, body = request("POST", "/telemetry/events", {
        "session_id": str(uuid.uuid4()),
        "events": [{"type": "hall_view", "ts": ts(2), "props": {"retry": True}}],
    })
    check(status == 202 and body == {"accepted": 1, "rejected": 0},
          "§1 retry у чужого типа события не ломает приём", f"{status} {json.dumps(body)}")


def check_hall_catalog() -> None:
    """§2: результат прогона cleanup_hall_catalog.py на проде."""
    status, public = request("GET", "/halls?limit=100")
    assert status == 200, f"/halls → {status}"
    status, full = request("GET", "/halls?limit=100&include_service=true")
    assert status == 200, f"/halls?include_service=true → {status}"

    public_items = public["items"]
    full_items = full["items"]
    by_name = {h["name"].strip(): h for h in full_items}

    # Обе проверки по лестнице инвертированы 31.08.2026: п.5 от 28.07.2026 отменён
    # п. I-1 («добавить первым залом "Парадная лестница"»). Зал должен быть в
    # публичной выдаче, не быть служебным и стоять ПЕРВЫМ среди пронумерованных —
    # это и есть то, что музей увидит в приложении.
    numbered_public = [h for h in public_items if h.get("hall_number") is not None]
    check(bool(numbered_public) and numbered_public[0]["name"].strip() == STAIRCASE,
          "§2 «Парадная лестница» первая в GET /halls",
          ", ".join(h["name"] for h in public_items))

    stairs = by_name.get(STAIRCASE)
    check(stairs is not None and stairs.get("is_service") is False,
          "§2 «Парадная лестница» больше не служебная (is_service: false)",
          json.dumps(stairs, ensure_ascii=False))

    outside = by_name.get(OUTSIDE_EXPO)
    check(outside is not None and outside.get("hall_number") is None,
          "§2 у «Вне постоянной экспозиции» hall_number: null",
          json.dumps(outside, ensure_ascii=False))

    check(PLACEHOLDER not in by_name,
          "§2 тестового зала «Название потом придумаем» нет",
          json.dumps(by_name.get(PLACEHOLDER), ensure_ascii=False))

    # Ни один экспонат не должен остаться без витрины после переносов: сумма по
    # залам (включая служебные) обязана сойтись с общим числом экспонатов.
    _s, all_exhibits = request("GET", "/exhibits?limit=1")
    per_hall = 0
    for hall in full_items:
        _s, page = request("GET", f"/exhibits?limit=1&hall_id={hall['id']}")
        per_hall += page.get("total", 0)
    check(per_hall == all_exhibits.get("total"),
          "§2 экспонатов без зала не осталось (перенос из удалённых залов сошёлся)",
          f"по залам {per_hall}, всего {all_exhibits.get('total')}")

    # Гид больше не говорит «зал 99»: номера у служебных залов не отдаются наружу.
    numbers = [h.get("hall_number") for h in public_items]
    check(99 not in numbers, "§2 в публичной выдаче нет зала 99", str(numbers))


def main() -> int:
    check_retry_contract()
    check_hall_catalog()
    print("—" * 60)
    print(f"пройдено: {passed}, провалено: {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
