#!/usr/bin/env python3
"""Разовый прогрев кэша вопросов-подсказок по всему каталогу (просьба заказчика 26.08.2026).

Вопросы-подсказки («Кому подарили это яйцо?») зависят только от карточки экспоната, и с
26.08.2026 они хранятся в БД (таблица ``exhibit_questions``, см. ``app/services/guide_questions``).
Кэш наполняется сам по мере обращений посетителей — этот скрипт наполняет его заранее, чтобы
первый посетитель у витрины не ждал лишний вызов LLM, а музей мог посмотреть вопросы по всему
каталогу до открытия.

Идемпотентен, и это главное свойство: карточка со свежей записью пропускается БЕЗ обращения к
LLM. «Свежая» — значит хэш исходного текста совпадает; поправили описание в админке — карточка
снова попадёт в план. Повторный прогон сразу после успешного печатает «сгенерировано: 0».

Сухой прогон по умолчанию: без ``--apply`` скрипт только считает, сколько карточек требует
генерации, и не тратит ни одного токена. Прогрев всего каталога — это один вызов LLM на
карточку (на проде это 1200+ вызовов), поэтому решение «жечь токены» принимает человек, а не
ключ по умолчанию.

    DATABASE_URL=... YANDEX_API_KEY=... YANDEX_FOLDER_ID=... \\
        python scripts/warm_guide_questions.py                    # сухой прогон, весь каталог
    ... --limit 20                                                # проба на 20 карточках
    ... --apply                                                   # прогреть весь каталог
    ... --ids 144,483 --apply                                     # точечно
    ... --force --apply                                           # перегенерировать всё заново
    ... --delay 0.5 --apply                                       # мягче к лимитам YandexGPT

``--force`` нужен в двух случаях: менялся промпт генерации вопросов (``llm._yandexgpt_questions``) или
подняли ``GUIDE_QUESTIONS_CACHE_SIZE`` и хочется расширить уже записанные наборы. Изменения описаний
ловятся хэшем и без него.

Сбой LLM на карточке не останавливает прогон: карточка уходит в «не удалось» и остаётся без
записи — следующий прогон возьмёт её снова. Так одна битая карточка не съедает уже проделанную
работу.

То же самое порциями и без шелла — ``POST /admin/guide/questions/warm`` (по ``limit`` карточек
за вызов); покрытие каталога — ``GET /admin/guide/questions/status``.

Env: DATABASE_URL (как у остальных скриптов), опционально DB_SSL_ROOT_CERT. Без ключей
YandexGPT скрипт отработает на стабе (``llm._questions_stub``) — в БД лягут заглушечные
вопросы, поэтому на проде сначала проверьте ``GET /health`` → ``dependencies.llm``.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import crud  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.services import guide_questions  # noqa: E402


def _parse_ids(value: str) -> list[int]:
    return [int(part) for part in value.replace(" ", "").split(",") if part]


async def _run(args: argparse.Namespace) -> int:
    counts = {"generated": 0, "cached": 0, "failed": 0, "planned": 0}
    failed_ids: list[int] = []

    async with SessionLocal() as session:
        rows = await crud.exhibits_to_warm(
            session, language=args.language, limit=args.limit, ids=args.ids
        )
        total, cached_before = await crud.exhibit_questions_coverage(session, args.language)
        print(
            f"каталог: {total} карточек, с записью кэша: {cached_before}, "
            f"в этом прогоне: {len(rows)}"
        )
        if not settings.llm_configured:
            print("ВНИМАНИЕ: LLM не настроен — вопросы будут заглушечными (stub/heuristic).")

        for index, (ex, cached_row) in enumerate(rows, start=1):
            outcome, questions = await guide_questions.warm_exhibit(
                session, ex, cached_row, language=args.language,
                force=args.force, dry_run=not args.apply,
            )
            counts[outcome] += 1
            if outcome == "failed":
                failed_ids.append(ex.id)
            if args.verbose or outcome == "failed":
                mark = {"generated": "+", "cached": "=", "planned": "?", "failed": "!"}[outcome]
                print(f"  {mark} id={ex.id} {ex.name[:60]}" + (f" → {len(questions)} вопр." if questions else ""))
            # Пауза только после реальных вызовов LLM: пропуск свежей карточки в
            # облако не ходит, и растягивать на него прогон незачем.
            if outcome == "generated" and args.delay and index < len(rows):
                await asyncio.sleep(args.delay)

        _, cached_after = await crud.exhibit_questions_coverage(session, args.language)

    print("")
    if args.apply:
        print(f"сгенерировано:   {counts['generated']}")
    else:
        print(f"требует генерации: {counts['planned']}  (сухой прогон, LLM не вызывался)")
    print(f"уже было свежих: {counts['cached']}")
    print(f"не удалось:      {counts['failed']}" + (f" (id: {', '.join(map(str, failed_ids))})" if failed_ids else ""))
    print(f"покрытие кэшем:  {cached_after} из {total}")
    if not args.apply and counts["planned"]:
        print("\nЭто был сухой прогон. Чтобы прогреть, повторите с --apply.")
    return 1 if counts["failed"] else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Warm the Faberge guide suggested-questions cache.")
    parser.add_argument("--apply", action="store_true", help="действительно генерировать (по умолчанию сухой прогон)")
    parser.add_argument("--limit", type=int, help="обработать не больше N карточек")
    parser.add_argument("--ids", type=_parse_ids, help="только эти экспонаты, через запятую")
    parser.add_argument("--language", default="ru", help="язык вопросов (по умолчанию ru)")
    parser.add_argument("--force", action="store_true", help="перегенерировать даже свежие записи")
    parser.add_argument("--delay", type=float, default=0.2, help="пауза между вызовами LLM, сек (по умолчанию 0.2)")
    parser.add_argument("--verbose", action="store_true", help="печатать каждую карточку")
    args = parser.parse_args()

    try:
        sys.exit(asyncio.run(_run(args)))
    except KeyboardInterrupt:
        # Прогрев прерываем без трассировки: уже записанные карточки в БД остались,
        # повторный запуск продолжит с того же места.
        print("\nПрервано. Уже прогретые карточки сохранены.", file=sys.stderr)
        sys.exit(130)
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
