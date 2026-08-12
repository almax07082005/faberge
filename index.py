"""Точка входа Yandex Cloud Functions: мост между событием API Gateway и ASGI-приложением FastAPI.

Кроме перекладывания события в ASGI-scope мост отвечает за то, что приложение из-под
FastAPI не видит (баг-репорт 12.08.2026, п.2 — «GET /halls/7/exhibits разово отдал
состав зала 14»):
  • `Cache-Control: no-store` на каждый ответ — чтобы промежуточный кэш не мог отдать
    клиенту старое или чужое тело;
  • сквозной `x-request-id` — вход события ↔ scope запроса ↔ заголовок ответа, иначе
    разовый симптом невозможно найти в логах;
  • уборка задач, переживших вызов, — общий event loop не должен копить мусор между
    вызовами «тёплого» экземпляра.

Тесты моста (без облака и без БД): tests/test_bridge.py.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import uuid
from urllib.parse import urlencode

# ВАЖНО: переменные окружения нужно выставить ДО импорта app.main —
# app/config.py читает их один раз при импорте (Settings кэшируется).
_HERE = os.path.dirname(os.path.abspath(__file__))
# Файловая система функции read-only; писать можно только в /tmp.
os.environ.setdefault("MEDIA_DIR", "/tmp/media")
# CA-сертификат Yandex для TLS-подключения к Managed PostgreSQL лежит в архиве рядом.
os.environ.setdefault("DB_SSL_ROOT_CERT", os.path.join(_HERE, "CA.pem"))

from app.main import app  # noqa: E402

logger = logging.getLogger(__name__)

# Один общий event loop на «тёплый» экземпляр функции, чтобы пул соединений
# asyncpg не отвязывался от петли событий между вызовами.
_loop = asyncio.new_event_loop()
asyncio.set_event_loop(_loop)

# Сквозной идентификатор запроса (баг-репорт 12.08.2026, п.2). Без него симптом
# «GET /halls/7/exhibits разово отдал состав зала 14» принципиально не разбирается
# по логам: у клиента на руках только тело ответа, а сопоставить его с конкретным
# вызовом функции нечем. Берём id из события (гейтвей/клиент прислал свой) либо
# генерируем и возвращаем в ответе — дальше заказчику достаточно приложить
# заголовок, чтобы вызов нашёлся в логах функции.
_REQUEST_ID_HEADER = "x-request-id"
# Осиротевшие задачи снимаются насильно; ждать их дольше секунды бессмысленно —
# ответ клиенту уже сформирован, а вызов функции тарифицируется по времени.
_STRAY_TASK_GRACE = 1.0


def _request_id(event: dict) -> str:
    """x-request-id из события или новый.

    Значение уезжает и в scope запроса, и в заголовки ответа, поэтому чужую строку
    берём только если она безопасна: печатный ASCII (заголовки кодируются в latin-1,
    кириллица в них уронила бы вызов) и разумной длины. Всё остальное — свой uuid4:
    лучше не совпасть с идентификатором клиента, чем не ответить вовсе.
    """
    for key, value in (event.get("headers") or {}).items():
        if key.lower() != _REQUEST_ID_HEADER:
            continue
        candidate = str(value).strip()
        if 0 < len(candidate) <= 200 and all(32 <= ord(ch) < 127 for ch in candidate):
            return candidate
    return uuid.uuid4().hex


def _scope_from_event(event: dict, request_id: str) -> dict:
    headers = [
        (k.lower().encode("latin-1"), str(v).encode("latin-1"))
        for k, v in (event.get("headers") or {}).items()
        # Свой x-request-id кладём ровно один — иначе приложение увидит два
        # значения через запятую и залогирует не то, что вернулось клиенту.
        if k.lower() != _REQUEST_ID_HEADER
    ]
    headers.append((_REQUEST_ID_HEADER.encode("latin-1"), request_id.encode("latin-1")))
    multi_q = event.get("multiValueQueryStringParameters")
    if multi_q:
        pairs = [(k, v) for k, vals in multi_q.items() for v in vals]
    else:
        pairs = list((event.get("queryStringParameters") or {}).items())
    path = event.get("path") or event.get("url") or "/"
    src_ip = (
        ((event.get("requestContext") or {}).get("identity") or {}).get("sourceIp")
    ) or ""
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": (event.get("httpMethod") or "GET").upper(),
        "scheme": "https",
        "path": path,
        "raw_path": path.encode("latin-1"),
        "query_string": urlencode(pairs).encode("latin-1"),
        "root_path": "",
        "headers": headers,
        "server": ("apigw", 443),
        "client": (src_ip, 0),
    }


def _drain_stray_tasks(path: str, request_id: str) -> None:
    """Снять задачи, пережившие вызов handler, и доложить об этом в лог.

    Loop общий на «тёплый» экземпляр функции, значит задача, оставшаяся после
    ответа, доживёт до следующего вызова и будет крутиться уже «в чужом» запросе.
    Тело ответа она подменить не может (out/send/receive — замыкания конкретного
    вызова, осиротевшая задача пишет в свой уже возвращённый out), но держит
    соединение из пула, продолжает ходить во внешние сервисы и путает логи.
    Сейчас такого мусора не возникает — тем ценнее WARNING: если он появится,
    станет видно сразу и с путём запроса, а не через месяц по косвенным симптомам.
    """
    stray = [task for task in asyncio.all_tasks(_loop) if not task.done()]
    if not stray:
        return
    logger.warning(
        "мост: после ответа осталось незавершённых задач: %d (path=%s, request_id=%s, задачи=%s)",
        len(stray), path, request_id, ", ".join(sorted(t.get_name() for t in stray)),
    )
    for task in stray:
        task.cancel()
    try:
        # Дождаться отмены нужно здесь же: иначе задачи «догорят» внутри следующего
        # run_until_complete и их исключения всплывут в чужом запросе.
        _loop.run_until_complete(asyncio.wait(stray, timeout=_STRAY_TASK_GRACE))
    except Exception:  # noqa: BLE001 — уборка не имеет права подменить собой ошибку запроса
        logger.warning("мост: не удалось дождаться отмены задач (request_id=%s)", request_id, exc_info=True)


def handler(event: dict, context) -> dict:
    raw = event.get("body") or ""
    body = (
        base64.b64decode(raw) if event.get("isBase64Encoded") else raw.encode("utf-8")
    )

    request_id = _request_id(event)
    scope = _scope_from_event(event, request_id)
    out = {"status": 500, "headers": [], "body": bytearray()}
    sent = False

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        # Тело запроса уже отдано целиком, второй раз receive() зовёт только
        # StreamingResponse — из listen_for_disconnect. Ответить здесь
        # http.disconnect значит сказать «клиент отвалился»: Starlette снимает
        # задачу, которая ещё не успела отправить тело, и выгрузка
        # /admin/analytics/export приезжала как 200 с пустым файлом (заголовки
        # уже ушли, статус не поправить). Событие никогда не наступает —
        # задачу-группу снимет сам StreamingResponse, когда допишет тело.
        await asyncio.Event().wait()

    async def send(message):
        if message["type"] == "http.response.start":
            out["status"] = message["status"]
            out["headers"] = message.get("headers", [])
        elif message["type"] == "http.response.body":
            out["body"].extend(message.get("body", b""))

    try:
        _loop.run_until_complete(app(scope, receive, send))
    finally:
        # Даже если приложение упало — мусор в общий loop оставлять нельзя.
        _drain_stray_tasks(scope["path"], request_id)

    # Имена заголовков в ASGI и так в нижнем регистре; приводим явно, чтобы в
    # словаре не оказалось «Cache-Control» и «cache-control» одновременно —
    # гейтвей отправил бы оба, и промежуточный кэш выбрал бы себе удобный.
    headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in out["headers"]}
    # no-store на ВСЕ ответы функции (баг-репорт 12.08.2026, п.2). Разбор показал,
    # что чужое тело («зал 7» с составом зала 14) наше приложение отдать не может:
    # единственный слой, который на это способен, — HTTP-кэш между клиентом и
    # функцией (браузер, service worker, промежуточный прокси), а заголовков
    # кэширования не ставит ни приложение, ни прод — то есть кэш волен решать сам.
    # Ставим здесь, в мосте, а не в app/main.py: так правило распространяется на
    # весь ответ функции, включая ошибки гейтвея-уровня и то, что роутеры не видят.
    # Ручку-исключение мы не выделяем сознательно: кэшировать в этом API нечего
    # (каталог правится админкой, гид и телеметрия персональны, /media на проде
    # раздаёт Object Storage, а не функция), а «одна незакрытая ручка» — это ровно
    # тот класс, который мы закрываем. setdefault, а не присваивание: если ручка
    # когда-нибудь сама выберет политику кэширования, мост её не перебьёт.
    headers.setdefault("cache-control", "no-store")
    # request_id возвращаем всегда и своим значением: в логах функции записан именно он.
    headers[_REQUEST_ID_HEADER] = request_id

    return {
        "statusCode": out["status"],
        "headers": headers,
        "body": base64.b64encode(bytes(out["body"])).decode("ascii"),
        "isBase64Encoded": True,
    }
