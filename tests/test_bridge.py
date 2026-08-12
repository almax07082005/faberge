"""Тесты моста Yandex Cloud Functions ↔ ASGI (index.py) — баг-репорт 12.08.2026, п.2.

Повод: «GET /halls/7/exhibits разово вернул total=171 с витринами Голубой гостиной».
Разбор показал, что чужое тело мост отдать не может (out/send/receive — замыкания
конкретного вызова), но проверено это было только рассуждением: тестов у моста
не было ни одного. Здесь закрыт весь класс:
  • ответ вызова — свой (тело, статус, заголовки приложения);
  • два последовательных вызова не перетекают друг в друга;
  • задача, дожившая с прошлого вызова, пишет в свой уже возвращённый буфер и
    текущий ответ не портит, а сам мост такие задачи снимает и логирует WARNING;
  • на каждом ответе есть `Cache-Control: no-store` (единственный слой, способный
    подменить тело, — HTTP-кэш между клиентом и функцией);
  • `x-request-id` проходит насквозь: событие → scope запроса → заголовок ответа.

Ни облака, ни БД, ни сети: вместо `app.main:app` подставляется игрушечное ASGI-приложение
(index.handler берёт приложение из глобали, поэтому подмена честная — мост тестируется
настоящий). Импорт index тянет app.main, но тот на импорте только собирает FastAPI:
соединение с БД создаётся лениво, наружу никто не ходит.

Запуск:
    python -m pytest tests/test_bridge.py
    python tests/test_bridge.py     # standalone
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import index  # noqa: E402


# ── Инструменты: событие гейтвея, вызов моста, разбор ответа ──────────────────────────────
def event(path: str = "/halls/7/exhibits", *, method: str = "GET", headers=None, query=None, body: str = "") -> dict:
    """Событие API Gateway в том виде, в каком его получает handler."""
    return {
        "httpMethod": method,
        "path": path,
        "headers": dict(headers or {}),
        "queryStringParameters": dict(query or {}),
        "body": body,
        "isBase64Encoded": False,
        "requestContext": {"identity": {"sourceIp": "203.0.113.7"}},
    }


def call(asgi_app, evt: dict) -> dict:
    """Вызов handler с подменённым приложением; исходное возвращаем в любом случае."""
    saved = index.app
    index.app = asgi_app
    try:
        return index.handler(evt, None)
    finally:
        index.app = saved


def body_of(response: dict) -> str:
    """Тело ответа функции — всегда base64 (мост отдаёт бинарь одинаково для всех ручек)."""
    return base64.b64decode(response["body"]).decode("utf-8")


def header_of(scope: dict, name: str) -> list:
    """Все значения заголовка в scope — список, чтобы ловить дубли."""
    return [v.decode("latin-1") for k, v in scope["headers"] if k.decode("latin-1") == name]


class LogCapture:
    """Перехват записей логгера моста.

    caplog не годится: тест должен работать и как standalone-скрипт без pytest.
    """

    def __init__(self, logger: logging.Logger = index.logger):
        self.logger = logger
        self.records: list[logging.LogRecord] = []

    def __enter__(self):
        capture = self

        class _Handler(logging.Handler):
            def emit(self, record):
                capture.records.append(record)

        self._handler = _Handler()
        self._level = self.logger.level
        self.logger.setLevel(logging.DEBUG)
        self.logger.addHandler(self._handler)
        return self

    def __exit__(self, *exc):
        self.logger.removeHandler(self._handler)
        self.logger.setLevel(self._level)
        return False

    def warnings(self) -> list[str]:
        return [r.getMessage() for r in self.records if r.levelno >= logging.WARNING]


# ── Игрушечные ASGI-приложения ────────────────────────────────────────────────────────────
class ToyApp:
    """Минимальное ASGI-приложение: отдаёт заданный ответ и запоминает, что ему пришло."""

    def __init__(self, body: bytes = b"halls-7", status: int = 200, headers=()):
        self.body = body
        self.status = status
        self.headers = [(k.encode("latin-1"), v.encode("latin-1")) for k, v in headers]
        self.scopes: list[dict] = []
        self.request_bodies: list[bytes] = []
        self.sends: list = []

    async def __call__(self, scope, receive, send):
        self.scopes.append(scope)
        self.sends.append(send)
        message = await receive()
        self.request_bodies.append(message.get("body", b""))
        await send({"type": "http.response.start", "status": self.status, "headers": list(self.headers)})
        await send({"type": "http.response.body", "body": self.body})

    @property
    def scope(self) -> dict:
        return self.scopes[-1]


class LeakyApp(ToyApp):
    """Приложение, оставляющее после ответа незавершённую задачу.

    Так выглядит любой «забытый» фоновой job: он переживает вызов и в общем loop
    доживает до следующего запроса. Мост обязан снять его и сказать об этом в лог.
    """

    async def __call__(self, scope, receive, send):
        await super().__call__(scope, receive, send)
        self.leaked = asyncio.get_running_loop().create_task(
            asyncio.Event().wait(), name="осиротевшая-задача"
        )


class OrphanWriterApp(ToyApp):
    """Приложение, в котором «доживает» задача предыдущего вызова.

    Задача держит send прошлого запроса и дописывает в него тело уже во время
    текущего. Проверяем, что текущий ответ от этого не меняется.
    """

    def __init__(self, orphan_send, **kwargs):
        super().__init__(**kwargs)
        self.orphan_send = orphan_send

    async def __call__(self, scope, receive, send):
        await self.orphan_send({"type": "http.response.body", "body": b"-hall-14-tail"})
        await super().__call__(scope, receive, send)


# ── Обычный ответ ─────────────────────────────────────────────────────────────────────────
def test_response_carries_own_body_status_and_headers():
    """Базовое: тело, статус и заголовки приложения доезжают до гейтвея без изменений."""
    app = ToyApp(body=b"total=81", status=207, headers=[("content-type", "application/json")])
    response = call(app, event())
    assert response["statusCode"] == 207
    assert body_of(response) == "total=81"
    assert response["headers"]["content-type"] == "application/json"
    assert response["isBase64Encoded"] is True


def test_request_reaches_the_app_intact():
    """Метод, путь, query и тело запроса переносятся в scope и receive()."""
    app = ToyApp()
    call(app, event("/telemetry/events", method="POST", query={"debug": "1"}, body='{"a":1}'))
    assert app.scope["method"] == "POST"
    assert app.scope["path"] == "/telemetry/events"
    assert app.scope["query_string"] == b"debug=1"
    assert app.request_bodies == [b'{"a":1}']


# ── Изоляция вызовов ──────────────────────────────────────────────────────────────────────
def test_two_calls_do_not_bleed_into_each_other():
    """Симптом заказчика в чистом виде: второй вызов не должен видеть тело первого."""
    first = call(ToyApp(body=b"hall-14: total=171"), event("/halls/14/exhibits"))
    second = call(ToyApp(body=b"hall-7: total=81"), event("/halls/7/exhibits"))
    assert body_of(first) == "hall-14: total=171"
    assert body_of(second) == "hall-7: total=81"


def test_orphan_task_writes_into_its_own_returned_buffer():
    """Задача с прошлого вызова портит только свой (уже отданный) буфер, не текущий ответ."""
    previous_app = ToyApp(body=b"hall-14: total=171")
    first = call(previous_app, event("/halls/14/exhibits"))
    orphan_send = previous_app.sends[-1]

    current = call(OrphanWriterApp(orphan_send, body=b"hall-7: total=81"), event("/halls/7/exhibits"))

    assert body_of(current) == "hall-7: total=81"
    assert body_of(first) == "hall-14: total=171"      # ответ уже сериализован, дописать в него нечего


def test_stray_task_is_cancelled_and_logged():
    """Незавершённую задачу мост снимает сразу и пишет WARNING с путём запроса."""
    app = LeakyApp()
    with LogCapture() as log:
        call(app, event("/admin/analytics/export"))
    assert app.leaked.cancelled()
    assert len(log.warnings()) == 1
    message = log.warnings()[0]
    assert "/admin/analytics/export" in message
    assert "осиротевшая-задача" in message


def test_stray_task_does_not_survive_into_the_next_call():
    """Мусор не копится: к следующему вызову общий loop пуст, и предупреждать не о чем."""
    with LogCapture():                                 # предупреждение здесь ожидаемо, в вывод не пускаем
        call(LeakyApp(), event("/admin/analytics/export"))
    assert [t for t in asyncio.all_tasks(index._loop) if not t.done()] == []
    with LogCapture() as log:
        response = call(ToyApp(body=b"halls-7"), event())
    assert log.warnings() == []
    assert body_of(response) == "halls-7"


def test_clean_call_is_silent():
    """Обычный запрос не должен порождать WARNING — иначе предупреждение обесценится."""
    with LogCapture() as log:
        call(ToyApp(), event())
    assert log.warnings() == []


# ── Кэширование ───────────────────────────────────────────────────────────────────────────
def test_cache_control_no_store_is_always_set():
    """no-store на каждом ответе: промежуточный кэш не должен отдавать старое тело."""
    response = call(ToyApp(), event())
    assert response["headers"]["cache-control"] == "no-store"


def test_cache_control_of_the_app_is_not_overridden():
    """Если ручка сама выбрала политику кэширования, мост её не перебивает."""
    app = ToyApp(headers=[("cache-control", "public, max-age=60")])
    response = call(app, event("/media/exhibit-52.jpg"))
    assert response["headers"]["cache-control"] == "public, max-age=60"


def test_error_response_is_also_no_store():
    """Ошибки кэшировать особенно вредно: 500 «залипнет» у клиента после починки."""
    response = call(ToyApp(body=b'{"detail":"boom"}', status=500), event())
    assert response["headers"]["cache-control"] == "no-store"


# ── Сквозной идентификатор запроса ────────────────────────────────────────────────────────
def test_request_id_is_passed_through():
    """Присланный x-request-id уходит в приложение и возвращается клиенту без изменений."""
    app = ToyApp()
    response = call(app, event(headers={"X-Request-Id": "req-2026-08-12-0001"}))
    assert response["headers"]["x-request-id"] == "req-2026-08-12-0001"
    assert header_of(app.scope, "x-request-id") == ["req-2026-08-12-0001"]


def test_request_id_is_generated_when_missing():
    """Без заголовка мост генерирует id — и в scope, и в ответе он один и тот же."""
    app = ToyApp()
    response = call(app, event())
    generated = response["headers"]["x-request-id"]
    assert generated
    assert header_of(app.scope, "x-request-id") == [generated]


def test_request_id_is_unique_per_call():
    """Разные вызовы — разные id, иначе по логам их не различить."""
    first = call(ToyApp(), event())
    second = call(ToyApp(), event())
    assert first["headers"]["x-request-id"] != second["headers"]["x-request-id"]


def test_unsafe_request_id_is_replaced():
    """Чужой id с кириллицей не уронит вызов (заголовки кодируются в latin-1) — он заменяется."""
    app = ToyApp()
    response = call(app, event(headers={"x-request-id": "запрос-№1"}))
    assert response["headers"]["x-request-id"] != "запрос-№1"
    assert header_of(app.scope, "x-request-id") == [response["headers"]["x-request-id"]]


def test_request_id_is_not_duplicated_in_scope():
    """В scope ровно один x-request-id — иначе приложение залогирует не то, что ушло клиенту."""
    app = ToyApp()
    call(app, event(headers={"X-Request-Id": "a", "x-request-id": "b"}))
    assert len(header_of(app.scope, "x-request-id")) == 1


def test_other_headers_survive():
    """Правка заголовков не должна выкидывать всё остальное, что прислал клиент."""
    app = ToyApp()
    call(app, event(headers={"Authorization": "Bearer t", "X-Request-Id": "r-1"}))
    assert header_of(app.scope, "authorization") == ["Bearer t"]


if __name__ == "__main__":
    failures = 0
    for name, func in sorted(globals().items()):
        if name.startswith("test_") and callable(func):
            try:
                func()
                print(f"ok   {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
    print("—" * 40)
    print("все тесты пройдены" if not failures else f"провалено: {failures}")
    sys.exit(1 if failures else 0)
