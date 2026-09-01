-- =============================================================================
-- Миграция: новая причина неудачи ответа гида — 'llm_hedge'
-- (баг-репорт 31.08.2026, п. II-3; замечание проверяющих Ж1 по второй волне)
--
-- Проблема
--   Признак «гид не ответил» ставился одним широким предикатом
--   (app/services/guide_intel.is_refusal): маркер «не знаю» / «нет данных» /
--   «не могу сказать» искался ГДЕ УГОДНО в тексте ответа. Пока признак кормил
--   только отчёт /admin/analytics/unanswered, ширина была полезна и безопасна:
--   лишнюю строку в отчёте видит сотрудник музея и отбрасывает.
--
--   С этим же релизом причины 'llm_refusal' и 'no_context' начали кормить
--   ГЛОБАЛЬНУЮ память отказов (решение Д8, см.
--   db/migrations/2026-08-31_guide_refusal_memory.sql): вопрос с такой причиной
--   перестаёт предлагаться ВСЕМ посетителям этого экспоната на
--   GUIDE_REFUSAL_MEMORY_DAYS дней. Ширина предиката превратилась из безобидной
--   в опасную: развёрнутый ответ с одной оговоркой «точной даты не знаю»
--   после двух повторов молча уносил из подсказок НОРМАЛЬНЫЙ вопрос. Хуже того,
--   переписанный этим же релизом промпт диалога (llm._CHAT_SYSTEM) сам просит
--   модель такие оговорки писать — то есть мы наказывали её за требуемое
--   поведение.
--
-- Что делает
--   Расширяет CHECK-ограничение guide_messages.fail_reason пятым значением
--   'llm_hedge' — «ответ по существу, но с оговоркой». Оно ставится, когда
--   guide_intel.is_refusal сработал, а строгий guide_intel.is_hard_refusal —
--   нет (маркер не в первой фразе либо у ответа есть содержательное
--   продолжение). В отчёт такая строка по-прежнему попадает (answered = FALSE),
--   а в выборку crud.exhibit_refused_questions — нет.
--
-- Данных не трогает
--   Ни одной строки не переписывает. Уже накопленные 'llm_refusal' остаются как
--   есть: перечитать по тексту, был это отказ целиком или оговорка, задним
--   числом можно (scripts/backfill_unanswered.py умеет и то и другое), но это
--   ОТДЕЛЬНОЕ решение и отдельный прогон — сама миграция ничего не
--   переклассифицирует.
--
-- Порядок применения
--   Строго ДО выкладки кода: код начинает писать 'llm_hedge' сразу, и на старом
--   ограничении такой INSERT упал бы с ошибкой CHECK на каждой реплике диалога,
--   где модель вежливо оговорилась.
--
--   psql "$DATABASE_URL" -f db/migrations/2026-08-31_guide_fail_reason_hedge.sql
--   (scripts/init_db.py применяет db/schema.sql целиком — на чистой базе
--    миграция не нужна.)
--
-- Идемпотентна (DROP IF EXISTS + ADD) и обратима — см. секцию «Откат».
-- =============================================================================

BEGIN;

ALTER TABLE guide_messages DROP CONSTRAINT IF EXISTS guide_messages_fail_reason_chk;
ALTER TABLE guide_messages ADD CONSTRAINT guide_messages_fail_reason_chk
    CHECK (fail_reason IS NULL OR fail_reason IN
           ('no_context', 'llm_refusal', 'llm_hedge', 'not_found', 'error'));

COMMIT;

-- ── Проверка (psql -f печатает результат прямо в консоль) ────────────────────

-- Ожидаем определение ограничения с пятью значениями, включая llm_hedge.
SELECT pg_get_constraintdef(oid) AS definition
  FROM pg_constraint
 WHERE conname = 'guide_messages_fail_reason_chk';

-- Распределение причин на сегодня. Сразу после выкладки кода часть того, что
-- раньше уезжало в 'llm_refusal', начнёт приходить как 'llm_hedge' — это и есть
-- та доля, которая ошибочно кормила бы память отказов.
SELECT fail_reason, count(*) AS messages
  FROM guide_messages
 WHERE role = 'user'
   AND answered IS FALSE
 GROUP BY fail_reason
 ORDER BY messages DESC;

-- ── Откат ────────────────────────────────────────────────────────────────────
-- ВНИМАНИЕ: сузить ограничение обратно можно только после того, как в таблице
-- не останется строк с 'llm_hedge', иначе ADD CONSTRAINT не пройдёт валидацию.
-- Откатывать нужно вместе с кодом (или перед ним).
--
-- BEGIN;
-- UPDATE guide_messages SET fail_reason = 'llm_refusal' WHERE fail_reason = 'llm_hedge';
-- ALTER TABLE guide_messages DROP CONSTRAINT IF EXISTS guide_messages_fail_reason_chk;
-- ALTER TABLE guide_messages ADD CONSTRAINT guide_messages_fail_reason_chk
--     CHECK (fail_reason IS NULL OR fail_reason IN
--            ('no_context', 'llm_refusal', 'not_found', 'error'));
-- COMMIT;
