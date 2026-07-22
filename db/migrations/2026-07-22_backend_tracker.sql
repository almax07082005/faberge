-- ============================================================================
--  Миграция: закрытие бэкенд-трекера (B3, B4, B8/C27)
--
--  Задачи:
--    B3  — номер экспоната по путеводителю: колонка exhibits.exhibit_number
--          (VARCHAR — в путеводителе бывают номера вида «12а»). Отдаётся в
--          ExhibitSummary / Exhibit / ExhibitAdmin, показывается перед названием.
--    B4  — видео экспоната (C22): колонка exhibits.video_url (галерея images[]
--          в публичной карточке уже отдавалась; здесь добавляется поле под видео).
--    B8/C27 — полнотекстовый поиск с ранжированием: GENERATED-колонки
--          halls.search_vector / exhibits.search_vector (русская конфигурация,
--          взвешенная по важности поля) + GIN-индексы. Покрывает
--          short_description и raw_history (где упоминается, напр., Николай II).
--
--  Применение к живой БД (Yandex Managed PostgreSQL):
--    psql "$DATABASE_URL" -f db/migrations/2026-07-22_backend_tracker.sql
--  либо (тот же эффект, идемпотентно) переприменить схему:
--    python scripts/init_db.py
--
--  Идемпотентна и обратима (см. секцию «Откат» ниже).
-- ============================================================================

BEGIN;

-- B3 — номер экспоната по путеводителю ----------------------------------------
ALTER TABLE exhibits ADD COLUMN IF NOT EXISTS exhibit_number VARCHAR(32);

-- B4 — видео экспоната --------------------------------------------------------
ALTER TABLE exhibits ADD COLUMN IF NOT EXISTS video_url TEXT;

-- B8/C27 — полнотекстовый поиск -----------------------------------------------
-- Все функции (to_tsvector(regconfig,text), setweight, ||) immutable, поэтому
-- допустимы в GENERATED ALWAYS ... STORED. Выражения совпадают с db/schema.sql
-- и app/models.py (_HALL_TSV / _EXHIBIT_TSV) — держать в синхроне!
ALTER TABLE halls ADD COLUMN IF NOT EXISTS search_vector tsvector GENERATED ALWAYS AS (
    setweight(to_tsvector('russian', coalesce(name,'')), 'A') ||
    setweight(to_tsvector('russian', coalesce(description,'')), 'C')
) STORED;

ALTER TABLE exhibits ADD COLUMN IF NOT EXISTS search_vector tsvector GENERATED ALWAYS AS (
    setweight(to_tsvector('russian', coalesce(name,'')), 'A') ||
    setweight(to_tsvector('russian', coalesce(master_name,'') || ' ' || coalesce(exhibit_number,'')), 'B') ||
    setweight(to_tsvector('russian', coalesce(short_description,'')), 'C') ||
    setweight(to_tsvector('russian', coalesce(raw_history,'')), 'D')
) STORED;

CREATE INDEX IF NOT EXISTS idx_halls_search    ON halls    USING gin (search_vector);
CREATE INDEX IF NOT EXISTS idx_exhibits_search ON exhibits USING gin (search_vector);

COMMIT;

-- ── Откат ────────────────────────────────────────────────────────────────────
-- BEGIN;
-- DROP INDEX IF EXISTS idx_exhibits_search;
-- DROP INDEX IF EXISTS idx_halls_search;
-- ALTER TABLE exhibits DROP COLUMN IF EXISTS search_vector;
-- ALTER TABLE halls    DROP COLUMN IF EXISTS search_vector;
-- ALTER TABLE exhibits DROP COLUMN IF EXISTS video_url;
-- ALTER TABLE exhibits DROP COLUMN IF EXISTS exhibit_number;
-- COMMIT;
