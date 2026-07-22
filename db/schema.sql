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
    hall_number     INT  NOT NULL UNIQUE,
    name            VARCHAR(255),
    description     TEXT,                       -- описание зала (план/путеводитель)
    level           INT,                        -- этаж/уровень в здании
    cover_image_url TEXT,
    is_temporary    BOOLEAN NOT NULL DEFAULT false,  -- зал временной выставки (vs основная экспозиция)
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
ALTER TABLE halls ADD COLUMN IF NOT EXISTS sort_order   INT NOT NULL DEFAULT 0;
ALTER TABLE halls ADD COLUMN IF NOT EXISTS search_vector tsvector GENERATED ALWAYS AS (
    setweight(to_tsvector('russian', coalesce(name,'')), 'A') ||
    setweight(to_tsvector('russian', coalesce(description,'')), 'C')
) STORED;

-- Бэкофилл порядка для залов, которым его ещё не задавали (sort_order = 0):
-- берём hall_number как естественный первичный порядок. Reorder присваивает
-- значения 1..N, поэтому переставленные админом залы сюда уже не попадут.
UPDATE halls SET sort_order = hall_number WHERE sort_order = 0;

-- Витрины --------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS showcases (
    id              SERIAL PRIMARY KEY,
    hall_id         INT NOT NULL REFERENCES halls(id) ON DELETE CASCADE,
    showcase_number INT NOT NULL,
    name            VARCHAR(255),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (hall_id, showcase_number)
);

-- Экспонаты ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS exhibits (
    id                SERIAL PRIMARY KEY,
    showcase_id       INT REFERENCES showcases(id) ON DELETE CASCADE,
    label_slug        VARCHAR(100) UNIQUE,      -- класс, который возвращает YOLO
    exhibit_number    VARCHAR(32),              -- номер по путеводителю музея (B3): перед названием в каталоге
    name              VARCHAR(255) NOT NULL,
    year_created      INT,
    master_name       VARCHAR(255),
    material          VARCHAR(255),
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

-- Диалоги с ИИ-гидом ---------------------------------------------------------
CREATE TABLE IF NOT EXISTS guide_sessions (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    context       JSONB,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_activity TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS guide_messages (
    id         BIGSERIAL PRIMARY KEY,
    session_id UUID NOT NULL REFERENCES guide_sessions(id) ON DELETE CASCADE,
    role       VARCHAR(16) NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
    content    TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Телеметрия (источник для административной аналитики) ------------------------
CREATE TABLE IF NOT EXISTS events (
    id         BIGSERIAL PRIMARY KEY,
    session_id UUID,
    type       VARCHAR(32) NOT NULL,
    exhibit_id INT,
    hall_id    INT,
    label_slug VARCHAR(100),
    props      JSONB,
    ts         TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Индексы --------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_showcases_hall       ON showcases(hall_id);
CREATE INDEX IF NOT EXISTS idx_exhibits_showcase    ON exhibits(showcase_id);
CREATE INDEX IF NOT EXISTS idx_exhibit_images_exh   ON exhibit_images(exhibit_id);
CREATE INDEX IF NOT EXISTS idx_guide_messages_sess  ON guide_messages(session_id);
CREATE INDEX IF NOT EXISTS idx_events_type_ts       ON events(type, ts);
CREATE INDEX IF NOT EXISTS idx_events_session_ts    ON events(session_id, ts);  -- аналитика по сессиям (C17/C18)
CREATE INDEX IF NOT EXISTS idx_halls_name_trgm      ON halls    USING gin (name gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_halls_is_temporary    ON halls(is_temporary);
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
