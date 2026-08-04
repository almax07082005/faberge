"""Ночной пересчёт аналитических агрегатов: функция под таймер-триггер.

Зачем отдельная функция, а не таймер на `faberge-api`: `index.handler` основной
функции — мост «событие API Gateway → ASGI» и читает из события `httpMethod` и
`path`. Таймер-триггер отдаёт событие другой формы, поэтому вызов выродился бы
в `GET /` вместо пересчёта. Здесь тот же результат достигается обычным HTTPS-
запросом к уже опубликованному эндпоинту — без правок боевого кода.

Эквивалент `python scripts/rebuild_analytics.py` (§12 ТЗ 03.08.2026):
`POST /admin/analytics/rebuild` БЕЗ `from`/`to`.

Почему без границ периода, хотя в ТЗ джоб описан как `--days 2`: ключ кэша
отчёта — это `<from>:<to>` (таблица `analytics_reports`, колонка `period_key`).
Дашборд открывается без фильтра по датам, то есть читает ключ открытого периода
`:`. Пересчёт с окном пишет в ключ `2026-08-03:2026-08-04` и ключа `:` не
касается — дашборд продолжает отдавать протухшую запись, пока ей не выйдет
`ANALYTICS_CACHE_TTL_MINUTES` (сутки), и только тогда считается на лету. Ровно
то, чего джоб должен был избежать. Прогон без границ греет те самые ключи,
которые запрашивает дашборд, и заодно пересчитывает весь суточный срез.

Идемпотентно: повторный запуск за ту же дату перезаписывает строки среза.

Стоимость прогона на 04.08.2026 — 1.6 с при 214 событиях и 19 сутках среза.
Таблица `events` по требованию заказчика не чистится, поэтому время растёт
линейно от посещаемости; основной функции отведено 30 с. Когда прогон начнёт
подбираться к этому пределу, вариантов два: поднять `--execution-timeout` у
`faberge-api` либо перенести пересчёт на `scripts/rebuild_analytics.py`,
запускаемый там, где лимита нет. Текущую длительность видно в логах этой
функции — она печатает `elapsed_sec` на каждый прогон.

Только стандартная библиотека — архиву функции не нужен requirements.txt.

Env:
    ANALYTICS_API_BASE  базовый URL API Gateway (без слэша на конце)
    ADMIN_API_TOKEN     тот же Bearer, что у остальной админки
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

_TIMEOUT_SEC = 55


def handler(event, context):  # noqa: ANN001 — сигнатура задана Cloud Functions
    base = os.environ["ANALYTICS_API_BASE"].rstrip("/")
    token = os.environ["ADMIN_API_TOKEN"]

    request = urllib.request.Request(
        f"{base}/admin/analytics/rebuild",
        method="POST",
        headers={"Authorization": f"Bearer {token}"},
    )

    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_SEC) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # Ошибку не глотаем: пусть попадёт в лог функции и в метрики триггера,
        # иначе дашборд молча поедет на устаревших агрегатах.
        detail = exc.read().decode("utf-8", "replace")[:500]
        raise RuntimeError(f"rebuild вернул {exc.code}: {detail}") from exc

    elapsed = round(time.monotonic() - started, 2)
    print(json.dumps({"elapsed_sec": elapsed, "result": body}, ensure_ascii=False))
    return {"statusCode": 200, "elapsed_sec": elapsed, "body": body}
