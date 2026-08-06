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
  fix_showcase_orphans.py       Ревизия витрин: записи без номера, повисшие в витринах
  fix_catalog_typography.py     Типографика каталога: кавычки, дефис, пробелы
  fetch_pdf_font.py             Доставка DejaVuSans.ttf для PDF-выгрузки (перед сборкой zip)
  rebuild_analytics.py          Ночной пересчёт аналитических агрегатов (cron)
  backfill_unanswered.py        Разовая разметка «гид не ответил» по старым диалогам
  smoke_backend_tasks.py        Интеграционный smoke бэкенд-трекера (B1–B11)
  smoke_bugreport_20260728.py   Интеграционный smoke по баг-репорту 28.07.2026
  smoke_analytics_20260803.py   Интеграционный smoke аналитики посетителей
docs/
  analytics-privacy.md  Состав данных аналитики: что храним, зачем, как обезличено
  analytics-metrics.md  Метрики визита: формулировки плиток и знаменатель конверсий
  chat-history-decision.md      История чатов: как есть, решение и контракт ручки
assets/fonts/           TTF с кириллицей для PDF-выгрузки (в git не хранится, см. ниже)
tests/                  Юнит-тесты чистых функций (запускаются без БД и сети)
Dockerfile, docker-compose.yml, .env.example
```

## Тесты

```bash
python tests/test_guide_intel.py        # разбор намерения реплики гида (B7/B9/B10, C25)
python tests/test_text_normalize.py     # числительные для озвучки («Пётр Первый»)
python tests/test_recognizer_match.py   # сшивка названий ML-индекса с каталогом
python tests/test_question_cluster.py   # смысловая группировка вопросов посетителей
python tests/test_visits.py             # разбиение сессии на визиты по таймауту 30 мин
python tests/test_event_contract.py     # словарь типов событий и белый список props
python tests/test_guide_import.py       # сшивка выписки путеводителя с каталогом (п.1, 06.08.2026)
python tests/test_showcase_orphans.py   # записи без номера в витринах: разбор и откат (п.1/п.4)
python tests/test_analytics_export.py   # типы ячеек xlsx/pdf и «весь отчёт» одним файлом (п.5–6)
python tests/test_engagement_conversion.py  # база конверсий визита: доля не больше 100 % (п.7)
# только под pytest (подменяет зависимости FastAPI):
python -m pytest tests/test_analytics_export_route.py   # 503 без шрифта, report=all не падает целиком
# либо все сразу, если установлен pytest:
python -m pytest tests/

# интеграционные (нужен запущенный API с применённой схемой + seed):
BASE_URL=http://localhost:8000 python scripts/smoke_backend_tasks.py
BASE_URL=http://localhost:8000 python scripts/smoke_bugreport_20260728.py
BASE_URL=http://localhost:8000 python scripts/smoke_analytics_20260803.py   # аналитика посетителей
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
| Администрирование* | CRUD `/admin/exhibits`, `/admin/halls`, `PATCH /admin/halls/{id}`, `/admin/showcases`, `PATCH /admin/showcases/{id}` |
| Админ · аналитика* | `GET /admin/analytics/{overview,questions,unanswered,engagement,routes,exhibits,recognition,daily}`, `GET /admin/analytics/export` (в т. ч. `report=all` — всё одним файлом), `POST /admin/analytics/rebuild` |
| Телеметрия | `POST /telemetry/events` (обязательный контракт с 21.07.2026 — источник аналитики) |

\* — вне MVP; защищено `bearerAuth` (заголовок `Authorization: Bearer <token>`, токен — из `POST /admin/login` или `ADMIN_API_TOKEN`). Загрузка медиа/обложек и вход — рабочие (в MVP): размер ≤ `MAX_UPLOAD_MB` (2,5 МБ — потолок платформы, см. ниже), форматы JPEG/PNG/WebP; `thumbnail_url` совпадает с `image_url` (отдельная миниатюра не генерируется).

