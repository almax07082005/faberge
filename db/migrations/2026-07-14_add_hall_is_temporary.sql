-- ============================================================================
--  Миграция: маркер «временная выставка» на уровне зала
--  Задача (правки заказчика, раунд 1): тег/фильтр «Временная выставка» в каталоге,
--  переключатель в админке, две кнопки на главной («Основная экспозиция» /
--  «Временная выставка»), ведущие в разные каталоги.
--
--  Применение к живой БД (Yandex Managed PostgreSQL):
--    psql "$DATABASE_URL" -f db/migrations/2026-07-14_add_hall_is_temporary.sql
--  либо (тот же эффект, идемпотентно) переприменить схему:
--    python scripts/init_db.py
--
--  Идемпотентна и обратима (см. секцию REVERT ниже). Дефолт false — все
--  существующие залы остаются основной экспозицией, пока админ не отметит зал.
-- ============================================================================

BEGIN;

ALTER TABLE halls ADD COLUMN IF NOT EXISTS is_temporary BOOLEAN NOT NULL DEFAULT false;

CREATE INDEX IF NOT EXISTS idx_halls_is_temporary ON halls(is_temporary);

-- Разметка данных под текущую экспозицию Музея Фаберже: «Выставочный зал» —
-- пространство сменных тематических выставок. Правьте под факт на месте.
UPDATE halls SET is_temporary = true WHERE name ILIKE '%выставочный зал%';

COMMIT;

-- ── Откат ────────────────────────────────────────────────────────────────────
-- BEGIN;
-- DROP INDEX IF EXISTS idx_halls_is_temporary;
-- ALTER TABLE halls DROP COLUMN IF EXISTS is_temporary;
-- COMMIT;
