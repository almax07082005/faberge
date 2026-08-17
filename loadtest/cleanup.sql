-- Удаление следов нагрузочного прогона.
--
-- ЗАЧЕМ. События телеметрии по требованию заказчика не удаляются (README,
-- §«Аналитика посетителей»), поэтому нагрузочный прогон навсегда испортил бы
-- отчёты: сотни фальшивых визитов, распознаваний и вопросов гиду. Пометить
-- событие «своим» полем нельзя — props режется белым списком (schemas.py §10).
-- Поэтому маркер зашит в первую группу UUID: все session_id и device_id
-- прогона начинаются с 10adfe57 (loadtest/lib/fixtures.js, MARKER).
--
-- КОГДА. Сразу после прогона, до ночного пересчёта аналитики (cron 0 4 * * *).
-- Если пересчёт уже прошёл — после очистки перегнать агрегаты:
--     python scripts/rebuild_analytics.py --from <дата> --to <дата>
--
-- КАК:
--     psql "$DATABASE_URL_PSQL" -f loadtest/cleanup.sql
--
-- Сначала посмотреть, сколько удалится (безопасно, ничего не меняет):
--     psql "$DATABASE_URL_PSQL" -f loadtest/cleanup.sql -v ONLY_COUNT=1

\set marker '10adfe57-%'

BEGIN;

-- ── Сколько нашлось ──────────────────────────────────────────────────────────
SELECT 'events (device_id)'    AS what, count(*) FROM events         WHERE device_id::text  LIKE :'marker'
UNION ALL
SELECT 'events (session_id)',        count(*) FROM events            WHERE session_id::text LIKE :'marker'
UNION ALL
SELECT 'guide_messages',             count(*) FROM guide_messages    WHERE session_id::text LIKE :'marker'
UNION ALL
SELECT 'guide_sessions',             count(*) FROM guide_sessions    WHERE id::text         LIKE :'marker';

-- ── Удаление ─────────────────────────────────────────────────────────────────
DELETE FROM events
 WHERE device_id::text  LIKE :'marker'
    OR session_id::text LIKE :'marker';

-- guide_messages уедут каскадом по FK (ondelete='CASCADE'), но удаляем явно:
-- сообщение могло уехать в сессию, созданную не прогоном.
DELETE FROM guide_messages WHERE session_id::text LIKE :'marker';
DELETE FROM guide_sessions WHERE id::text         LIKE :'marker';

-- Проверить и только потом COMMIT.
COMMIT;

-- ── Что НЕ чистится ──────────────────────────────────────────────────────────
-- analytics_daily и analytics_reports — производные. Если ночной джоб успел
-- посчитать день с нагрузочными данными, перегнать его после очистки:
--     python scripts/rebuild_analytics.py --from <дата> --to <дата>
