-- ============================================================================
--  Миграция: порядок залов (C11) + spoken-описание экспоната для TTS (E15)
--            + индекс под аналитику по сессиям (C17/C18)
--
--  Задачи:
--    C11 — drag-n-drop порядок блоков (залов): колонка halls.sort_order.
--    E15 — озвучка с числами прописью: колонка exhibits.short_description_spoken
--          (LLM переписывает «Александр III» → «Александр Третий», «XIX век» →
--           «девятнадцатый век»); на фронт уходят цифры, в TTS — прописью.
--    C17/C18 — аналитика длительности сессии и маршрута по залам: индекс
--          events(session_id, ts) ускоряет GROUP BY session_id + ORDER BY ts.
--
--  Применение к живой БД (Yandex Managed PostgreSQL):
--    psql "$DATABASE_URL" -f db/migrations/2026-07-15_analytics_reorder_tts.sql
--  либо (тот же эффект, идемпотентно) переприменить схему:
--    python scripts/init_db.py
--
--  Идемпотентна и обратима (см. секцию REVERT ниже).
-- ============================================================================

BEGIN;

-- C11 — порядок залов ---------------------------------------------------------
ALTER TABLE halls ADD COLUMN IF NOT EXISTS sort_order INT NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS idx_halls_sort_order ON halls(sort_order);
-- Первичный порядок = hall_number. Reorder присваивает 1..N, поэтому уже
-- переставленные залы (sort_order > 0) не перетираются.
UPDATE halls SET sort_order = hall_number WHERE sort_order = 0;

-- E15 — spoken-описание экспоната для синтеза речи -----------------------------
ALTER TABLE exhibits ADD COLUMN IF NOT EXISTS short_description_spoken TEXT;

-- C17/C18 — индекс под сессионную аналитику ------------------------------------
CREATE INDEX IF NOT EXISTS idx_events_session_ts ON events(session_id, ts);

COMMIT;

-- ── Откат ────────────────────────────────────────────────────────────────────
-- BEGIN;
-- DROP INDEX IF EXISTS idx_events_session_ts;
-- ALTER TABLE exhibits DROP COLUMN IF EXISTS short_description_spoken;
-- DROP INDEX IF EXISTS idx_halls_sort_order;
-- ALTER TABLE halls DROP COLUMN IF EXISTS sort_order;
-- COMMIT;
