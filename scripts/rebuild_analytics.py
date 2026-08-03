#!/usr/bin/env python3
"""Ночной пересчёт аналитических агрегатов (§12 ТЗ 03.08.2026).

По ТЗ реалтайм не требуется — данные обновляются раз в сутки. До этого все шесть
отчётов считались на лету при каждом открытии дашборда: `analytics_routes` тянул
в память все события за период, `_top_items` делал запрос за именем на каждую
строку. На накопленной за сезон таблице `events` (она по требованию заказчика не
чистится) дашборд просто перестал бы открываться.

Что делает:
  • пересчитывает плоский суточный срез `analytics_daily`;
  • прогревает кэш отчётов `analytics_reports` с параметрами по умолчанию —
    именно их запрашивает дашборд.
Эндпоинты после этого только читают готовые агрегаты и отдают `updated_at`.

Идемпотентен: повторный запуск за ту же дату перезаписывает строки, а не
удваивает цифры.

    python scripts/rebuild_analytics.py                      # за всё время
    python scripts/rebuild_analytics.py --days 30            # за последние 30 суток
    python scripts/rebuild_analytics.py --from 2026-07-01 --to 2026-07-31

Пример записи в cron (ежедневно в 04:00):
    0 4 * * * cd /srv/faberge && .venv/bin/python scripts/rebuild_analytics.py --days 2

Почему cron, а не APScheduler в приложении: бэкенд ездит в Yandex Cloud
Functions, где «постоянно живущего» процесса с планировщиком нет — задача
вешается на Cloud-триггер по расписанию, вызывающий этот же скрипт.

Тот же пересчёт руками, без ожидания ночи: POST /admin/analytics/rebuild.

Env: DATABASE_URL (как у остальных скриптов), опционально DB_SSL_ROOT_CERT.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import crud  # noqa: E402
from app.db import SessionLocal  # noqa: E402


async def _run(dfrom: date | None, dto: date | None) -> None:
    async with SessionLocal() as session:
        result = await crud.rebuild_analytics(session, dfrom, dto)
    print(f"суток в срезе:   {result.daily_days}")
    print(f"строк в срезе:   {result.daily_rows}")
    print(f"отчётов прогрето: {len(result.rebuilt_reports)} ({', '.join(result.rebuilt_reports)})")
    print(f"updated_at:      {result.updated_at.isoformat()}")


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Rebuild Faberge analytics aggregates.")
    parser.add_argument("--from", dest="dfrom", type=_parse_date, help="начало периода (YYYY-MM-DD)")
    parser.add_argument("--to", dest="dto", type=_parse_date, help="конец периода включительно (YYYY-MM-DD)")
    parser.add_argument("--days", type=int, help="последние N суток, включая сегодня (вместо --from/--to)")
    args = parser.parse_args()

    start, end = args.dfrom, args.dto
    if args.days:
        end = date.today()
        start = end - timedelta(days=args.days - 1)

    try:
        asyncio.run(_run(start, end))
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
