# ИИ-гид музея Фаберже — backend (FastAPI + PostgreSQL 17)

Backend мобильного web-приложения (PWA) **«ИИ-гид музея Фаберже»** по дорожной
карте MVP (25 мая – 25 июня) и его OpenAPI-контракт.

Посетитель сканирует **QR-код** на входе, ходит по **интерактивной карте**
(зал → витрина → экспонат) и может **сфотографировать экспонат**, получить о
нём рассказ ИИ-гида и **прослушать** озвучку.

```
Клик «Распознать экспонат» → камера → снимок
   → POST /recognition   (фото → YOLO → label_slug)
   → POST /guide/story    (label_slug → raw_history из БД → YandexGPT → рассказ + 3–4 вопроса)
   → POST /speech         («Прослушать» → SpeechKit → аудио)
   → GET  /exhibits/{id}/related   («Другие экспонаты зала»)
```

## Быстрый старт

### Вариант A — Docker Compose (PostgreSQL 17 + API одной командой)

```bash
docker compose up --build
```

- API + Swagger UI: <http://localhost:8000/docs>
- ReDoc: <http://localhost:8000/redoc>
- PostgreSQL 17: `localhost:5432` (`faberge` / `faberge`)

Схема (`db/schema.sql`) и демо-данные (`db/seed.sql`) применяются автоматически
при первой инициализации тома БД.

### Вариант B — локально (venv + своя БД)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r app/requirements.txt

export DATABASE_URL="postgresql+asyncpg://faberge:faberge@localhost:5432/faberge"
python scripts/init_db.py --seed          # применить схему + демо-данные
uvicorn app.main:app --reload --port 8000
```

### Подключение к Yandex Managed PostgreSQL 17

TLS обязателен — сначала скачайте CA-сертификат
([инструкция Yandex Cloud](https://yandex.cloud/docs/managed-postgresql/operations/connect)):

```bash
export DATABASE_URL="postgresql+asyncpg://<user>:<pwd>@<fqdn>:6432/<db>"
export DB_SSL_ROOT_CERT="$HOME/.postgresql/root.crt"
python scripts/init_db.py --seed          # создаст схему в managed-кластере
uvicorn app.main:app --port 8000
```

Полный список переменных окружения — в [`.env.example`](.env.example).

## Архитектура: что реально, а что — стаб

| Слой | Реализация |
|------|------------|
| **Навигация, каталог, поиск, экспонаты** | **Полностью реальны** — запросы к PostgreSQL 17 (SQLAlchemy 2.0 async + asyncpg). |
| **Распознавание / YandexGPT / SpeechKit / Object Storage** | Интерфейс + **рабочий стаб** (детерминированный, без облака) **и** реальный вызов API Yandex Cloud. Реализация выбирается автоматически по наличию ключей в окружении. |

Без ключей Yandex (как в локальной разработке) сервис работает целиком:
распознавание детерминированно сопоставляет фото с экспонатом из БД, рассказ
собирается из полей экспоната и `raw_history`, а «Прослушать» отдаёт реальный
(тихий) WAV. Появились ключи → те же эндпоинты ходят в YOLO / YandexGPT /
SpeechKit. Флаги готовности видны в `GET /health`.

### Распознавание: сшивка с каталогом

ML-сервис поиска по фото ключует предметы по названию (`title`), каталог — по
`label_slug`. Названия сопоставляются после нормализации (регистр, «ё/е»,
кавычки-ёлочки, двойные пробелы) и, при промахе, нечётко — порог
`RECOGNITION_NAME_MATCH_CUTOFF`. Несшитые предсказания попадают в лог уровня
`WARNING` вместе с самим `title`, `item_id` и `confidence`, сырой ответ
сервиса — в `INFO`: по этим строкам видно, из-за чего распознавание не находит
экспонат. Если сшить не удалось ни одно предсказание, кандидаты добираются
полнотекстовым поиском по названиям — фронт показывает «возможно, это» вместо
глухой ошибки.

### Озвучивание: числительные

В синтез уходит текст с числами прописью в нужном падеже — «Пётр Первый», а не
«Пётр один», «в девятнадцатом веке», а не «в 19 веке». Переписывает
`llm.to_spoken_text` (YandexGPT, `temperature=0`), фолбэк без LLM —
детерминированный `app/services/text_normalize.py`. LLM зовётся только когда в
тексте есть числа, результат кэшируется по хэшу исходного текста; отключается
через `TTS_SPOKEN_VIA_LLM=false`. Нормализация выполняется **до** подсчёта
символов и ключа кэша аудио.

## Документация API: два представления

| | Дизайн-контракт | Живая реализация |
|---|---|---|
| Источник | [`openapi.yaml`](openapi.yaml) (рукописный, OpenAPI 3.0.3, с примерами) | автогенерация FastAPI из кода (OpenAPI 3.1) |
| Swagger UI | `swagger/` через `./serve.sh` → <http://localhost:8080/swagger/> | <http://localhost:8000/docs> |
| Когда смотреть | согласование контракта, фронтенд, codegen | то, что сервер реально отдаёт сейчас |

## Структура репозитория

```
openapi.yaml            Дизайн-контракт (OpenAPI 3.0.3, рукописный)
swagger/                Vendored Swagger UI (offline) + брендированная страница
serve.sh                Запуск статического Swagger UI (порт 8080)

