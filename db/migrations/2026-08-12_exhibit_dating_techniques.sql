-- =============================================================================
-- Миграция: датировка и техники экспоната отдельными полями
--           (баг-репорт заказчика 12.08.2026, п.5)
--
-- Проблема
--   Каталожная строка путеводителя устроена так:
--       «Санкт-Петербург, 1899–1903. Фирма К. Фаберже, мастер В. Аарне.
--        Золото, серебро, рубины, дерево; выпиловка, чеканка, гравировка, эмаль»
--   и целиком лежит текстом в short_description у 1048 карточек из 1252 на проде.
--   Разложить её по существующим колонкам мешают две вещи:
--
--   1. year_created — INT. В указателе 627 карточек датированы ДИАПАЗОНОМ
--      («1899–1903»), 100 — десятилетием («1880-е»), а ещё 105 вообще не имеют
--      арабского года («конец XIX — начало XX века»). В INT влезает только
--      нижняя граница, и «1899–1903» неотличимо от точного 1899.
--   2. Материалы и техники разделены в указателе точкой с запятой. Без отдельной
--      колонки техники либо теряются, либо (как сейчас у id 48 «Царские врата»)
--      попадают в material: «Серебро, Дерево, Левкас, Темпера, Тиснение».
--
-- Задачи
--   • dating     — датировка строкой, дословно как в указателе. year_created
--                  остаётся и хранит НИЖНЮЮ границу (или NULL у вековых датировок),
--                  чтобы сортировка и фильтры по году продолжали работать.
--   • techniques — техники (хвост строки после «;»), строчными, через «, ».
--
-- Что делает
--   Добавляет две nullable-колонки в exhibits. Данные не трогает: бэкфилл идёт
--   отдельно и обратимо — scripts/backfill_catalog_fields.py (сухой прогон по
--   умолчанию, --apply, файл отката, --rollback).
--
-- Чего НЕ делает
--   Не трогает search_vector. Полнотекстовый вектор покрывает прозу
--   (name/master_name/short_description/raw_history); material и year_created в
--   него не входят сознательно, dating и techniques — того же класса. Это важно
--   технически: на живой БД колонка search_vector уже существует, и
--   ADD COLUMN IF NOT EXISTS её выражение МОЛЧА НЕ ИЗМЕНИТ. Понадобится
--   индексировать — нужен ALTER COLUMN ... SET EXPRESSION (PostgreSQL 17,
--   полная перезапись таблицы) либо DROP+ADD с обязательным пересозданием
--   idx_exhibits_search, и синхронная правка всех трёх копий выражения:
--   db/schema.sql (CREATE TABLE), db/schema.sql (ALTER) и app/models.py::_EXHIBIT_TSV.
--
-- Применение к живой БД
--   psql "$DATABASE_URL" -f db/migrations/2026-08-12_exhibit_dating_techniques.sql
--   (scripts/init_db.py применяет db/schema.sql целиком и уже содержит эти ALTER'ы —
--    на чистой базе миграция не нужна.)
--
-- Идемпотентна и обратима — см. секцию «Откат» ниже.
-- =============================================================================

BEGIN;

-- Тип TEXT, а не VARCHAR(n): датировка вроде «1880-е — первая половина 1890-х.
-- Накладной знак: начало XX века» и перечень из полутора десятков техник в
-- 255 символов укладываются не всегда, а усечение здесь означало бы потерю
-- данных путеводителя.
ALTER TABLE exhibits ADD COLUMN IF NOT EXISTS dating TEXT;
ALTER TABLE exhibits ADD COLUMN IF NOT EXISTS techniques TEXT;

COMMENT ON COLUMN exhibits.dating IS
    'Датировка дословно как в путеводителе: «1899–1903», «1880-е», «конец XIX — начало XX века». '
    'year_created хранит нижнюю границу этого же периода (NULL, если арабского года нет).';
COMMENT ON COLUMN exhibits.techniques IS
    'Техники из хвоста каталожной строки (после «;»), строчными, через «, ». '
    'В material техникам не место — там только материалы.';

COMMIT;

-- ── Проверка (psql -f печатает результат прямо в консоль) ────────────────────

-- Ожидаем две строки: dating | text | YES и techniques | text | YES.
SELECT column_name, data_type, is_nullable
  FROM information_schema.columns
 WHERE table_name = 'exhibits' AND column_name IN ('dating', 'techniques')
 ORDER BY column_name;

-- Ожидаем 0 и 0: миграция только заводит колонки, заполняет их бэкфилл.
SELECT count(*) FILTER (WHERE dating IS NOT NULL)     AS with_dating,
       count(*) FILTER (WHERE techniques IS NOT NULL) AS with_techniques
  FROM exhibits;

-- Сколько работы предстоит бэкфиллу: карточки, где каталожная строка ещё лежит
-- в short_description, а структурные поля пусты. На проде 12.08.2026 — 1048.
SELECT count(*) AS stub_cards
  FROM exhibits
 WHERE coalesce(short_description, '') <> ''
   AND (year_created IS NULL OR master_name IS NULL OR material IS NULL);

-- ── Откат ────────────────────────────────────────────────────────────────────
-- Снимает колонки вместе с данными бэкфилла. Каталожные строки при этом не
-- пострадают: бэкфилл не стирает short_description, он только дочитывает из него.
--
-- BEGIN;
-- ALTER TABLE exhibits DROP COLUMN IF EXISTS dating;
-- ALTER TABLE exhibits DROP COLUMN IF EXISTS techniques;
-- COMMIT;
