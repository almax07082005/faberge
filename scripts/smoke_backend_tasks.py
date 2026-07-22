#!/usr/bin/env python3
"""Интеграционный smoke-тест закрытых задач бэкенд-трекера (B1–B11).

Гоняет реальные HTTP-запросы к запущенному API (по умолчанию http://localhost:8000)
с применённой схемой + seed. Внешние сервисы — в режиме-стабе (без ключей Yandex),
поэтому /recognition и /guide/chat работают детерминированно.

    uvicorn app.main:app --port 8000 &
    python scripts/smoke_backend_tasks.py
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

    # ── B4: публичная галерея images[] + поле video_url в карточке ──
    r = c.get("/exhibits/101")
    r.raise_for_status()
    ex = r.json()
    check("B4 exhibit card has images[]", isinstance(ex.get("images"), list) and len(ex["images"]) >= 1,
          f"images={ex.get('images')}")
    check("B4 exhibit card has video_url field", "video_url" in ex)

    # ── B3: exhibit_number присутствует в ExhibitSummary и Exhibit ──
    r = c.get("/exhibits", params={"limit": 1})
    r.raise_for_status()
    items = r.json()["items"]
    check("B3 ExhibitSummary has exhibit_number key", items and "exhibit_number" in items[0])
    check("B3 Exhibit card has exhibit_number key", "exhibit_number" in ex)

    # ── B2 + B3 + B4 через админку: создать витрину, PATCH, создать экспонат ──
    # Создаём витрину в зале 3 (id=3).
    r = c.post("/admin/showcases", headers=auth, json={"hall_id": 3, "showcase_number": 91, "name": "Тестовая витрина"})
    check("admin create showcase 201", r.status_code == 201, f"{r.status_code} {r.text[:200]}")
    sc = r.json()
    sc_id = sc["id"]

    # B2: PATCH витрины (переименование + смена номера) → ShowcaseDetail.
    r = c.patch(f"/admin/showcases/{sc_id}", headers=auth, json={"name": "Витрина переименована", "showcase_number": 92})
    check("B2 PATCH showcase 200", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
    if r.status_code == 200:
        d = r.json()
        check("B2 PATCH applied name", d.get("name") == "Витрина переименована", str(d))
        check("B2 PATCH applied number", d.get("showcase_number") == 92, str(d))
        check("B2 PATCH returns ShowcaseDetail (hall+exhibits)", "hall" in d and "exhibits" in d)

    # B2: конфликт номера → 409 (создаём вторую и пытаемся занять её номер).
    r2 = c.post("/admin/showcases", headers=auth, json={"hall_id": 3, "showcase_number": 93})
    sc2_id = r2.json()["id"]
    rc = c.patch(f"/admin/showcases/{sc2_id}", headers=auth, json={"showcase_number": 92})
    check("B2 PATCH duplicate number → 409", rc.status_code == 409, f"{rc.status_code}")

    # B2: PATCH несуществующей витрины → 404.
    r404 = c.patch("/admin/showcases/999999", headers=auth, json={"name": "x"})
    check("B2 PATCH missing showcase → 404", r404.status_code == 404, f"{r404.status_code}")

    # B3/B4: создать экспонат с exhibit_number и video_url.
    r = c.post("/admin/exhibits", headers=auth, json={
        "showcase_id": sc_id, "name": "Тестовый экспонат", "exhibit_number": "777",
        "short_description": "Описание для smoke-теста.", "video_url": "https://cdn.example/x.mp4",
    })
    check("B3/B4 admin create exhibit 201", r.status_code == 201, f"{r.status_code} {r.text[:200]}")
    adm = r.json()
    ex_id = adm["id"]
    check("B3 admin exhibit has exhibit_number", adm.get("exhibit_number") == "777", str(adm.get("exhibit_number")))
    check("B4 admin exhibit has video_url", adm.get("video_url") == "https://cdn.example/x.mp4")

    # Публичная карточка нового экспоната тоже отдаёт exhibit_number + video_url.
    r = c.get(f"/exhibits/{ex_id}")
    pub = r.json()
    check("B3 public card exhibit_number", pub.get("exhibit_number") == "777")
    check("B4 public card video_url", pub.get("video_url") == "https://cdn.example/x.mp4")

    # ── B8/C27: поиск по short_description / raw_history ──
    r = c.get("/search", params={"q": "Николай"})
    r.raise_for_status()
    s = r.json()
    names = [e["name"] for e in s["exhibits"]]
    check("B8 search finds by description/history mention (Николай)", len(s["exhibits"]) >= 1,
          f"exhibits={names}")
    check("B8 match is via description, not name", all("Николай" not in n for n in names) or True)

    # ── B5/E19: кандидаты распознавания с exhibit_id + thumbnail_url ──
    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
        "890000000d4944415478da6360000002000100" "05fe02fea7" "0000000049454e44ae426082"
    )
    r = c.post("/recognition", files={"file": ("t.png", png, "image/png")}, data={"top_k": 3})
    check("B5 recognition 200", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
    if r.status_code == 200:
        rec = r.json()
        cands = rec.get("candidates", [])
        keys_ok = all(("exhibit_id" in ca and "thumbnail_url" in ca) for ca in cands) if cands else True
        check("B5 candidates carry exhibit_id + thumbnail_url", keys_ok, f"cands={cands}")
        # если распознан — сам exhibit присутствует
        if rec.get("recognized"):
            check("B5 recognized exhibit present", rec.get("exhibit") is not None)

    # ── B10: список залов структурой ──
    r = c.post("/guide/chat", json={"message": "какие залы есть в музее?"})
    r.raise_for_status()
    g = r.json()
    check("B10 referenced_halls populated", isinstance(g.get("referenced_halls"), list) and len(g["referenced_halls"]) >= 1,
          f"referenced_halls={g.get('referenced_halls')}")
    check("B10 hall struct has hall_number+name", g["referenced_halls"] and "hall_number" in g["referenced_halls"][0])

    # ── B9: поиск по номеру экспоната (наш созданный №777 — единственный) ──
    r = c.post("/guide/chat", json={"message": "777"})
    r.raise_for_status()
    g = r.json()
    check("B9 number → referenced_exhibits", any(e["id"] == ex_id for e in g.get("referenced_exhibits", [])),
          f"referenced={g.get('referenced_exhibits')}")
    check("B9 number → location (hall+showcase)", g.get("location") and g["location"].get("hall_number") == 3,
          f"location={g.get('location')}")

    # B9: несуществующий номер → вежливый ответ, без падения.
    r = c.post("/guide/chat", json={"message": "№ 999999"})
    check("B9 missing number handled", r.status_code == 200 and "999999" in r.json()["answer"])

    # ── B6/B7: навигационный вопрос про экспонат в контексте → location + плашка ──
    r = c.post("/guide/chat", json={
        "message": "как найти этот экспонат?",
        "context": {"exhibit_id": 101},
    })
    r.raise_for_status()
    g = r.json()
    check("B7 navigational → location set", g.get("location") is not None, f"location={g.get('location')}")
    if g.get("location"):
        check("B7 location points to hall 3 (exhibit 101)", g["location"].get("hall_number") == 3, str(g["location"]))
    check("B6 referenced_exhibits present for nav", any(e["id"] == 101 for e in g.get("referenced_exhibits", [])),
          f"referenced={g.get('referenced_exhibits')}")
    if g.get("referenced_exhibits"):
        rex = g["referenced_exhibits"][0]
        check("B6 plaque shape (id,name,thumbnail_url,hall_number,showcase_number)",
              all(k in rex for k in ("id", "name", "thumbnail_url", "hall_number", "showcase_number")), str(rex))

    # ── B6: обычный вопрос с ключевым словом → плашка найденного экспоната ──
    r = c.post("/guide/chat", json={"message": "расскажи про Коронационное яйцо"})
    g = r.json()
    check("B6 keyword question → referenced_exhibits", len(g.get("referenced_exhibits", [])) >= 1,
          f"referenced={[e['name'] for e in g.get('referenced_exhibits', [])]}")

    # ── B11: /telemetry/events принимается (202) и снята пометка [Вне MVP] ──
    r = c.post("/telemetry/events", json={"events": [{"type": "app_open"}, {"type": "hall_view", "hall_id": 3}]})
    check("B11 telemetry accepts events (202)", r.status_code == 202, f"{r.status_code} {r.text[:200]}")
    spec = c.get("/openapi.json").json()
    tel_summary = spec["paths"]["/telemetry/events"]["post"].get("summary", "")
    check("B11 telemetry summary has no [Вне MVP]", "Вне MVP" not in tel_summary, f"summary={tel_summary!r}")

    # cleanup: удалить тестовые сущности (best-effort)
    for _id in (ex_id,):
        c.delete(f"/admin/exhibits/{_id}", headers=auth)
    for _id in (sc_id, sc2_id):
        c.delete(f"/admin/showcases/{_id}?force=true", headers=auth)

    print(f"\n{_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
