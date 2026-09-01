-- ============================================================================
--  Схема БД «ИИ-гид музея Фаберже» — PostgreSQL 17 (Yandex Managed PostgreSQL)
-- ----------------------------------------------------------------------------
--  Применение:
--    psql "$DATABASE_URL" -f db/schema.sql
--    -- или: python scripts/init_db.py            (для Yandex Managed PG)
--
--  Базовые таблицы halls / showcases / exhibits взяты из роадмапа и расширены
--  полями, которые отдаёт публичный API (описание зала, медиа экспоната и т.д.).
--  gen_random_uuid() — встроенная функция начиная с PostgreSQL 13, расширение не
--  требуется. pg_trgm используется для поиска по подстроке (ILIKE '%...%').
-- ============================================================================

BEGIN;

CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- Триггерная функция автоматического обновления updated_at -------------------
CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Залы -----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS halls (
    id              SERIAL PRIMARY KEY,
    -- NULL — зал без номера («Вне постоянной экспозиции»): в подписях и в ответах
    -- гида он выводится только по названию, без «зал № …».
    hall_number     INT  UNIQUE,
    name            VARCHAR(255),
    description     TEXT,                       -- описание зала (план/путеводитель)
    level           INT,                        -- этаж/уровень в здании
    cover_image_url TEXT,
    is_temporary    BOOLEAN NOT NULL DEFAULT false,  -- зал временной выставки (vs основная экспозиция)
    is_service      BOOLEAN NOT NULL DEFAULT false,  -- служебная запись каталога (технические залы): скрыта из публичной выдачи
                                                     -- «Парадная лестница» была такой с 29.07 по 31.08.2026 — решение отменено,
                                                     -- см. docs/staircase-hall-decision.md и db/migrations/2026-08-31_staircase_public.sql
    sort_order      INT NOT NULL DEFAULT 0,     -- порядок вывода залов в каталоге/админке (drag-n-drop)
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Полнотекстовый вектор поиска (B8/C27): русская конфигурация, name важнее описания.
    search_vector   tsvector GENERATED ALWAYS AS (
        setweight(to_tsvector('russian', coalesce(name,'')), 'A') ||
        setweight(to_tsvector('russian', coalesce(description,'')), 'C')
    ) STORED
);

-- Идемпотентная миграция для уже существующих БД (CREATE TABLE IF NOT EXISTS выше
-- не добавит колонку к таблице, созданной ранее). Повторный запуск init_db.py
-- безопасен: столбец добавляется только при отсутствии.
ALTER TABLE halls ADD COLUMN IF NOT EXISTS is_temporary BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE halls ADD COLUMN IF NOT EXISTS is_service   BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE halls ADD COLUMN IF NOT EXISTS sort_order   INT NOT NULL DEFAULT 0;
ALTER TABLE halls ALTER COLUMN hall_number DROP NOT NULL;
ALTER TABLE halls ADD COLUMN IF NOT EXISTS search_vector tsvector GENERATED ALWAYS AS (
    setweight(to_tsvector('russian', coalesce(name,'')), 'A') ||
    setweight(to_tsvector('russian', coalesce(description,'')), 'C')
) STORED;

-- Бэкофилл порядка для залов, которым его ещё не задавали (sort_order = 0):
-- берём hall_number как естественный первичный порядок. Reorder присваивает
-- значения 1..N, поэтому переставленные админом залы сюда уже не попадут.
-- Залы без номера пропускаем — sort_order NOT NULL, им порядок ставит бэкенд
-- (crud.create_hall) или админ вручную.
UPDATE halls SET sort_order = hall_number WHERE sort_order = 0 AND hall_number IS NOT NULL;

-- Витрины --------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS showcases (
    id              SERIAL PRIMARY KEY,
    hall_id         INT NOT NULL REFERENCES halls(id) ON DELETE CASCADE,
    -- NULL — группа «не в витринах» (в путеводителе отмечена пустым квадратом):
    -- экспонаты зала, стоящие вне витрин.
    showcase_number INT,
    name            VARCHAR(255),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (hall_id, showcase_number)
);

