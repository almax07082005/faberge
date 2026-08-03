-- ============================================================================
--  Миграция: аналитика посетителей (ТЗ [BE] от 03.08.2026)
--
--  Задачи:
--    §2 — новые колонки телеметрии и индексы под аналитику:
--         • events.showcase_id — витрина, с которой пришло событие (showcase_view
--           и exhibit_view из витрины); раньше уровень витрины терялся;
--         • events.device_id — АНОНИМНЫЙ постоянный идентификатор устройства
--           (localStorage на фронте). Нужен только для «повторных визитов» (§7):
--           session_id живёт в sessionStorage, поэтому новая вкладка = новый
--           «человек». Со справочниками админов/пользователей НЕ связывается —
--           см. docs/analytics-privacy.md;
--         • индексы: до этой миграции все аналитические отчёты читали events
--           полным сканом. Таблица по требованию заказчика не чистится, то есть
--           растёт линейно от посещаемости.
--    §4 — признак «гид не смог ответить» на сообщениях диалога:
--         • guide_messages.answered / fail_reason — проставляются В МОМЕНТ
--           генерации ответа (app/routers/guide.py), а не постфактум;
--         • guide_messages.exhibit_id / hall_id — контекст вопроса, чтобы отчёт
--           «вопросы без ответа» показывал, у какого экспоната не хватает
--           описания. Признак пишется на ОБЕ строки пары (вопрос пользователя и
--           ответ гида) — отчёт читает строку role='user' без self-join.
--    §12 — таблицы под ночной пересчёт агрегатов:
--         • analytics_daily — плоский суточный срез (метрика × измерение × день);
--         • analytics_reports — кэш готовых отчётов за период. Отчёты
--           «вопросы», «маршруты», «распознавание» в плоскую суточную схему не
--           ложатся (кластеризация и последовательности событий по сессии), для
--           них ТЗ прямо разрешает отдельное хранилище.
--
--  Применение к живой БД (Yandex Managed PostgreSQL):
--    psql "$DATABASE_URL" -f db/migrations/2026-08-03_analytics.sql
--  либо (тот же эффект, идемпотентно) переприменить схему:
--    python scripts/init_db.py
--
--  Идемпотентна и обратима (см. секцию «Откат» ниже).
--  Блокировок таблиц не создаёт: ADD COLUMN без DEFAULT в PG 11+ мгновенный,
--  индексы на боевой БД лучше создавать CONCURRENTLY (см. примечание в конце).
-- ============================================================================

BEGIN;

-- §2 — новые колонки телеметрии ----------------------------------------------
ALTER TABLE events ADD COLUMN IF NOT EXISTS showcase_id INT;
ALTER TABLE events ADD COLUMN IF NOT EXISTS device_id   UUID;

-- §2 — индексы под аналитические выборки --------------------------------------
-- idx_events_type_ts и idx_events_session_ts уже созданы схемой (db/schema.sql,
-- миграция 2026-07-15) — в ТЗ они перечислены как ix_events_*, но это те же
-- самые индексы; второй экземпляр только удорожил бы вставку.
CREATE INDEX IF NOT EXISTS idx_events_ts           ON events (ts);
CREATE INDEX IF NOT EXISTS idx_events_device       ON events (device_id) WHERE device_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_events_exhibit_type ON events (exhibit_id, type) WHERE exhibit_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_events_hall_type    ON events (hall_id, type)    WHERE hall_id    IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_events_showcase     ON events (showcase_id)      WHERE showcase_id IS NOT NULL;

-- §4 — «вопросы без ответа гида» ----------------------------------------------
ALTER TABLE guide_messages ADD COLUMN IF NOT EXISTS answered    BOOLEAN;
ALTER TABLE guide_messages ADD COLUMN IF NOT EXISTS fail_reason VARCHAR(32);
ALTER TABLE guide_messages ADD COLUMN IF NOT EXISTS exhibit_id  INT;
ALTER TABLE guide_messages ADD COLUMN IF NOT EXISTS hall_id     INT;

