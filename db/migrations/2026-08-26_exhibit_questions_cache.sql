-- =============================================================================
-- Миграция: кэш вопросов-подсказок на экспонаты (просьба заказчика 26.08.2026)
--
-- Проблема
--   Вопросы-подсказки («Кому подарили это яйцо?», «Что за сюрприз внутри?»)
--   считает отдельный вызов YandexGPT — operation=questions в логах расхода.
--   Он идёт ВТОРЫМ рядом с каждым рассказом (POST /guide/story) и рядом с
--   КАЖДОЙ репликой диалога об экспонате (POST /guide/chat). При этом результат
--   зависит только от карточки экспоната: ни от посетителя, ни от истории
--   диалога, ни от заданного вопроса. Один экспонат за день открывают десятки
--   раз — и каждый раз мы платим за один и тот же текст.
--
-- Что делает
--   Заводит таблицу exhibit_questions: (экспонат, язык) → список вопросов.
--   Ключ свежести — source_hash: sha256 текста, который уходит в промпт
--   (raw_history / short_description / name после чистки от ссылок-источников)
--   вместе с языком. Музей поправил описание — хэш разошёлся, запись сама
--   считается устаревшей и перегенерируется при первом же обращении. Ручной
--   инвалидации и TTL нет: TTL здесь просто вернул бы часть расхода.
--
--   Хранится ПУЛ вопросов (GUIDE_QUESTIONS_CACHE_SIZE, по умолчанию 6), а
--   отдаётся срез под max_questions запроса — /guide/story просит 4,
--   /guide/chat 3, и разные лимиты не выбивают кэш друг друга.
--
-- Чего НЕ делает
--   Не трогает exhibits и guide_messages: это отдельное хранилище, при удалении
--   экспоната запись уезжает по ON DELETE CASCADE. Не заполняет данные —
--   разовый прогрев каталога идёт отдельно и по желанию:
--     python scripts/warm_guide_questions.py            # сухой прогон
--     python scripts/warm_guide_questions.py --apply
--   либо POST /admin/guide/questions/warm (порциями, из админки).
--   Без прогрева всё работает как раньше — кэш просто наполняется по мере
--   обращений посетителей.
--
-- Применение к живой БД
--   psql "$DATABASE_URL" -f db/migrations/2026-08-26_exhibit_questions_cache.sql
--   (scripts/init_db.py применяет db/schema.sql целиком — на чистой базе
--    миграция не нужна.)
--
-- Идемпотентна и обратима — см. секцию «Откат» ниже.
-- =============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS exhibit_questions (
    exhibit_id  INT         NOT NULL REFERENCES exhibits(id) ON DELETE CASCADE,
    language    VARCHAR(8)  NOT NULL DEFAULT 'ru',
    questions   JSONB       NOT NULL,
    source_hash CHAR(64)    NOT NULL,
    model       VARCHAR(128),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (exhibit_id, language)
);

CREATE INDEX IF NOT EXISTS idx_exhibit_questions_updated ON exhibit_questions(updated_at);

COMMENT ON TABLE exhibit_questions IS
    'Кэш вопросов-подсказок ИИ-гида: (экспонат, язык) → список вопросов. '
    'Свежесть определяется source_hash, а не временем.';
COMMENT ON COLUMN exhibit_questions.questions IS
    'Пул вопросов по порядку показа (JSON-массив строк); отдаётся срез под max_questions запроса.';
COMMENT ON COLUMN exhibit_questions.source_hash IS
    'sha256 от языка и текста, ушедшего в промпт (raw_history / short_description / name '
    'после чистки от ссылок-источников). Расхождение = описание изменилось = перегенерировать.';
COMMENT ON COLUMN exhibit_questions.model IS
    'Чем сгенерировано: URI модели YandexGPT либо stub/heuristic (LLM не настроен).';

COMMIT;

-- ── Проверка (psql -f печатает результат прямо в консоль) ────────────────────

-- Ожидаем шесть строк описания колонок.
SELECT column_name, data_type, is_nullable
  FROM information_schema.columns
 WHERE table_name = 'exhibit_questions'
 ORDER BY ordinal_position;

-- Ожидаем 0: миграция только заводит таблицу, наполняет её прогрев или трафик.
SELECT count(*) AS cached_exhibits FROM exhibit_questions;

-- Объём работы для прогрева: карточки, у которых кэша ещё нет.
SELECT count(*) AS exhibits_without_questions
  FROM exhibits e
  LEFT JOIN exhibit_questions q ON q.exhibit_id = e.id AND q.language = 'ru'
 WHERE q.exhibit_id IS NULL;

-- ── Откат ────────────────────────────────────────────────────────────────────
-- Безопасен: таблица — только кэш, первичных данных в ней нет. После отката
-- (и с GUIDE_QUESTIONS_CACHE_ENABLED=false) гид снова считает вопросы каждый раз.
--
-- BEGIN;
-- DROP TABLE IF EXISTS exhibit_questions;
-- COMMIT;