-- Идемпотентная миграция для существующих БД (см. пояснение у halls выше).
ALTER TABLE showcases ALTER COLUMN showcase_number DROP NOT NULL;
-- UNIQUE(hall_id, showcase_number) не ограничивает строки с NULL, поэтому группу
-- «не в витринах» держим единственной в зале отдельным частичным индексом.
CREATE UNIQUE INDEX IF NOT EXISTS uq_showcases_hall_unnumbered
    ON showcases (hall_id) WHERE showcase_number IS NULL;

-- Экспонаты ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS exhibits (
    id                SERIAL PRIMARY KEY,
    showcase_id       INT REFERENCES showcases(id) ON DELETE CASCADE,
    label_slug        VARCHAR(100) UNIQUE,      -- класс, который возвращает YOLO
    exhibit_number    VARCHAR(32),              -- номер по путеводителю музея (B3): перед названием в каталоге
    name              VARCHAR(255) NOT NULL,
    -- Датировка строкой, дословно как в путеводителе: «1899–1903», «1880-е»,
    -- «конец XIX — начало XX века». До 17.08.2026 — INT (нижняя граница) в паре
    -- с колонкой-дублем dating; теперь поле датировки одно (таска 17.08.2026,
    -- миграция 2026-08-17_year_created_text.sql).
    year_created      TEXT,
    -- Место создания дословно как в путеводителе («Санкт-Петербург», «Швейцария,
    -- Женева»): музей просит показывать его рядом с датой (баг-репорт 31.08.2026,
    -- п. I-2). Парсер каталожной строки место извлекал и раньше, колонки не было.
    origin_place      TEXT,
    master_name       VARCHAR(255),
    material          VARCHAR(255),
    -- Техники (хвост каталожной строки после «;»): «штамп, чеканка, эмаль по гильошированному фону».
    -- Отдельно от material, чтобы «акварель» и «чеканка» не значились материалами (там же, п.5).
    techniques        TEXT,
    short_description TEXT,
    short_description_spoken TEXT,              -- версия short_description для TTS: числа прописью в нужном падеже (LLM)
    raw_history       TEXT,                     -- внутренние факты для YandexGPT
    image_url         TEXT,
    video_url         TEXT,                     -- видео экспоната (B4/C22)
    model_3d_url      TEXT,                     -- ссылка на 3D-модель Koinovo
    model_3d_embed    TEXT,
    audio_url         TEXT,                     -- предсинтезированная озвучка
    source_url        TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Полнотекстовый вектор поиска (B8/C27): русская конфигурация, взвешенная по
    -- важности поля (name=A ... raw_history=D). Покрывает short_description и
    -- raw_history — там упоминается, напр., Николай II. Все функции immutable.
    search_vector     tsvector GENERATED ALWAYS AS (
        setweight(to_tsvector('russian', coalesce(name,'')), 'A') ||
        setweight(to_tsvector('russian', coalesce(master_name,'') || ' ' || coalesce(exhibit_number,'')), 'B') ||
        setweight(to_tsvector('russian', coalesce(short_description,'')), 'C') ||
        setweight(to_tsvector('russian', coalesce(raw_history,'')), 'D')
    ) STORED
);

