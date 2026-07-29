-- ============================================================================
--  Миграция: баг-репорт заказчика от 28.07.2026 (пп. 4–5) — каталог залов и витрин
--
--  Задачи:
--    п.5 — чистка каталога залов:
--          • halls.hall_number становится NULLABLE — зал «Вне постоянной
--            экспозиции» заказчик просил показывать БЕЗ номера («зал № 99» в
--            подписях и в ответах гида недопустим);
--          • halls.is_service — служебная запись (Парадная лестница и т.п.):
--            хранится в каталоге, но не попадает ни в GET /halls, ни на карту,
--            ни в ответы гида. Удалять запись не обязательно — при служебном
--            флаге привязанные экспонаты остаются доступными по прямым ссылкам.
--    п.4 — витрины по путеводителю:
--          • showcases.showcase_number становится NULLABLE — это группа «не в
--            витринах» (в путеводителе отмечена пустым квадратом): экспонаты
--            зала, стоящие вне витрин. Такая группа в зале одна — гарантирует
--            частичный уникальный индекс (обычный UNIQUE(hall_id, showcase_number)
--            в PostgreSQL допускает сколько угодно строк с NULL).
--
--  Данные (какие именно записи убрать/переименовать) миграция НЕ трогает —
--  это отдельный шаг с проверкой привязанных экспонатов:
--      python scripts/cleanup_hall_catalog.py --dry-run
--      python scripts/cleanup_hall_catalog.py --apply
--
--  Применение к живой БД (Yandex Managed PostgreSQL):
--    psql "$DATABASE_URL" -f db/migrations/2026-07-29_bugreport_catalog.sql
--  либо (тот же эффект, идемпотентно) переприменить схему:
--    python scripts/init_db.py
--
--  Идемпотентна и обратима (см. секцию «Откат» ниже).
-- ============================================================================

BEGIN;

-- п.5 — зал без номера --------------------------------------------------------
ALTER TABLE halls ALTER COLUMN hall_number DROP NOT NULL;

-- п.5 — служебный зал ---------------------------------------------------------
ALTER TABLE halls ADD COLUMN IF NOT EXISTS is_service BOOLEAN NOT NULL DEFAULT false;
CREATE INDEX IF NOT EXISTS idx_halls_is_service ON halls(is_service);

-- п.4 — группа «не в витринах» ------------------------------------------------
ALTER TABLE showcases ALTER COLUMN showcase_number DROP NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_showcases_hall_unnumbered
    ON showcases (hall_id) WHERE showcase_number IS NULL;

COMMIT;

-- ── Откат ────────────────────────────────────────────────────────────────────
-- Внимание: обратные ALTER ... SET NOT NULL упадут, если к моменту отката в
-- таблицах уже есть строки с NULL (зал без номера, группа «не в витринах»).
-- Сначала проставьте номера, потом откатывайте.
-- BEGIN;
-- DROP INDEX IF EXISTS uq_showcases_hall_unnumbered;
-- ALTER TABLE showcases ALTER COLUMN showcase_number SET NOT NULL;
-- DROP INDEX IF EXISTS idx_halls_is_service;
-- ALTER TABLE halls DROP COLUMN IF EXISTS is_service;
-- ALTER TABLE halls ALTER COLUMN hall_number SET NOT NULL;
-- COMMIT;