app/
  main.py               Сборка FastAPI: роутеры, CORS, /media, обработка ошибок
  config.py             Настройки (pydantic-settings, .env)
  db.py                 Async-движок и сессии SQLAlchemy
  models.py             ORM-модели (= db/schema.sql)
  schemas.py            Pydantic-схемы запросов/ответов
  crud.py               Запросы к БД и сериализация
  dependencies.py       Пагинация, Bearer-авторизация админа
  routers/              system, navigation, exhibits, search, recognition,
                        guide, speech, admin, telemetry
  services/             recognizer (YOLO), llm (YandexGPT), tts (SpeechKit),
                        storage (Object Storage) — стаб + реальный вызов
  requirements.txt

db/
  schema.sql            DDL для PostgreSQL 17 (halls/showcases/exhibits + …)
  seed.sql              Демо-данные (залы, витрины, шедевры коллекции)
  migrations/           Инкрементальные миграции к живой БД (идемпотентные)
  guide_showcases.example.json  Формат выписки из путеводителя для импорта витрин
scripts/
  init_db.py            Применить схему/сид к локальной или Managed PG
  cleanup_hall_catalog.py       Чистка каталога залов (лестница / № 99 / № 100)
  import_guide_showcases.py     Импорт витрин и экспонатов по путеводителю
  smoke_backend_tasks.py        Интеграционный smoke бэкенд-трекера (B1–B11)
  smoke_bugreport_20260728.py   Интеграционный smoke по баг-репорту 28.07.2026
tests/                  Юнит-тесты чистых функций (запускаются без БД и сети)
Dockerfile, docker-compose.yml, .env.example
```

## Тесты

```bash
python tests/test_guide_intel.py        # разбор намерения реплики гида (B7/B9/B10, C25)
python tests/test_text_normalize.py     # числительные для озвучки («Пётр Первый»)
python tests/test_recognizer_match.py   # сшивка названий ML-индекса с каталогом
# либо все сразу, если установлен pytest:
python -m pytest tests/