### Лимит загрузки: 2,5 МБ, а не 10

`MAX_UPLOAD_MB` по умолчанию **2.5** (было 10) — п.10 баг-репорта 06.08.2026.
Приложение обещало 10 МБ, но Yandex API Gateway рубит запрос раньше — на
3 670 016 Б (3.5 МиБ) — и отвечает **без CORS-заголовков**, поэтому в браузере
это не 413, а «Failed to fetch»: фронт не может сказать «фото слишком большое».
Наш лимит обязан быть заведомо ниже платформенного, тогда 413 отдаёт FastAPI —
с CORS и внятным текстом («Фото слишком большое — 3,7 МБ при допустимых 2,5 МБ»).

| Стена | Сколько | Почему считаем по ней |
|---|---|---|
| API Gateway, весь запрос | 3 670 016 Б | `413 request entity is larger than limits (3670016)` |
| Синхронный вызов Cloud Function | те же 3 670 016 Б на payload | тело уезжает в функцию как base64 (+33 %), поэтому на файл остаётся `× 3/4` |
| Запас на multipart и заголовки | 65 536 Б | boundary, имя файла, поля `hall_id`/`top_k` |

Итого `3 670 016 × 3/4 − 65 536 = 2 686 976 Б ≈ 2,5 МБ`. Расчёт живёт в
`app/config.py` (`platform_max_upload_bytes`, `max_upload_bytes`,
`max_upload_label`) и настраивается через `GATEWAY_MAX_REQUEST_BYTES` /
`UPLOAD_REQUEST_OVERHEAD_BYTES`. Значение `MAX_UPLOAD_MB` выше платформенного
приложение не примет всерьёз: зажмёт до фактического и напишет `WARNING` в лог —
падать при старте из-за строчки в env для музейного PWA хуже. Поднять сам
потолок можно **только** в конфигурации API Gateway / квотах Yandex Cloud,
кодом — нельзя; там же лечатся и CORS-заголовки на ответах гейтвея.

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
psql "$DATABASE_URL" -f db/migrations/2026-08-03_analytics.sql
# либо (тот же эффект) переприменить схему целиком:
python scripts/init_db.py

# правка данных живого каталога — только миграцией, init_db.py её НЕ сделает:
psql "$DATABASE_URL" -f db/migrations/2026-08-06_bugreport_iter2.sql
```

`2026-08-06_bugreport_iter2.sql` — п.2 баг-репорта 06.08.2026: снимает
`is_temporary` с «Выставочного зала» (№9 по путеводителю), из-за которого зал
уходил в ветку «Временная выставка» и в основной экспозиции его не было. Ищет
зал по паре «номер + название» (id в окружениях разные), печатает вторым
запросом залы, у которых флаг остался — их быть не должно. Флаг ставили
`2026-07-14_add_hall_is_temporary.sql` (`name ILIKE '%выставочный зал%'`) и
`db/seed_fabergemuseum.sql`; из сида он убран, чтобы баг не вернулся после
переналивки, а ту миграцию переприменять нельзя. Эквивалент из админки, если
доступа к `psql` нет — `PATCH /admin/halls/8 {"is_temporary": false}`.

### Каталог залов и витрин

| Признак | Что означает |
|---|---|
| `halls.hall_number = NULL` | Зал без номера («Вне постоянной экспозиции»): в подписях и в ответах гида — только название, без «зал № …». В списке идёт последним и не попадает в счётчик «В музее N залов». |
| `halls.is_service = true` | Служебная запись (Парадная лестница). Остаётся в каталоге и доступна по прямой ссылке, но не попадает ни в `GET /halls`, ни на карту, ни в ответы гида. Админке видна по `?include_service=true`. |
| `showcases.showcase_number = NULL` | Группа «не в витринах» (в путеводителе — пустой квадрат): экспонаты зала вне витрин. В зале такая группа одна (частичный уникальный индекс), выводится последней. |

Разовые операции над каталогом (все скрипты идемпотентны, по умолчанию — сухой
прогон, печатающий план; изменения — только с `--apply`):

```bash
python scripts/cleanup_hall_catalog.py --apply          # лестница → служебная, № 99 без номера, № 100 удалить
BASE_URL=... ADMIN_TOKEN=... \
  python scripts/import_guide_showcases.py db/guide_showcases.json --apply