-- Идемпотентная миграция для существующих БД (см. пояснение у halls выше).
-- Порядок важен: exhibit_number добавляем ДО search_vector (вектор на него ссылается).
ALTER TABLE exhibits ADD COLUMN IF NOT EXISTS short_description_spoken TEXT;
ALTER TABLE exhibits ADD COLUMN IF NOT EXISTS exhibit_number VARCHAR(32);
ALTER TABLE exhibits ADD COLUMN IF NOT EXISTS video_url TEXT;
ALTER TABLE exhibits ADD COLUMN IF NOT EXISTS techniques TEXT;
-- Место создания (баг-репорт 31.08.2026, п. I-2): db/migrations/2026-08-31_exhibit_origin_place.sql.
-- Заполняет колонку не миграция, а обратимый бэкфилл scripts/backfill_exhibit_origin_place.py.
ALTER TABLE exhibits ADD COLUMN IF NOT EXISTS origin_place TEXT;
-- year_created INTEGER → TEXT (+ перенос данных из бывшей колонки dating) для живой БД
-- делает db/migrations/2026-08-17_year_created_text.sql — идемпотентным ALTER'ом это не
-- выражается, а на чистой базе колонка сразу TEXT (CREATE TABLE выше).
-- techniques в search_vector НЕ входит — как и material, и year_created: вектор
-- покрывает прозу (name/master_name/short_description/raw_history). Если однажды понадобится
-- их проиндексировать, ADD COLUMN IF NOT EXISTS выражение уже созданной GENERATED-колонки
-- молча НЕ поменяет — нужен ALTER COLUMN ... SET EXPRESSION (PG17) либо DROP+ADD с
-- пересозданием idx_exhibits_search, и правка всех трёх копий выражения (здесь, в CREATE
-- TABLE выше и в app/models.py::_EXHIBIT_TSV).
ALTER TABLE exhibits ADD COLUMN IF NOT EXISTS search_vector tsvector GENERATED ALWAYS AS (
    setweight(to_tsvector('russian', coalesce(name,'')), 'A') ||
    setweight(to_tsvector('russian', coalesce(master_name,'') || ' ' || coalesce(exhibit_number,'')), 'B') ||
    setweight(to_tsvector('russian', coalesce(short_description,'')), 'C') ||
    setweight(to_tsvector('russian', coalesce(raw_history,'')), 'D')
) STORED;

-- Галерея изображений экспоната ----------------------------------------------
CREATE TABLE IF NOT EXISTS exhibit_images (
    id         SERIAL PRIMARY KEY,
    exhibit_id INT NOT NULL REFERENCES exhibits(id) ON DELETE CASCADE,
    url        TEXT NOT NULL,
    alt        VARCHAR(255),
    width      INT,
    height     INT,
    is_primary BOOLEAN NOT NULL DEFAULT false,
    position   INT NOT NULL DEFAULT 0
);

-- Кэш вопросов-подсказок ИИ-гида ---------------------------------------------
-- Вопросы под рассказом («Кому подарили это яйцо?») зависят только от карточки
-- экспоната, а генерировались на КАЖДЫЙ открытый экспонат и на КАЖДУЮ реплику
-- диалога — вторым вызовом LLM рядом с рассказом/ответом. Один и тот же
-- экспонат за день открывают десятки посетителей, и текст вопросов при этом
-- каждый раз оплачивается заново. Здесь он хранится.
--
-- source_hash — sha256 от текста, из которого вопросы сгенерированы (то же, что
-- уходит в промпт: raw_history / short_description / name после чистки от
-- ссылок-источников) плюс язык. Отредактировал музей описание — хэш разошёлся,
-- запись считается устаревшей и перегенерируется сама, без ручной инвалидации.
-- TTL нет намеренно: описание меняется редко, а «протухание по времени» просто
-- вернуло бы часть расхода.
CREATE TABLE IF NOT EXISTS exhibit_questions (
    exhibit_id  INT         NOT NULL REFERENCES exhibits(id) ON DELETE CASCADE,
    language    VARCHAR(8)  NOT NULL DEFAULT 'ru',
    -- Список строк по порядку показа. Храним пул (GUIDE_QUESTIONS_CACHE_SIZE),
    -- а отдаём срез под max_questions запроса: /guide/story просит 4,
    -- /guide/chat — 3, и разные лимиты не должны бить друг другу кэш.
    questions   JSONB       NOT NULL,
    source_hash CHAR(64)    NOT NULL,
    model       VARCHAR(128),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (exhibit_id, language)
);