# интеграционные (нужен запущенный API с применённой схемой + seed):
BASE_URL=http://localhost:8000 python scripts/smoke_backend_tasks.py
BASE_URL=http://localhost:8000 python scripts/smoke_bugreport_20260728.py
```

## Карта эндпоинтов

| Группа | Эндпоинты |
|--------|-----------|
| Система | `GET /health` |
| Карта и навигация | `GET /map`, `/halls`, `/halls/{id}`, `/halls/{id}/showcases`, `/halls/{id}/exhibits`, `/showcases/{id}`, `/showcases/{id}/exhibits` |
| Экспонаты | `GET /exhibits`, `/exhibits/{id}`, `/exhibits/by-slug/{slug}`, `/exhibits/{id}/related` |
| Поиск | `GET /search` |
| Распознавание | `POST /recognition` (multipart: фото → YOLO) |
| ИИ-гид | `POST /guide/story`, `POST /guide/chat` (YandexGPT + вопросы-подсказки) |
| Озвучивание | `POST /speech` (SpeechKit) |
| Админ · вход | `POST /admin/login` (логин/пароль → Bearer-токен; открытый) |
| Админ · медиа | `GET`/`POST /admin/exhibits/{id}/media`, `DELETE /admin/exhibits/{id}/media/{image_id}`, `POST /admin/halls/{id}/cover` |
| Администрирование* | CRUD `/admin/exhibits`, `/admin/halls`, `PATCH /admin/halls/{id}`, `/admin/showcases`, `PATCH /admin/showcases/{id}`, `/admin/analytics/overview` |
| Телеметрия | `POST /telemetry/events` (обязательный контракт с 21.07.2026 — источник аналитики) |

\* — вне MVP; защищено `bearerAuth` (заголовок `Authorization: Bearer <token>`, токен — из `POST /admin/login` или `ADMIN_API_TOKEN`). Загрузка медиа/обложек и вход — рабочие (в MVP): размер ≤ `MAX_UPLOAD_MB` (10 МБ), форматы JPEG/PNG/WebP; `thumbnail_url` совпадает с `image_url` (отдельная миниатюра не генерируется).

## База данных

Иерархия `halls` → `showcases` → `exhibits` из роадмапа, расширенная полями,
которые отдаёт API (описание зала, медиа экспоната, галерея). Ключевое поле
`exhibits.label_slug` — класс от YOLO (напр. `faberge_egg_winter`);
`exhibits.raw_history` — внутренние факты для YandexGPT (в публичный API не
отдаётся, доступны только админ-эндпоинтам). Полный DDL — [`db/schema.sql`](db/schema.sql).

### Миграции

`db/migrations/*.sql` — инкрементальные, идемпотентные, с секцией отката в конце
файла. Применяются до деплоя новой версии кода:

```bash
psql "$DATABASE_URL" -f db/migrations/2026-07-29_bugreport_catalog.sql
# либо (тот же эффект) переприменить схему целиком:
python scripts/init_db.py
```

### Каталог залов и витрин

| Признак | Что означает |
|---|---|
| `halls.hall_number = NULL` | Зал без номера («Вне постоянной экспозиции»): в подписях и в ответах гида — только название, без «зал № …». В списке идёт последним и не попадает в счётчик «В музее N залов». |
| `halls.is_service = true` | Служебная запись (Парадная лестница). Остаётся в каталоге и доступна по прямой ссылке, но не попадает ни в `GET /halls`, ни на карту, ни в ответы гида. Админке видна по `?include_service=true`. |
| `showcases.showcase_number = NULL` | Группа «не в витринах» (в путеводителе — пустой квадрат): экспонаты зала вне витрин. В зале такая группа одна (частичный уникальный индекс), выводится последней. |

Разовые операции над каталогом (оба скрипта идемпотентны, по умолчанию — сухой
прогон, печатающий план):

```bash
python scripts/cleanup_hall_catalog.py --apply          # лестница → служебная, № 99 без номера, № 100 удалить
BASE_URL=... ADMIN_TOKEN=... \
  python scripts/import_guide_showcases.py db/guide_showcases.json --apply
```

Группировку «витрина → её экспонаты» фронт собирает из одного ответа
`GET /halls/{id}/exhibits`: каждый элемент несёт `showcase_id` и `showcase_number`.

### Контекст диалога ИИ-гида

`POST /guide/chat` различает «контекст не передавали» и «сбросьте контекст» —
иначе зал «залипал» в сессии и общий вопрос получал отказ «в материалах о
Рыцарском зале нет информации»:

| Что прислал клиент | Что делает бэкенд |
|---|---|
| поля `context` нет | подставляет контекст, сохранённый в сессии (продолжение разговора) |
| `"context": {}` или `null` | сбрасывает контекст сессии, отвечает на вопрос как на общий |
| `"reset_context": true` | то же самое, но без передачи поля `context` |
| заполненный `context` | заменяет контекст сессии целиком (старый `hall_id` не «доклеивается») |

Сам контекст зала подаётся модели как подсказка, а не как единственный источник:
если ответа в нём нет, гид отвечает по общим знаниям о музее и коллекции.

## Технологический стек

Python + FastAPI · SQLAlchemy 2.0 (async) + asyncpg · Yandex Managed
PostgreSQL 17 · Yandex API Gateway · Object Storage + CDN · Cloud Functions ·
YOLO (Datasphere → Cloud) · YandexGPT · SpeechKit · Koinovo (3D).

## Источники данных для наполнения

- План экспозиции: <https://fabergemuseum.ru/posetitelyam/plan-ekspozitsii>
- Путеводитель (PDF): <http://fabergemuseum.ru/image/pdf/faberge_expo.pdf>
- Шедевры коллекции: <https://fabergemuseum.ru/kollekczii/shedevryi-kollekczii/>
- 3D-модели (Koinovo): <https://koinovo.ru/fabergemuseum>
