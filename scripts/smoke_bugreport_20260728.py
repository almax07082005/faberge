#!/usr/bin/env python3
"""Интеграционный smoke-тест по баг-репорту заказчика от 28.07.2026 + C25.

Проверяет Definition of Done тех пунктов, которые видны через HTTP и не требуют
внешних сервисов Yandex (без ключей они работают в режиме-стабе):

  C25  — «А какие вообще есть залы» → структурированный список + referenced_halls;
  п.2  — POST /speech с «Пётр I …» уходит в синтез с числами прописью;
  п.3  — контекст зала не «залипает»: reset_context / context:{} сбрасывают его,
         а без поля контекст по-прежнему поднимается из сессии (не регрессить C24);
  п.4  — витрины с номерами, группа «не в витринах», группировка «витрина → её
         экспонаты» собирается из GET /halls/{id}/exhibits;
  п.5  — служебные залы скрыты из GET /halls, зал без номера отдаётся с
         hall_number = null, гид считает только пронумерованные залы.

Гоняет реальные HTTP-запросы к запущенному API с применённой схемой + seed:

    uvicorn app.main:app --port 8000 &
    python scripts/smoke_bugreport_20260728.py

Тест создаёт свои временные зал/витрины/экспонаты и убирает их за собой.
"""
from __future__ import annotations

import os
import sys

import httpx

BASE = os.environ.get("BASE_URL", "http://localhost:8000")
ADMIN_TOKEN = os.environ.get("ADMIN_API_TOKEN", "dev-admin-token")

_passed = 0
_failed = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"PASS {name}")
    else:
        _failed += 1
        print(f"FAIL {name}  {detail}")


