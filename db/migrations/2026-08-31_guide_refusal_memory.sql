-- =============================================================================
-- Миграция: индекс под память об отказах ИИ-гида (баг-репорт 31.08.2026, п. II-3)
--
-- Проблема
--   Дословно из письма музея: «существуют принципиальные сбои в работе
--   алгоритма, например, AI гид предлагает задать вопрос, на который не знает
--   ответа». Цепочка со скриншотов: гид предложил «Как функционирует механизм
--   со стрелкой-змейкой?» → посетитель спросил → «К сожалению, я не знаю…» → и
--   СНОВА предлагает «Как функционирует механизм вращающейся стрелки-змейки?».
--
-- Что делает
--   Ничего не добавляет в данные: отказ уже пишется в guide_messages
--   (answered = FALSE, fail_reason = 'llm_refusal' | 'no_context', exhibit_id) —
--   это сделано ещё 03.08.2026 ради отчёта /admin/analytics/unanswered. Новой
--   таблицы под «память» не нужно, нужен только вход в уже существующие данные
--   по экспонату.
--
--   Индекс частичный (WHERE role = 'user' AND answered IS FALSE): отказы —
--   малая доля таблицы, и полный индекс по exhibit_id был бы в разы толще без
--   пользы. Ведущая колонка exhibit_id, вторая created_at — запрос всегда
--   «отказы по ЭТОМУ экспонату не старше GUIDE_REFUSAL_MEMORY_DAYS».
--
-- Зачем индекс вообще
--   Запрос идёт на КАЖДОЙ реплике диалога об экспонате, а guide_messages по
--   требованию заказчика не чистится и растёт линейно от посещаемости. Уже
--   существующий idx_guide_messages_unanswered здесь не помогает: он ведёт по
--   created_at (для отчёта за период), а нам нужен вход по конкретному
--   exhibit_id.
--
-- Чего НЕ делает
--   Не меняет ни одной колонки и ни одной строки. Старый код с этим индексом
--   работает без изменений — он его просто не использует.
--
-- Применение к живой БД
--   psql "$DATABASE_URL" -f db/migrations/2026-08-31_guide_refusal_memory.sql
--   (scripts/init_db.py применяет db/schema.sql целиком — на чистой базе
--    миграция не нужна.)
--
-- Идемпотентна и обратима — см. секцию «Откат» ниже.
-- =============================================================================

BEGIN;

CREATE INDEX IF NOT EXISTS idx_guide_messages_refused
    ON guide_messages(exhibit_id, created_at)
    WHERE role = 'user' AND answered IS FALSE AND exhibit_id IS NOT NULL;

COMMIT;

-- ── Проверка (psql -f печатает результат прямо в консоль) ────────────────────

-- Ожидаем строку idx_guide_messages_refused.
SELECT indexname
  FROM pg_indexes
 WHERE tablename = 'guide_messages'
 ORDER BY indexname;

-- Сколько отказов уже накоплено и по скольким экспонатам — это ровно тот
-- материал, из которого сразу после деплоя начнёт работать память об отказах.
SELECT count(*)                    AS refusals,
       count(DISTINCT exhibit_id)  AS exhibits_touched
  FROM guide_messages
 WHERE role = 'user'
   AND answered IS FALSE
   AND fail_reason IN ('llm_refusal', 'no_context')
   AND exhibit_id IS NOT NULL;

-- Формулировки, которые перестанут предлагаться сразу (порог по умолчанию —
-- GUIDE_REFUSAL_MEMORY_MIN_COUNT = 2 отказа по одному экспонату за 90 дней).
SELECT exhibit_id, trim(content) AS question, count(*) AS refusals
  FROM guide_messages
 WHERE role = 'user'
   AND answered IS FALSE
   AND fail_reason IN ('llm_refusal', 'no_context')
   AND exhibit_id IS NOT NULL
   AND created_at >= now() - INTERVAL '90 days'
 GROUP BY exhibit_id, trim(content)
HAVING count(*) >= 2
 ORDER BY refusals DESC
 LIMIT 50;

-- ── Откат ────────────────────────────────────────────────────────────────────
-- Безопасен: индекс не хранит данных. Отключать память об отказах ради отката
-- индекса не нужно — для этого есть GUIDE_REFUSAL_MEMORY_ENABLED=false, и он
-- убирает сам запрос.
--
-- BEGIN;
-- DROP INDEX IF EXISTS idx_guide_messages_refused;
-- COMMIT;
