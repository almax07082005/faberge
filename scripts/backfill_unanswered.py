#!/usr/bin/env python3
"""Разовый бэкфилл признака «гид не смог ответить» (§4 ТЗ 03.08.2026).

Начиная с 03.08.2026 признак `answered` и причина `fail_reason` проставляются в
момент генерации ответа (app/routers/guide.py). У сообщений, накопленных ДО
миграции, признака нет (`answered IS NULL`), и восстановить его можно только
эвристикой по тексту ответа — этим и занимается скрипт.

Почему это отдельный скрипт, а не рантайм: эвристика по фразам-маркерам грубее
разметки в момент генерации (она не знает, была ли справка и нашёлся ли
экспонат), и запускать её на каждый запрос нельзя — иначе в отчёте перемешаются
достоверные и восстановленные признаки. Здесь она отрабатывает один раз, по
уже накопленным данным.

Как размечает:
  • ответ содержит маркер отказа (app/services/guide_intel.is_refusal) →
    answered = false;
  • причина зависит от того, отказ это ЦЕЛИКОМ или оговорка в содержательном
    ответе (app/services/guide_intel.is_hard_refusal): отказ целиком →
    `no_context`, если у сессии не было контекста экспоната/зала, иначе
    `llm_refusal`; оговорка → `llm_hedge`;
  • иначе → answered = true.

Разделение причин обязательно, а не «для красоты отчёта»: `llm_refusal` и
`no_context` кормят глобальную память отказов (app/crud.py:
exhibit_refused_questions), то есть вопрос с такой причиной перестаёт
предлагаться ВСЕМ посетителям экспоната. Разметить широким признаком весь
исторический хвост — значит одним прогоном спрятать из подсказок сотни
нормальных вопросов, у которых модель просто оговорилась в середине ответа.
Признак пишется на ОБЕ строки пары (вопрос посетителя и ответ гида) — так же,
как это делает рантайм.

Идемпотентен: трогает только строки с `answered IS NULL`. По умолчанию — сухой
прогон, который печатает план.

    python scripts/backfill_unanswered.py            # показать план
    python scripts/backfill_unanswered.py --apply    # применить

Требует применённой миграции db/migrations/2026-08-03_analytics.sql.
Env: DATABASE_URL (как у остальных скриптов), опционально DB_SSL_ROOT_CERT.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import ssl
import sys
from pathlib import Path

import asyncpg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.guide_intel import is_hard_refusal, is_refusal  # noqa: E402


def _dsn() -> str:
    url = os.environ.get("DATABASE_URL", "postgresql+asyncpg://faberge:faberge@localhost:5432/faberge")
    return url.replace("+asyncpg", "")


# Пары «вопрос → ответ» внутри сессии: ответ гида — ближайшее сообщение
# assistant после реплики пользователя.
_PAIRS_SQL = """
SELECT q.id            AS question_id,
       a.id            AS answer_id,
       a.content       AS answer,
       s.context       AS context
FROM guide_messages q
JOIN guide_sessions s ON s.id = q.session_id
JOIN LATERAL (
    SELECT m.id, m.content
    FROM guide_messages m
    WHERE m.session_id = q.session_id
      AND m.role = 'assistant'
      AND m.id > q.id
    ORDER BY m.id
    LIMIT 1
) a ON true
WHERE q.role = 'user' AND q.answered IS NULL
ORDER BY q.id
"""


async def _run(apply: bool) -> None:
    ca = os.environ.get("DB_SSL_ROOT_CERT")
    conn = await asyncpg.connect(_dsn(), ssl=ssl.create_default_context(cafile=ca) if ca else None)
    try:
        rows = await conn.fetch(_PAIRS_SQL)
        updates: list[tuple[int, int, bool, str | None]] = []
        for row in rows:
            answer = row["answer"] or ""
            refused = is_refusal(answer)
            reason = None
            if refused:
                if is_hard_refusal(answer):
                    # Контекст сессии — единственный след того, была ли у гида справка.
                    has_context = bool(row["context"]) and row["context"] not in ("{}", "null")
                    reason = "llm_refusal" if has_context else "no_context"
                else:
                    # Ответ по существу, в котором модель оговорилась. В отчёт —
                    # да, в память отказов — нет (см. шапку файла).
                    reason = "llm_hedge"
            updates.append((row["question_id"], row["answer_id"], not refused, reason))

        unanswered = sum(1 for *_rest, answered, _reason in updates if not answered)
        hedges = sum(1 for *_rest, reason in updates if reason == "llm_hedge")
        print(f"пар «вопрос — ответ» без признака: {len(updates)}")
        print(f"будет размечено как отказ: {unanswered}")
        print(f"  из них оговорок (llm_hedge, в память отказов НЕ идут): {hedges}")
        if not apply:
            print("сухой прогон — ничего не изменено (--apply, чтобы применить)")
            return

        async with conn.transaction():
            for question_id, answer_id, answered, reason in updates:
                await conn.execute(
                    "UPDATE guide_messages SET answered = $1, fail_reason = $2 WHERE id = ANY($3::bigint[])",
                    answered, reason, [question_id, answer_id],
                )
        print(f"обновлено строк: {len(updates) * 2}")
    finally:
        await conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill guide_messages.answered / fail_reason.")
    parser.add_argument("--apply", action="store_true", help="применить изменения (иначе сухой прогон)")
    args = parser.parse_args()
    try:
        asyncio.run(_run(args.apply))
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