# то же + хвост несшитых записей уезжает в «Не в витринах» своего зала:
  ... db/guide_showcases.json --sweep-unmatched --apply

# ревизия витрин: записи без exhibit_number, повисшие в пронумерованных витринах
python scripts/fix_showcase_orphans.py                                # отчёт для музея
python scripts/fix_showcase_orphans.py --apply --drop-empty-showcases # перенос + удалить опустевшие витрины
python scripts/fix_showcase_orphans.py --rollback showcase_orphans_rollback_*.json --apply

# типографика каталога: «ёлочки», «пресс- папье», двойные и хвостовые пробелы
python scripts/fix_catalog_typography.py --report-file typography.csv  # список замен заказчику
python scripts/fix_catalog_typography.py --apply
python scripts/fix_catalog_typography.py --rollback catalog_typography_rollback_*.json --apply
```

Что закрывает каждый скрипт (баг-репорт 06.08.2026):

| Скрипт / ключ | Зачем | Поведение по умолчанию |
|---|---|---|
| `import_guide_showcases.py --sweep-unmatched` | п.1. Импорт нумеровал только то, что нашлось в выписке, а остальное молча оставалось в первой витрине зала — заказчик увидел это как «лишние экспонаты». | Отчёт «не сшито N записей» печатается **всегда**; сам перенос в «Не в витринах» выключен — включает `--sweep-unmatched`. Записи **с** номером свип не трогает. |
| `fix_showcase_orphans.py` | п.1 и п.4. Разбирает те же записи уже по живому каталогу (на проде 06.08.2026 — 42 в экспозиционных залах, одна из них и есть витрина №1 Верхней буфетной, которой нет в путеводителе; ещё 4 в служебном «Вне постоянной экспозиции» не трогаются). Делит их на «вероятный дубль» и «вне путеводителя 2014» и переносит в группу «Не в витринах» своего зала, создавая её при необходимости. | Сухой прогон. Экспонаты **не удаляются**: удаление — только по подтверждённому музеем списку `--delete-ids`, и оно блокируется, если на карточке есть фото, описание, озвучка, каталожные поля или `label_slug`. |
| `fix_catalog_typography.py` | п.9. Машинные дефекты текста: прямые кавычки → «ёлочки», `пресс- папье` → `пресс-папье`, двойные/хвостовые пробелы, невидимые символы из PDF. Правила — в `app/services/text_normalize.analyze_typography`. | Сухой прогон со списком замен и подсветкой различий; `--report-file` выгружает его в CSV (для Excel музея) или JSON. Строку с **непарной** кавычкой скрипт не трогает и выносит в секцию «требует глаз». |

Автоматическое удаление «дублей» по сходству названий **запрещено осознанно**:
нечёткое совпадение даёт ложные срабатывания — «Богоматерь Тихвинская» и
«Богоматерь Иверская» похожи на 0.94, а это разные иконы. Скрипт только
помечает пары в отчёте, решение принимает музей.

Откат: `--apply` пишет файл отката с датой в имени (`showcase_orphans_rollback_…json`,
`catalog_typography_rollback_…json`), обратный прогон — `--rollback <файл> --apply`.
Откат идемпотентен и не затирает правки, сделанные руками после прогона:
запись, которую с тех пор трогали, пропускается с явным сообщением.

Группировку «витрина → её экспонаты» фронт собирает из одного ответа
`GET /halls/{id}/exhibits`: каждый элемент несёт `showcase_id` и `showcase_number`.

### Аналитика посетителей

Источник — таблица `events` (телеметрия с фронта) и `guide_messages` (диалоги).
События по требованию заказчика **не удаляются**, поэтому таблица растёт линейно
от посещаемости — отсюда индексы в миграции `2026-08-03_analytics.sql` и ночной
пересчёт агрегатов.

| Что | Как устроено |
|---|---|
| Контракт событий | Закрытый словарь типов (`schemas.EventType`). Неизвестный тип не роняет батч: отбрасывается поштучно, число — в `rejected`. `audio_play` нормализуется в `tts_play`. |
| Приватность | `props` принимается по белому списку ключей; IP и User-Agent не читаются и не логируются. Подробно — [`docs/analytics-privacy.md`](docs/analytics-privacy.md). |
| Границы периода | `from`/`to` — **включительно с обеих сторон**: `to=2026-07-31` отдаёт и события 31 июля. |
| Визиты | Поток событий сессии режется по неактивности дольше `SESSION_TIMEOUT_MINUTES` (30 мин): вкладка, ожившая через четыре часа, даёт два визита, а не один на четыре часа. |
| Вопросы | Группируются по смыслу (`services/question_cluster.py`), а не по точному совпадению строки: «Сколько стоит яйцо?» и «какая цена яйца» — один кластер. |
| Вопросы без ответа | Признак `guide_messages.answered` + причина проставляются в момент генерации ответа. Старые диалоги размечает `scripts/backfill_unanswered.py`. |
| Агрегаты | Отчёты отдаются из кэша (`analytics_reports`), суточный срез — в `analytics_daily`; в каждом ответе есть `updated_at`. |
| Метрики визита | «Средний визит», «Конверсия в диалог», «Глубина визита» — готовые формулировки для подсказок дашборда и разбор знаменателя: [`docs/analytics-metrics.md`](docs/analytics-metrics.md). |
| Знаменатель конверсий | Отдаётся явно: `conversion_basis` (`app_open` — визиты с запуском приложения, `all_visits` — фолбэк для периодов, где событие ещё не долетало) и `conversion_denominator`. Числитель считается **внутри** базы, поэтому доля не может превысить 100 %. Фронт шлёт `app_open` с 04.08.2026 — цифры сопоставимы между собой с этой даты и только при одинаковом `conversion_basis`. |
| Выгрузка | `GET /admin/analytics/export?report=…&format=xlsx\|pdf`. Даты — датой ячейки (а не строкой с микросекундами), счётчики — целыми, доли — процентом, шапка закреплена, ширины подогнаны. |
| Выгрузка: всё одним файлом | `report=all` — семь разделов (шесть плиток дашборда + `engagement`) в одном файле: лист на отчёт в `.xlsx`, раздел на отчёт в `.pdf`. Имя — `faberge-analytics-<from>-<to>.<ext>`, у одиночного отчёта — `faberge-<report>-<from>-<to>.<ext>`. Период учитывается так же, как в одиночных отчётах. Упавший раздел не уносит остальные: лист остаётся с пометкой «Нет данных», одиночный отчёт по-прежнему честно отдаёт 500. |
| PDF: кириллица | Нужен TTF-шрифт, иначе выгрузка отдаёт **503** с текстом «что доложить», а не лист с квадратами. Наличие шрифта видно заранее в `GET /health` → `dependencies.pdf_font` (`up`/`down`) — не нажимая кнопку. Подробнее — ниже. |

```bash
# ночной пересчёт (cron: 0 4 * * *)
python scripts/rebuild_analytics.py --days 2
# разовая разметка накопленных диалогов
python scripts/backfill_unanswered.py --apply
```

#### Шрифт с кириллицей для PDF-выгрузки

Стандартные шрифты ReportLab (Helvetica) кириллицы не содержат — заказчик видел
это как «отчёт в формате pdf не формируется» (п.3 баг-репорта 06.08.2026).
Шрифт ищется по `ANALYTICS_PDF_FONT_PATH`, затем в `assets/fonts/DejaVuSans.ttf`
(в том числе `/function/code/assets/fonts/` — так распаковывается архив Cloud
Function), затем по системным путям (DejaVu в Linux, Arial Unicode в macOS).
Битый файл на месте шрифта считается отсутствующим: проверяется сигнатура sfnt,
`OTTO` (OpenType/CFF) ReportLab не читает.

Сам `.ttf` (~750 КБ) лежит в репозитории — это осознанное решение: архив Cloud
Function собирается из дерева, а в рантайме функции нет ни apt, ни системных
шрифтов, поэтому без файла в git выгрузка в PDF отдавала 503 после каждого
чистого деплоя. Дополнительно шрифт приезжает и с образом:

| Куда | Как шрифт попадает |
|---|---|
| Архив Yandex Cloud Function | Файл уже в дереве (`assets/fonts/DejaVuSans.ttf`) — отдельного шага сборки не нужно, достаточно не исключить каталог при упаковке zip. |
| Docker-образ | Файл приезжает с исходниками; плюс отдельный слой `apt-get install fonts-dejavu-core` в [`Dockerfile`](Dockerfile) **до** копирования исходников — страховка на случай, если каталог `assets/` из образа исключат. Пакет кладёт файл в `/usr/share/fonts/truetype/dejavu`, этот путь уже в списке кандидатов. |

`scripts/fetch_pdf_font.py` нужен, чтобы обновить или восстановить файл (например,
если он потерялся при переносе репозитория) — на обычной сборке он не запускается.

```bash
python scripts/fetch_pdf_font.py                 # → assets/fonts/DejaVuSans.ttf (+ -Bold)
python scripts/fetch_pdf_font.py --force         # перекачать поверх существующего
PDF_FONT_URL=https://mirror/... python scripts/fetch_pdf_font.py   # своё зеркало
```

Скрипт идемпотентен (пригодный файл на месте — ничего не качает), пишет через
временный файл, проверяет размер и сигнатуру, а версия релиза DejaVu
зафиксирована — шрифт в отчёте музея не должен меняться сам по себе. Жирное
начертание необязательно: без него заголовки таблиц рисуются обычным. Если сети
нет — положите любой TTF с кириллицей руками и укажите
`ANALYTICS_PDF_FONT_PATH`. Проверка после деплоя:

```bash
curl -s "$BASE_URL/health" | grep -o '"pdf_font":"[a-z]*"'        # ожидаем "up"
```

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

### История чатов

Реплики пишутся на сервер (`guide_sessions` / `guide_messages`), но **ручки на
чтение нет** — наружу история не отдаётся: в API только `POST /guide/chat` и
`POST /guide/story`, а переписку для кнопки «назад» хранит фронт локально.
Принятое решение — «чат один на визит», бэкенд трогать не нужно. Разбор, что
именно уже пишется в БД, и готовый контракт `GET /guide/sessions/{session_id}/messages`
на случай, если продукт выберет список прошлых диалогов (вместе с вопросами
приватности — тексты вопросов посетителей это персональные данные) —
[`docs/chat-history-decision.md`](docs/chat-history-decision.md).

## Технологический стек

Python + FastAPI · SQLAlchemy 2.0 (async) + asyncpg · Yandex Managed
PostgreSQL 17 · Yandex API Gateway · Object Storage + CDN · Cloud Functions ·
YOLO (Datasphere → Cloud) · YandexGPT · SpeechKit · Koinovo (3D).

## Источники данных для наполнения

- План экспозиции: <https://fabergemuseum.ru/posetitelyam/plan-ekspozitsii>
- Путеводитель (PDF): <http://fabergemuseum.ru/image/pdf/faberge_expo.pdf>
- Шедевры коллекции: <https://fabergemuseum.ru/kollekczii/shedevryi-kollekczii/>
- 3D-модели (Koinovo): <https://koinovo.ru/fabergemuseum>