def main() -> int:
    c = httpx.Client(base_url=BASE, timeout=30.0)
    auth = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
    created_halls: list[int] = []
    created_showcases: list[int] = []
    created_exhibits: list[int] = []

    try:
        # ── C25: запрос списка залов со словами-вставками ──
        for message in ("А какие вообще есть залы", "сколько всего залов", "какие есть залы"):
            g = c.post("/guide/chat", json={"message": message}).json()
            check(
                f"C25 «{message}» → список залов",
                len(g.get("referenced_halls", [])) >= 1 and "В музее" in g.get("answer", ""),
                f"answer={g.get('answer', '')[:120]!r}",
            )
        # Негативный кейс: вопрос про содержимое зала не должен выдавать дамп залов.
        g = c.post("/guide/chat", json={"message": "какие экспонаты в зале 4"}).json()
        check("C25 вопрос про содержимое зала не выдаёт список залов",
              not g.get("referenced_halls"), f"halls={g.get('referenced_halls')}")

        # ── п.2: озвучка числительных ──
        plain = c.post("/speech", json={"text": "Пётр I основал Санкт-Петербург"}).json()
        check("п.2 /speech 200 и вернул audio_url", bool(plain.get("audio_url")), str(plain)[:200])
        # «Пётр I» (6 симв.) → «Пётр Первый» (11): в синтез уходит уже развёрнутый текст,
        # поэтому characters больше длины исходной строки.
        check("п.2 в синтез ушёл текст с числительным прописью",
              (plain.get("characters") or 0) > len("Пётр I основал Санкт-Петербург"),
              f"characters={plain.get('characters')}")
        no_num = c.post("/speech", json={"text": "Яйцо украшено жемчугом"}).json()
        check("п.2 текст без чисел не удлиняется",
              no_num.get("characters") == len("Яйцо украшено жемчугом"), f"characters={no_num.get('characters')}")

        # ── п.5: каталог залов ──
        halls = c.get("/halls", params={"limit": 100}).json()
        with_service = c.get("/halls", params={"limit": 100, "include_service": True}).json()
        check("п.5 GET /halls скрывает служебные записи",
              with_service["total"] >= halls["total"], f"{halls['total']} vs {with_service['total']}")
        check("п.5 в публичной выдаче нет служебных залов",
              all(not h.get("is_service") for h in halls["items"]))
        check("п.5 Парадной лестницы нет в списке залов",
              not any((h.get("name") or "").strip().lower() == "парадная лестница" for h in halls["items"]),
              str([h.get("name") for h in halls["items"]]))

        # Зал без номера отдаётся с hall_number = null и не ломает подписи гида.
        hall = c.post("/admin/halls", headers=auth,
                      json={"name": "Тест: зал без номера", "hall_number": None}).json()
        created_halls.append(hall["id"])
        check("п.5 зал можно завести без номера", hall.get("hall_number") is None, str(hall)[:200])
        listed = c.get("/halls", params={"limit": 100}).json()["items"]
        check("п.5 зал без номера присутствует в GET /halls",
              any(h["id"] == hall["id"] and h["hall_number"] is None for h in listed))
        g = c.post("/guide/chat", json={"message": "какие есть залы"}).json()
        check("п.5 гид не пишет «зал None»", "None" not in g.get("answer", ""), g.get("answer", "")[:200])
        numbered = [h for h in listed if h["hall_number"] is not None]
        check("п.5 счётчик гида = число пронумерованных залов",
              f"В музее {len(numbered)} " in g.get("answer", ""),
              f"пронумерованных={len(numbered)}, answer={g.get('answer', '')[:80]!r}")

        # ── п.4: витрины с номерами + группа «не в витринах» ──
        sc1 = c.post("/admin/showcases", headers=auth,
                     json={"hall_id": hall["id"], "showcase_number": 1, "name": "Тест: витрина 1"}).json()
        created_showcases.append(sc1["id"])
        sc_none = c.post("/admin/showcases", headers=auth,
                         json={"hall_id": hall["id"], "showcase_number": None, "name": "Не в витринах"}).json()
        created_showcases.append(sc_none["id"])
        check("п.4 группа «не в витринах» создаётся (showcase_number = null)",
              sc_none.get("showcase_number") is None, str(sc_none)[:200])
        dup = c.post("/admin/showcases", headers=auth,
                     json={"hall_id": hall["id"], "showcase_number": None, "name": "Дубль"})
        check("п.4 вторая группа «не в витринах» в зале → 409", dup.status_code == 409, str(dup.status_code))

        # Номера заведомо свободные и числовые — гид ищет экспонат по реплике-номеру (B9).
        for sc_id, number, name in ((sc1["id"], "9001", "Тест: в витрине"), (sc_none["id"], "9002", "Тест: вне витрин")):
            ex = c.post("/admin/exhibits", headers=auth,
                        json={"showcase_id": sc_id, "name": name, "exhibit_number": number}).json()
            created_exhibits.append(ex["id"])

        showcases = c.get(f"/halls/{hall['id']}/showcases").json()["items"]
        check("п.4 витрины зала отдаются с номерами", [s["showcase_number"] for s in showcases] == [1, None],
              str([s["showcase_number"] for s in showcases]))
        exhibits = c.get(f"/halls/{hall['id']}/exhibits").json()["items"]
        check("п.4 экспонаты зала несут showcase_number для группировки",
              all("showcase_number" in e for e in exhibits) and {e["showcase_number"] for e in exhibits} == {1, None},
              str([(e["exhibit_number"], e["showcase_number"]) for e in exhibits]))
        g = c.post("/guide/chat", json={"message": "9002"}).json()
        check("п.4 гид называет местоположение экспоната вне витрин",
              "вне витрин" in g.get("answer", ""), g.get("answer", "")[:200])
        g = c.post("/guide/chat", json={"message": "9001"}).json()
        check("п.4 гид называет номер витрины для экспоната в витрине",
              "витрина 1" in g.get("answer", ""), g.get("answer", "")[:200])

        # ── п.3: контекст зала не «залипает» ──
        hall_id = next(h["id"] for h in listed if h["hall_number"] is not None)
        first = c.post("/guide/chat", json={"message": "что тут есть", "context": {"hall_id": hall_id}}).json()
        sid = first["session_id"]
        keep = c.post("/guide/chat", json={"message": "а подробнее?", "session_id": sid}).json()
        check("п.3 без поля context он по-прежнему поднимается из сессии (не регрессить C24)",
              (keep.get("context") or {}).get("hall_id") == hall_id, str(keep.get("context")))
        reset = c.post("/guide/chat", json={"message": "Пётр I", "session_id": sid, "reset_context": True}).json()
        check("п.3 reset_context: true сбрасывает контекст", reset.get("context") is None, str(reset.get("context")))
        after = c.post("/guide/chat", json={"message": "и что дальше", "session_id": sid}).json()
        check("п.3 сброс сохраняется в сессии (зал не воскресает)",
              after.get("context") is None, str(after.get("context")))
        second = c.post("/guide/chat", json={"message": "что тут есть", "context": {"hall_id": hall_id}}).json()
        blank = c.post("/guide/chat",
                       json={"message": "Пётр I", "session_id": second["session_id"], "context": {}}).json()
        check("п.3 явный context: {} тоже сбрасывает", blank.get("context") is None, str(blank.get("context")))

        # ── п.1: ответ распознавания всегда содержит поле candidates ──
        png = bytes.fromhex(
            "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000a49444154"
            "789c63000100000500010d0a2db40000000049454e44ae426082"
        )
        r = c.post("/recognition", files={"file": ("t.png", png, "image/png")})
        check("п.1 /recognition 200", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
        if r.status_code == 200:
            body = r.json()
            check("п.1 в ответе есть список candidates (E19)", isinstance(body.get("candidates"), list),
                  str(body)[:200])
    finally:
        for _id in created_exhibits:
            c.delete(f"/admin/exhibits/{_id}", headers=auth)
        for _id in created_showcases:
            c.delete(f"/admin/showcases/{_id}?force=true", headers=auth)
        for _id in created_halls:
            c.delete(f"/admin/halls/{_id}?force=true", headers=auth)

    print(f"\n{_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