-- Диалоги с ИИ-гидом ---------------------------------------------------------
CREATE TABLE IF NOT EXISTS guide_sessions (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    context       JSONB,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_activity TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS guide_messages (
    id          BIGSERIAL PRIMARY KEY,
    session_id  UUID NOT NULL REFERENCES guide_sessions(id) ON DELETE CASCADE,
    role        VARCHAR(16) NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
    content     TEXT NOT NULL,
    -- Смог ли гид ответить (§4 ТЗ 03.08.2026). Проставляется в момент генерации
    -- ответа, не постфактум. NULL — признак не проставлен (сообщения до миграции
    -- 2026-08-03; разовый бэкфилл — scripts/backfill_unanswered.py).
    -- Пишется на ОБЕ строки пары (вопрос и ответ), чтобы отчёт «вопросы без
    -- ответа» читал role='user' без self-join.
    answered    BOOLEAN,
    -- Причина, по которой ответа не получилось. 'llm_hedge' (31.08.2026) — это
    -- НЕ отказ, а содержательный ответ с оговоркой «этого точно не знаю»:
    -- отдельная причина нужна потому, что 'llm_refusal'/'no_context' кормят
    -- глобальную память отказов (app/crud.py: exhibit_refused_questions) и
    -- прячут вопрос из подсказок у всех посетителей — см. guide_intel.is_hard_refusal.
    fail_reason VARCHAR(32) CHECK (fail_reason IS NULL OR fail_reason IN
                     ('no_context', 'llm_refusal', 'llm_hedge', 'not_found', 'error')),
    -- Контекст вопроса: у какого экспоната/зала посетитель спрашивал.
    exhibit_id  INT,
    hall_id     INT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Телеметрия (источник для административной аналитики) ------------------------
-- Персональных данных не содержит: session_id и device_id — случайные UUID,
-- генерируются на клиенте; props принимается по белому списку ключей
-- (app/schemas.py: EVENT_PROPS_ALLOWED). См. docs/analytics-privacy.md.
CREATE TABLE IF NOT EXISTS events (
    id          BIGSERIAL PRIMARY KEY,
    session_id  UUID,
    type        VARCHAR(32) NOT NULL,
    exhibit_id  INT,
    hall_id     INT,
    showcase_id INT,
    label_slug  VARCHAR(100),
    -- Анонимный постоянный идентификатор устройства (localStorage) — только для
    -- метрики повторных визитов (§7). Ни с чем персональным не связывается.
    device_id   UUID,
    props       JSONB,
    ts          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Идемпотентная миграция для существующих БД (см. пояснение у halls выше).
ALTER TABLE guide_messages ADD COLUMN IF NOT EXISTS answered    BOOLEAN;
ALTER TABLE guide_messages ADD COLUMN IF NOT EXISTS fail_reason VARCHAR(32);
ALTER TABLE guide_messages ADD COLUMN IF NOT EXISTS exhibit_id  INT;
ALTER TABLE guide_messages ADD COLUMN IF NOT EXISTS hall_id     INT;
ALTER TABLE guide_messages DROP CONSTRAINT IF EXISTS guide_messages_fail_reason_chk;
ALTER TABLE guide_messages ADD CONSTRAINT guide_messages_fail_reason_chk
    CHECK (fail_reason IS NULL OR fail_reason IN
           ('no_context', 'llm_refusal', 'llm_hedge', 'not_found', 'error'));
ALTER TABLE events ADD COLUMN IF NOT EXISTS showcase_id INT;
ALTER TABLE events ADD COLUMN IF NOT EXISTS device_id   UUID;

-- Ночной пересчёт агрегатов (§12) ---------------------------------------------
-- Плоский суточный срез: метрика × измерение × день. Джоб идемпотентен —
-- пересчёт за дату перезаписывает строки по первичному ключу.
-- dimension_key = '' означает метрику без разреза (NULL в PK недопустим).
CREATE TABLE IF NOT EXISTS analytics_daily (
    date          DATE             NOT NULL,
    metric        VARCHAR(64)      NOT NULL,
    dimension_key VARCHAR(128)     NOT NULL DEFAULT '',
    dimension_id  INT,
    value         DOUBLE PRECISION NOT NULL DEFAULT 0,
    updated_at    TIMESTAMPTZ      NOT NULL DEFAULT now(),
    PRIMARY KEY (date, metric, dimension_key)
);

-- Кэш готовых отчётов за период. Отчёты с кластеризацией вопросов и разбором
-- последовательностей событий в плоскую суточную схему не ложатся.
-- period_key = '<from>:<to>' с пустыми частями для открытых границ.
CREATE TABLE IF NOT EXISTS analytics_reports (
    report      VARCHAR(32) NOT NULL,
    period_key  VARCHAR(64) NOT NULL,
    period_from DATE,
    period_to   DATE,
    payload     JSONB       NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (report, period_key)
);

-- Индексы --------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_showcases_hall       ON showcases(hall_id);
CREATE INDEX IF NOT EXISTS idx_exhibits_showcase    ON exhibits(showcase_id);
CREATE INDEX IF NOT EXISTS idx_exhibit_images_exh   ON exhibit_images(exhibit_id);
CREATE INDEX IF NOT EXISTS idx_guide_messages_sess  ON guide_messages(session_id);
-- Прогрев каталога и отчёт «сколько карточек без вопросов» ходят по свежести записи.
CREATE INDEX IF NOT EXISTS idx_exhibit_questions_updated ON exhibit_questions(updated_at);
CREATE INDEX IF NOT EXISTS idx_events_type_ts       ON events(type, ts);
CREATE INDEX IF NOT EXISTS idx_events_session_ts    ON events(session_id, ts);  -- аналитика по сессиям (C17/C18)
-- Аналитика 03.08.2026 (§2): events не чистится и растёт линейно от посещаемости,
-- без этих индексов все отчёты читают таблицу полным сканом.
CREATE INDEX IF NOT EXISTS idx_events_ts            ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_device        ON events(device_id)   WHERE device_id   IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_events_exhibit_type  ON events(exhibit_id, type) WHERE exhibit_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_events_hall_type     ON events(hall_id, type)    WHERE hall_id    IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_events_showcase      ON events(showcase_id) WHERE showcase_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_guide_messages_unanswered
    ON guide_messages(created_at) WHERE answered IS FALSE AND role = 'user';
-- Память об отказах (баг-репорт 31.08.2026, п. II-3): вопрос, на который гид уже
-- не смог ответить по ЭТОМУ экспонату, больше не предлагается в подсказках.
-- Запрос идёт на КАЖДОЙ реплике диалога об экспонате, а guide_messages не
-- чистится и растёт линейно от посещаемости — без индекса это полный скан на
-- каждый вопрос посетителя. idx_guide_messages_unanswered здесь не помогает:
-- он ведёт по created_at, а нам нужен вход по конкретному exhibit_id.
CREATE INDEX IF NOT EXISTS idx_guide_messages_refused
    ON guide_messages(exhibit_id, created_at)
    WHERE role = 'user' AND answered IS FALSE AND exhibit_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_analytics_daily_metric   ON analytics_daily(metric, date);
CREATE INDEX IF NOT EXISTS idx_analytics_reports_updated ON analytics_reports(updated_at);
CREATE INDEX IF NOT EXISTS idx_halls_name_trgm      ON halls    USING gin (name gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_halls_is_temporary    ON halls(is_temporary);
CREATE INDEX IF NOT EXISTS idx_halls_is_service      ON halls(is_service);
CREATE INDEX IF NOT EXISTS idx_halls_sort_order      ON halls(sort_order);
CREATE INDEX IF NOT EXISTS idx_exhibits_name_trgm   ON exhibits USING gin (name gin_trgm_ops);
-- Полнотекстовый поиск (B8/C27): GIN по сгенерированным tsvector-колонкам.
CREATE INDEX IF NOT EXISTS idx_halls_search         ON halls    USING gin (search_vector);
CREATE INDEX IF NOT EXISTS idx_exhibits_search      ON exhibits USING gin (search_vector);

-- Триггеры updated_at --------------------------------------------------------
DROP TRIGGER IF EXISTS trg_halls_updated     ON halls;
DROP TRIGGER IF EXISTS trg_showcases_updated ON showcases;
DROP TRIGGER IF EXISTS trg_exhibits_updated  ON exhibits;

CREATE TRIGGER trg_halls_updated     BEFORE UPDATE ON halls
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_showcases_updated BEFORE UPDATE ON showcases
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_exhibits_updated  BEFORE UPDATE ON exhibits
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

COMMIT;
