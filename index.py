"""Точка входа Yandex Cloud Functions: мост между событием API Gateway и ASGI-приложением FastAPI."""

from __future__ import annotations

import asyncio
import base64
import os
from urllib.parse import urlencode

# ВАЖНО: переменные окружения нужно выставить ДО импорта app.main —
# app/config.py читает их один раз при импорте (Settings кэшируется).
_HERE = os.path.dirname(os.path.abspath(__file__))
# Файловая система функции read-only; писать можно только в /tmp.
os.environ.setdefault("MEDIA_DIR", "/tmp/media")
# CA-сертификат Yandex для TLS-подключения к Managed PostgreSQL лежит в архиве рядом.
os.environ.setdefault("DB_SSL_ROOT_CERT", os.path.join(_HERE, "CA.pem"))

from app.main import app  # noqa: E402

# Один общий event loop на «тёплый» экземпляр функции, чтобы пул соединений
# asyncpg не отвязывался от петли событий между вызовами.
_loop = asyncio.new_event_loop()
asyncio.set_event_loop(_loop)


def _scope_from_event(event: dict) -> dict:
    headers = [
        (k.lower().encode("latin-1"), str(v).encode("latin-1"))
        for k, v in (event.get("headers") or {}).items()
    ]
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


def handler(event: dict, context) -> dict:
    raw = event.get("body") or ""
    body = (
        base64.b64decode(raw) if event.get("isBase64Encoded") else raw.encode("utf-8")
    )

    scope = _scope_from_event(event)
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

    _loop.run_until_complete(app(scope, receive, send))

    return {
        "statusCode": out["status"],
        "headers": {
            k.decode("latin-1"): v.decode("latin-1") for k, v in out["headers"]
        },
        "body": base64.b64encode(bytes(out["body"])).decode("ascii"),
        "isBase64Encoded": True,
    }
