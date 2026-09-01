#!/usr/bin/env python3
"""Чистка каталога залов по баг-репорту заказчика от 28.07.2026 (п.5).

Что делает:

  1. «Парадная лестница» — НИЧЕГО, если не попросить явно. Пункт 5 от 28.07.2026
     («убрать Парадную лестницу из списка залов») ОТМЕНЁН музеем 31.08.2026:
     п. I-1 нового баг-репорта — «Во вкладке "Основная экспозиция" добавить первым
     залом "Парадная лестница" с рассказом о дворце и истории создания Музея».
     Разбор обоих решений — docs/staircase-hall-decision.md.
     Ключ ``--hide-staircase`` возвращает старое поведение (пометить служебной,
     ``is_service = true``), ``--delete-staircase`` — удалить запись совсем
     (привязанные экспонаты перед этим переносятся, см. п.3).
  2. «Вне постоянной экспозиции» (зал № 99) — остаётся, но БЕЗ номера:
     ``hall_number = NULL``. Подписи и ответы гида перестают говорить «зал 99».
  3. «Название потом придумаем» (зал № 100) — тестовая запись, удаляется.
     Если к ней привязаны экспонаты, они сначала переносятся в зал «Вне
     постоянной экспозиции», в группу «не в витринах» (``showcase_number IS
     NULL``) — чтобы ни один экспонат не остался привязан к удалённой записи.

Почему шаг 1 стал опт-ин, а не удалён вместе с пунктом
------------------------------------------------------
Пункты 2 и 3 живые, и ради них скрипт зовут до сих пор (README, раздел «Каталог
залов и витрин»). Пока шаг с лестницей оставался поведением по умолчанию, любой
такой прогон третьим действием молча возвращал ``is_service = true`` и отменял
решение музея от 31.08.2026 — самый вероятный путь регресса по п. I-1. Удалять
шаг целиком тоже нельзя: механизм скрытия остаётся в контракте, и он ещё
понадобится для настоящих служебных записей.

Требует применённой миграции db/migrations/2026-07-29_bugreport_catalog.sql
(nullable hall_number/showcase_number, колонка is_service).

Скрипт идемпотентен: повторный запуск ничего не меняет. По умолчанию — сухой
прогон, который только печатает план.

    python scripts/cleanup_hall_catalog.py               # показать план
    python scripts/cleanup_hall_catalog.py --apply       # применить (лестницу НЕ трогает)
    python scripts/cleanup_hall_catalog.py --apply --hide-staircase
    python scripts/cleanup_hall_catalog.py --apply --delete-staircase

Env: DATABASE_URL (как у остальных скриптов), опционально DB_SSL_ROOT_CERT.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import ssl
import sys

import asyncpg

# Записи из баг-репорта. Ищем по названию (устойчиво к смене номера), номер —
# запасной признак: в проде зал мог быть заведён с другим названием.
STAIRCASE = ("Парадная лестница", 1)
OUTSIDE_EXPO = ("Вне постоянной экспозиции", 99)
PLACEHOLDER = ("Название потом придумаем", 100)


def _dsn() -> str:
    url = os.environ.get("DATABASE_URL", "postgresql+asyncpg://faberge:faberge@localhost:5432/faberge")
    return url.replace("+asyncpg", "")  # asyncpg.connect ожидает driver-agnostic DSN


async def _find_hall(conn: asyncpg.Connection, name: str, number: int):
    """Зал по названию (регистр/пробелы не важны), иначе по номеру."""
    row = await conn.fetchrow(
        "SELECT id, hall_number, name, is_service FROM halls WHERE lower(btrim(name)) = lower($1)", name
    )
    if row is None:
        row = await conn.fetchrow(
            "SELECT id, hall_number, name, is_service FROM halls WHERE hall_number = $1", number
        )
    return row


async def _exhibit_count(conn: asyncpg.Connection, hall_id: int) -> int:
    return await conn.fetchval(
        "SELECT count(*) FROM exhibits e JOIN showcases s ON s.id = e.showcase_id WHERE s.hall_id = $1",
        hall_id,
    )


async def _unnumbered_showcase(conn: asyncpg.Connection, hall_id: int, apply: bool) -> int | None:
    """id группы «не в витринах» в зале; создаёт её при необходимости."""
    sid = await conn.fetchval(
        "SELECT id FROM showcases WHERE hall_id = $1 AND showcase_number IS NULL", hall_id
    )
    if sid is not None or not apply:
        return sid
    return await conn.fetchval(
        "INSERT INTO showcases (hall_id, showcase_number, name) VALUES ($1, NULL, $2) RETURNING id",
        hall_id, "Не в витринах",
    )


async def _move_exhibits(conn: asyncpg.Connection, src_hall: int, dst_hall: int, apply: bool) -> int:
    """Перенести все экспонаты зала в группу «не в витринах» другого зала."""
    count = await _exhibit_count(conn, src_hall)
    if count == 0:
        return 0
    dst_showcase = await _unnumbered_showcase(conn, dst_hall, apply)
    if not apply:
        return count
    await conn.execute(
        "UPDATE exhibits SET showcase_id = $1 "
        "WHERE showcase_id IN (SELECT id FROM showcases WHERE hall_id = $2)",
        dst_showcase, src_hall,
    )
    return count


async def _run(apply: bool, delete_staircase: bool, hide_staircase: bool = False) -> int:
    ca = os.environ.get("DB_SSL_ROOT_CERT")
    conn = await asyncpg.connect(_dsn(), ssl=ssl.create_default_context(cafile=ca) if ca else None)
    plan: list[str] = []
    try:
        async with conn.transaction():
            outside = await _find_hall(conn, *OUTSIDE_EXPO)

            # ── 1. Парадная лестница ──────────────────────────────────────────
            stairs = await _find_hall(conn, *STAIRCASE)
            if stairs is None:
                plan.append(f"— «{STAIRCASE[0]}»: записи нет, пропускаю")
            elif delete_staircase:
                moved = 0
                if outside is not None and outside["id"] != stairs["id"]:
                    moved = await _move_exhibits(conn, stairs["id"], outside["id"], apply)
                elif await _exhibit_count(conn, stairs["id"]):
                    print(
                        f"ОШИБКА: к «{stairs['name']}» привязаны экспонаты, а зала "
                        f"«{OUTSIDE_EXPO[0]}» для переноса нет. Удаление отменено.",
                        file=sys.stderr,
                    )
                    return 1
                if apply:
                    await conn.execute("DELETE FROM halls WHERE id = $1", stairs["id"])
                plan.append(
                    f"— «{stairs['name']}» (id={stairs['id']}): УДАЛИТЬ"
                    + (f", предварительно перенести экспонатов: {moved}" if moved else "")
                )
            elif not hide_staircase:
                # Дефолт с 31.08.2026: зал публичный по решению музея (п. I-1).
                # Молча ставить флаг обратно нельзя — прогон ради пунктов 2 и 3
                # отменял бы решение заказчика (см. шапку).
                plan.append(
                    f"— «{stairs['name']}»: пропускаю, с 31.08.2026 зал публичный по решению "
                    f"музея (см. docs/staircase-hall-decision.md). Спрятать всё же нужно — "
                    f"добавьте --hide-staircase"
                )
            elif stairs["is_service"]:
                plan.append(f"— «{stairs['name']}»: уже служебная, ничего не делаю")
            else:
                if apply:
                    await conn.execute("UPDATE halls SET is_service = true WHERE id = $1", stairs["id"])
                plan.append(
                    f"— «{stairs['name']}» (id={stairs['id']}): пометить служебной "
                    f"(is_service = true) → исчезнет из GET /halls, карты и ответов гида"
                )

            # ── 2. Зал № 99 «Вне постоянной экспозиции» — без номера ───────────
            if outside is None:
                plan.append(f"— «{OUTSIDE_EXPO[0]}»: записи нет, пропускаю")
            elif outside["hall_number"] is None:
                plan.append(f"— «{outside['name']}»: номер уже пуст, ничего не делаю")
            else:
                if apply:
                    await conn.execute("UPDATE halls SET hall_number = NULL WHERE id = $1", outside["id"])
                plan.append(
                    f"— «{outside['name']}» (id={outside['id']}): убрать номер "
                    f"{outside['hall_number']} → hall_number = NULL"
                )

            # ── 3. Зал № 100 «Название потом придумаем» — удалить ──────────────
            placeholder = await _find_hall(conn, *PLACEHOLDER)
            if placeholder is None:
                plan.append(f"— «{PLACEHOLDER[0]}»: записи нет, пропускаю")
            else:
                moved = 0
                if outside is not None and outside["id"] != placeholder["id"]:
                    moved = await _move_exhibits(conn, placeholder["id"], outside["id"], apply)
                elif await _exhibit_count(conn, placeholder["id"]):
                    print(
                        f"ОШИБКА: к «{placeholder['name']}» привязаны экспонаты, а зала "
                        f"«{OUTSIDE_EXPO[0]}» для переноса нет. Удаление отменено.",
                        file=sys.stderr,
                    )
                    return 1
                if apply:
                    await conn.execute("DELETE FROM halls WHERE id = $1", placeholder["id"])
                plan.append(
                    f"— «{placeholder['name']}» (id={placeholder['id']}): УДАЛИТЬ"
                    + (f", предварительно перенести экспонатов: {moved}" if moved else "")
                )

            if not apply:
                raise _DryRun()  # откатываем транзакцию сухого прогона
    except _DryRun:
        pass
    finally:
        visible = await conn.fetchval("SELECT count(*) FROM halls WHERE is_service = false")
        orphans = await conn.fetchval("SELECT count(*) FROM exhibits WHERE showcase_id IS NULL")
        await conn.close()

    print("План" if not apply else "Применено")
    for line in plan:
        print(" ", line)
    print(f"\nЗалов в публичной выдаче сейчас: {visible}")
    print(f"Экспонатов без витрины: {orphans}")
    if not apply:
        print("\nЭто сухой прогон, изменения откачены. Повторите с --apply.")
    return 0


class _DryRun(Exception):
    """Служебное исключение: откатить транзакцию сухого прогона."""


def build_parser() -> argparse.ArgumentParser:
    """Парсер отдельной функцией — чтобы дефолты проверялись тестом без БД.

    Проверяется ровно одно и очень конкретное свойство: пустой argv НЕ включает
    шаг с лестницей. Это и есть защита от регресса по п. I-1.
    """
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="применить изменения (без ключа — сухой прогон)")
    parser.add_argument(
        "--hide-staircase", action="store_true",
        help="пометить «Парадную лестницу» служебной (is_service = true) — поведение до 31.08.2026; "
             "по умолчанию зал не трогаем, музей отменил п.5 от 28.07.2026",
    )
    parser.add_argument(
        "--delete-staircase", action="store_true",
        help="удалить «Парадную лестницу» вместо пометки служебной (экспонаты переносятся)",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return asyncio.run(_run(args.apply, args.delete_staircase, args.hide_staircase))


if __name__ == "__main__":
    sys.exit(main())
