-- =============================================================================
-- Миграция: year_created — строка датировки, dating выпилен
--           (таска фронта [BE] 17.08.2026)
--
-- Проблема
--   После 12.08 датировка жила в двух полях: year_created (INT, нижняя граница,
--   NULL у вековых датировок) и dating (строка дословно как в путеводителе).
--   В админке нельзя ввести «1899–1903» как есть, фронт на каждом экране решает
--   «показывай dating, если заполнен, иначе year_created», а одиночное число
--   без контекста врёт: 1899 на карточке выглядит точным годом, хотя в
--   путеводителе «1899–1903». Сортировки/фильтры по year_created в коде нет —
--   числовая нижняя граница не нужна никому, кроме самого разбора строки.
--
-- Что делает
--   1. year_created: INTEGER → TEXT, и сразу данными:
--        • dating заполнен  → year_created := dating (это и есть полная
--          датировка, число было её огрызком);
--        • dating пуст, год есть → year_created := год строкой («1899»);
--        • пусто и то и то → NULL.
--      Одним ALTER ... USING — без окна, в котором строки видны наполовину.
--   2. DROP COLUMN dating: после п.1 это дубль. Данные не теряются — строка
--      уже лежит в year_created; прежний INT был производным от неё (нижняя
--      граница, парсер каталожной строки восстановит при нужде).
--
-- ПОРЯДОК НА ПРОДЕ — миграция ломает СТАРЫЙ код и СТАРЫЕ скрипты:
--   • Скрипты 12.08 (apply_guide_fixes_20260812.py, чистка material), если они
--     ещё не прогнаны, гонять ДО этой миграции: их файл правок сверяет
--     expect_current с числовым year_created и пишет в колонку dating —
--     после миграции такие правки лягут в CONFLICT / уйдут в никуда.
--     scripts/backfill_catalog_fields.py обновлён под новую схему и идёт ПОСЛЕ.
--   • Деплой: сначала новая версия функции faberge-api, сразу за ней эта
--     миграция. Новый код на старой БД читает карточки корректно (int
--     приводится к строке в Pydantic), ломаются только админ-записи; старый
--     код на новой БД падал бы на чтении любого диапазона («1899–1903» в INT
--     не влезает) — поэтому НЕ наоборот.
--
-- Чего НЕ делает
--   Не трогает search_vector: year_created в него не входил и не входит
--   (вектор покрывает прозу — name/master_name/short_description/raw_history).
--
-- Применение к живой БД
--   psql "$DATABASE_URL" -f db/migrations/2026-08-17_year_created_text.sql
--   (scripts/init_db.py применяет db/schema.sql целиком — на чистой базе
--    миграция не нужна.)
--
-- Идемпотентность: повторный запуск безопасен — USING year_created::text на
-- уже-TEXT колонке ничего не меняет, а COALESCE/NULLIF без колонки dating не
-- выполняется (второй прогон падает на «column dating does not exist» ДО
-- каких-либо изменений — BEGIN/COMMIT держит всё в одной транзакции).
-- =============================================================================

BEGIN;

-- Тип и данные одним оператором: у строк с dating колонка сразу получает полную
-- датировку, остальные — прежний год строкой. TEXT, а не VARCHAR(n): «1880-е —
-- первая половина 1890-х. Накладной знак: начало XX века» в 255 символов
-- укладывается не всегда, усечение = потеря данных путеводителя.
ALTER TABLE exhibits
    ALTER COLUMN year_created TYPE TEXT
    USING COALESCE(NULLIF(btrim(dating), ''), year_created::text);

ALTER TABLE exhibits DROP COLUMN dating;

COMMENT ON COLUMN exhibits.year_created IS
    'Датировка дословно как в путеводителе: «1899–1903», «1880-е», «конец XIX — начало XX века». '
    'До 17.08.2026 — INT (нижняя граница) в паре с колонкой dating; теперь поле датировки одно.';

COMMIT;

-- ── Проверка (psql -f печатает результат прямо в консоль) ────────────────────

-- Ожидаем одну строку: year_created | text | YES — и НОЛЬ строк по dating.
SELECT column_name, data_type, is_nullable
  FROM information_schema.columns
 WHERE table_name = 'exhibits' AND column_name IN ('year_created', 'dating')
 ORDER BY column_name;

-- Датировки не потеряны: карточек с непустым year_created должно стать не
-- меньше, чем было с (year_created ИЛИ dating) до миграции — сверить с
-- предварительным замером:
--   SELECT count(*) FROM exhibits
--    WHERE year_created IS NOT NULL OR coalesce(btrim(dating), '') <> '';
SELECT count(*) AS with_dating_after
  FROM exhibits
 WHERE coalesce(btrim(year_created), '') <> '';

-- Глазами: диапазоны и века должны читаться дословно, «голых» лет — меньшинство.
SELECT year_created, count(*)
  FROM exhibits
 WHERE year_created IS NOT NULL
 GROUP BY year_created
 ORDER BY count(*) DESC
 LIMIT 15;

-- ── Откат ────────────────────────────────────────────────────────────────────
-- Возвращает пару полей 12.08: dating ← текущая строка, year_created ← первый
-- четырёхзначный год из неё (нижняя граница; NULL у вековых датировок).
-- Приближённо в одном месте: у карточек, где датировка — голый год («1897»),
-- dating останется NULL, хотя до миграции мог быть «1897» — различие «дубль
-- года в dating» / «пустой dating» на поведение API не влияло и не влияет.
--
-- BEGIN;
-- ALTER TABLE exhibits ADD COLUMN dating TEXT;
-- UPDATE exhibits SET dating = year_created
--  WHERE year_created IS NOT NULL AND year_created !~ '^\d{4}$';
-- ALTER TABLE exhibits
--     ALTER COLUMN year_created TYPE INTEGER
--     USING (substring(year_created FROM '(?:1[0-9]{3}|20[0-9]{2})'))::integer;
-- COMMIT;