-- answered = NULL означает «признак не проставлен» (сообщения, накопленные до
-- этой миграции). Разовый бэкфилл эвристикой: python scripts/backfill_unanswered.py
ALTER TABLE guide_messages DROP CONSTRAINT IF EXISTS guide_messages_fail_reason_chk;
ALTER TABLE guide_messages ADD CONSTRAINT guide_messages_fail_reason_chk
    CHECK (fail_reason IS NULL OR fail_reason IN ('no_context', 'llm_refusal', 'not_found', 'error'));

-- Отчёт читает только неотвеченные вопросы посетителя — частичный индекс.
CREATE INDEX IF NOT EXISTS idx_guide_messages_unanswered
    ON guide_messages (created_at) WHERE answered IS FALSE AND role = 'user';

-- §12 — суточный срез (плоские метрики) ---------------------------------------
--   metric        — 'events_total', 'sessions', 'exhibit_views', 'recognitions', …
--   dimension_key — ключ измерения ('' = метрика без разреза, иначе тип события,
--                   id экспоната/зала строкой). Пустая строка вместо NULL, т.к.
--                   NULL в первичном ключе PostgreSQL не допускает.
CREATE TABLE IF NOT EXISTS analytics_daily (
    date          DATE             NOT NULL,
    metric        VARCHAR(64)      NOT NULL,
    dimension_key VARCHAR(128)     NOT NULL DEFAULT '',
    dimension_id  INT,
    value         DOUBLE PRECISION NOT NULL DEFAULT 0,
    updated_at    TIMESTAMPTZ      NOT NULL DEFAULT now(),
    PRIMARY KEY (date, metric, dimension_key)
);
CREATE INDEX IF NOT EXISTS idx_analytics_daily_metric ON analytics_daily (metric, date);

-- §12 — кэш готовых отчётов за период ------------------------------------------
--   period_key — '<from>:<to>[:<параметры>]' с пустыми частями для открытых
--                границ ('2026-07-01:2026-07-31', ':2026-07-31', ':').
--                Отдельным полем, т.к. NULL в PK недопустим. Хвост с
--                параметрами (limit/order) разводит наборы аргументов, чтобы
--                отчёт с limit=50 не подменялся кэшем от limit=20.
CREATE TABLE IF NOT EXISTS analytics_reports (
    report      VARCHAR(32) NOT NULL,
    period_key  VARCHAR(64) NOT NULL,
    period_from DATE,
    period_to   DATE,
    payload     JSONB       NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (report, period_key)
);
CREATE INDEX IF NOT EXISTS idx_analytics_reports_updated ON analytics_reports (updated_at);

COMMIT;

-- ── Примечание по боевой БД ──────────────────────────────────────────────────
-- CREATE INDEX берёт SHARE-блокировку и на большой таблице events остановит
-- запись телеметрии на время построения. Если events уже велика, выполните
-- индексы отдельно, вне транзакции:
--   CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_events_ts ON events (ts);
--   … и так далее, затем прогоните файл — CREATE INDEX IF NOT EXISTS их пропустит.

-- ── Откат ────────────────────────────────────────────────────────────────────
-- BEGIN;
-- DROP TABLE IF EXISTS analytics_reports;
-- DROP TABLE IF EXISTS analytics_daily;
-- DROP INDEX IF EXISTS idx_guide_messages_unanswered;
-- ALTER TABLE guide_messages DROP CONSTRAINT IF EXISTS guide_messages_fail_reason_chk;
-- ALTER TABLE guide_messages DROP COLUMN IF EXISTS hall_id;
-- ALTER TABLE guide_messages DROP COLUMN IF EXISTS exhibit_id;
-- ALTER TABLE guide_messages DROP COLUMN IF EXISTS fail_reason;
-- ALTER TABLE guide_messages DROP COLUMN IF EXISTS answered;
-- DROP INDEX IF EXISTS idx_events_showcase;
-- DROP INDEX IF EXISTS idx_events_hall_type;
-- DROP INDEX IF EXISTS idx_events_exhibit_type;
-- DROP INDEX IF EXISTS idx_events_device;
-- DROP INDEX IF EXISTS idx_events_ts;
-- ALTER TABLE events DROP COLUMN IF EXISTS device_id;
-- ALTER TABLE events DROP COLUMN IF EXISTS showcase_id;
-- COMMIT;
