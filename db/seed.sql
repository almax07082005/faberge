-- ============================================================================
--  Демо-данные «ИИ-гид музея Фаберже» (PostgreSQL 17)
--  Применение: psql "$DATABASE_URL" -f db/seed.sql
--  Идентификаторы заданы явно и детерминированно (id 101 = Коронационное яйцо),
--  чтобы совпадать с примерами в openapi.yaml. В конце сбрасываются sequence.
-- ============================================================================

BEGIN;

-- Залы -----------------------------------------------------------------------
-- is_temporary=true — зал временной (сменной) выставки; остальные — основная экспозиция.
INSERT INTO halls (id, hall_number, name, description, level, cover_image_url, is_temporary) VALUES
 (1, 1, 'Рыцарский зал',
     'Парадный зал Шуваловского дворца с витринами портсигаров, рамок и предметов с эмалью.', 2,
     'https://cdn.example.cloud/halls/knights/cover.jpg', false),
 (2, 2, 'Красная гостиная',
     'Гостиная с камнерезными миниатюрами и анималистическими фигурками фирмы Фаберже.', 2,
     'https://cdn.example.cloud/halls/red/cover.jpg', false),
 (3, 3, 'Синяя гостиная',
     'Главный зал коллекции: здесь представлены императорские пасхальные яйца Фаберже.', 2,
     'https://cdn.example.cloud/halls/blue/cover.jpg', false),
 (4, 4, 'Золотая гостиная',
     'Зал с цветочными этюдами и кабинетными драгоценностями.', 2,
     'https://cdn.example.cloud/halls/gold/cover.jpg', false),
 (5, 5, 'Выставочный зал',
     'Пространство сменных тематических экспозиций музея.', 1,
     'https://cdn.example.cloud/halls/expo/cover.jpg', true),
 (6, 6, 'Готическая комната',
     'Камерный зал с произведениями эмальерного искусства.', 2,
     'https://cdn.example.cloud/halls/gothic/cover.jpg', false)
ON CONFLICT (id) DO NOTHING;

-- Витрины --------------------------------------------------------------------
INSERT INTO showcases (id, hall_id, showcase_number, name) VALUES
 (1, 3, 1, 'Императорские пасхальные яйца'),
 (2, 3, 2, 'Подарочные предметы'),
 (3, 1, 1, 'Портсигары и рамки'),
 (4, 2, 1, 'Камнерезные миниатюры'),
 (5, 4, 1, 'Цветочные этюды'),
 (6, 5, 1, 'Сменная экспозиция'),
 (7, 6, 1, 'Эмали')
ON CONFLICT (id) DO NOTHING;

-- Экспонаты ------------------------------------------------------------------
INSERT INTO exhibits
 (id, showcase_id, label_slug, name, year_created, master_name, material, short_description, raw_history, image_url, model_3d_url, source_url) VALUES
 (101, 1, 'faberge_egg_coronation', 'Яйцо «Коронационное»', 1897, 'Михаил Перхин',
   'Золото, гильоше-эмаль, бриллианты, рубины',
   'Пасхальное яйцо-сюрприз, подаренное Николаем II императрице Александре Фёдоровне в память о коронации 1896 года.',
   'Мастер — Михаил Перхин. Год — 1897. Заказчик — Николай II. Подарок императрице Александре Фёдоровне. Сюрприз — миниатюрная копия императорской кареты работы Георга Штейна. Материалы — золото, гильоше-эмаль цвета императорской мантии, бриллианты, рубины.',
   'https://cdn.example.cloud/exhibits/coronation/main.jpg', 'https://koinovo.ru/fabergemuseum/coronation',
   'https://fabergemuseum.ru/kollekczii/shedevryi-kollekczii/'),
 (102, 1, 'faberge_egg_lilies', 'Яйцо «Ландыши»', 1898, 'Михаил Перхин',
   'Золото, эмаль, жемчуг, бриллианты, рубины',
   'Яйцо в стиле модерн, украшенное ландышами; сюрприз — медальоны с портретами.',
   'Мастер — Михаил Перхин. Год — 1898. Стиль — ар-нуво. Заказчик — Николай II. Подарок Александре Фёдоровне. Сюрприз — три медальона с портретами Николая II и дочерей. Материалы — золото, эмаль, жемчуг, бриллианты-розы, рубины.',
   'https://cdn.example.cloud/exhibits/lilies/main.jpg', 'https://koinovo.ru/fabergemuseum/lilies',
   'https://fabergemuseum.ru/kollekczii/shedevryi-kollekczii/'),
 (103, 1, 'faberge_egg_rosebud', 'Яйцо «Бутон розы»', 1895, 'Михаил Перхин',
   'Золото, красная эмаль, бриллианты',
   'Первое пасхальное яйцо, подаренное Николаем II супруге; сюрприз — бутон розы.',
   'Мастер — Михаил Перхин. Год — 1895. Первый императорский подарок Николая II Александре Фёдоровне. Сюрприз — жёлтый бутон розы, внутри которого были корона и подвеска. Материалы — золото, прозрачная красная эмаль, бриллианты.',
   'https://cdn.example.cloud/exhibits/rosebud/main.jpg', 'https://koinovo.ru/fabergemuseum/rosebud',
   'https://fabergemuseum.ru/kollekczii/shedevryi-kollekczii/'),
 (104, 1, 'faberge_egg_renaissance', 'Яйцо «Ренессанс»', 1894, 'Михаил Перхин',
   'Агат, золото, эмаль, бриллианты, рубины',
   'Последнее пасхальное яйцо, подаренное Александром III Марии Фёдоровне; выполнено в форме шкатулки.',
   'Мастер — Михаил Перхин. Год — 1894. Заказчик — Александр III. Подарок Марии Фёдоровне. Форма — лежащая шкатулка по мотивам изделия Le Roy. Материалы — молочный агат, золото, белая и синяя эмаль, бриллианты, рубины.',
   'https://cdn.example.cloud/exhibits/renaissance/main.jpg', 'https://koinovo.ru/fabergemuseum/renaissance',
   'https://fabergemuseum.ru/kollekczii/shedevryi-kollekczii/'),
 (105, 1, 'faberge_egg_winter', 'Яйцо «Зимнее»', 1913, 'Альма Пиль',
   'Горный хрусталь, платина, бриллианты',
   'Яйцо в виде ледяного кристалла; сюрприз — корзина подснежников.',
   'Дизайн — Альма Пиль. Год — 1913. Заказчик — Николай II. Подарок императрице Марии Фёдоровне. Сюрприз — корзина подснежников из золота, нефрита и гранатов. Материалы — горный хрусталь, платина, более 3000 бриллиантов, имитирующих иней.',
   'https://cdn.example.cloud/exhibits/winter/main.jpg', 'https://koinovo.ru/fabergemuseum/winter',
   'https://fabergemuseum.ru/kollekczii/shedevryi-kollekczii/'),
 (106, 2, 'faberge_bonbonniere', 'Бонбоньерка с часами', 1900, 'Генрик Вигстрём',
   'Золото, гильоше-эмаль, бриллианты',
   'Настольная бонбоньерка-часы в стиле Людовика XVI.',
   'Мастер — Генрик Вигстрём. Около 1900 года. Назначение — настольные часы-бонбоньерка. Материалы — золото, розовая гильоше-эмаль, бриллианты-розы. Стиль — неоклассицизм.',
   'https://cdn.example.cloud/exhibits/bonbonniere/main.jpg', NULL,
   'https://fabergemuseum.ru/kollekczii/shedevryi-kollekczii/'),
 (110, 3, 'faberge_cigarette_case', 'Портсигар с сапфиром', 1908, 'Август Хольмстрём',
   'Золото, эмаль, сапфир',
   'Портсигар с гильоше-эмалью и сапфировой кнопкой-застёжкой.',
   'Мастер — Август Хольмстрём. Около 1908 года. Материалы — золото, синяя гильоше-эмаль, кабошон-сапфир на застёжке. Назначение — подарочный портсигар.',
   'https://cdn.example.cloud/exhibits/cigarette/main.jpg', NULL,
   'https://fabergemuseum.ru/kollekczii/shedevryi-kollekczii/'),
 (111, 3, 'faberge_photo_frame', 'Рамка для фотографии', 1905, 'Виктор Аарне',
   'Серебро, гильоше-эмаль, слоновая кость',
   'Настольная рамка с бирюзовой эмалью и жемчужной обводкой.',
   'Мастер — Виктор Аарне. Около 1905 года. Материалы — серебро, бирюзовая гильоше-эмаль, обводка из жемчуга, подложка из слоновой кости. Назначение — кабинетная фоторамка.',
   'https://cdn.example.cloud/exhibits/frame/main.jpg', NULL,
   'https://fabergemuseum.ru/kollekczii/shedevryi-kollekczii/'),
 (120, 4, 'faberge_stone_elephant', 'Фигурка «Слон»', 1900, 'Камнерезная мастерская Фаберже',
   'Обсидиан, золото, бриллианты',
   'Камнерезная анималистическая миниатюра — слон с погонщиком.',
   'Камнерезная мастерская Фаберже. Около 1900 года. Сюжет — слон с погонщиком. Материалы — обсидиан, золото, бриллианты-розы для глаз. Жанр — анималистическая миниатюра.',
   'https://cdn.example.cloud/exhibits/elephant/main.jpg', NULL,
   'https://fabergemuseum.ru/kollekczii/shedevryi-kollekczii/'),
 (130, 5, 'faberge_flower_study', 'Цветочный этюд «Незабудки»', 1910, 'Фирма Фаберже',
   'Золото, эмаль, нефрит, горный хрусталь',
   'Этюд незабудок в вазочке с «водой» из горного хрусталя.',
   'Фирма Фаберже. Около 1910 года. Сюжет — букетик незабудок. Материалы — золото, синяя и зелёная эмаль, нефритовые листья, вазочка из горного хрусталя, имитирующего воду. Жанр — цветочный этюд.',
   'https://cdn.example.cloud/exhibits/forgetmenot/main.jpg', NULL,
   'https://fabergemuseum.ru/kollekczii/shedevryi-kollekczii/'),
 -- Экспонат сменной (временной) выставки — витрина 6 в зале 5 (is_temporary=true).
 (140, 6, 'faberge_temp_brooch', 'Брошь «Весенние цветы» (временная выставка)', 1902, 'Фирма Фаберже',
   'Золото, эмаль, демантоиды, бриллианты',
   'Экспонат сменной тематической выставки: брошь в виде букета весенних цветов.',
   'Фирма Фаберже. Около 1902 года. Показывается в рамках временной выставки. Материалы — золото, оконная эмаль, уральские демантоиды, бриллианты-розы. Жанр — ювелирная брошь.',
   'https://cdn.example.cloud/exhibits/spring-brooch/main.jpg', NULL,
   'https://fabergemuseum.ru/kollekczii/shedevryi-kollekczii/')
ON CONFLICT (id) DO NOTHING;

-- Галерея (по 1–2 изображения на несколько экспонатов) -----------------------
INSERT INTO exhibit_images (exhibit_id, url, alt, width, height, is_primary, position) VALUES
 (101, 'https://cdn.example.cloud/exhibits/coronation/main.jpg',   'Коронационное яйцо, общий вид', 1600, 1600, true,  0),
 (101, 'https://cdn.example.cloud/exhibits/coronation/surprise.jpg','Сюрприз — карета',             1600, 1066, false, 1),
 (105, 'https://cdn.example.cloud/exhibits/winter/main.jpg',       'Зимнее яйцо, общий вид',        1600, 1600, true,  0)
ON CONFLICT DO NOTHING;

-- Сброс последовательностей --------------------------------------------------
SELECT setval(pg_get_serial_sequence('halls', 'id'),          (SELECT max(id) FROM halls));
SELECT setval(pg_get_serial_sequence('showcases', 'id'),      (SELECT max(id) FROM showcases));
SELECT setval(pg_get_serial_sequence('exhibits', 'id'),       (SELECT max(id) FROM exhibits));
SELECT setval(pg_get_serial_sequence('exhibit_images', 'id'), (SELECT COALESCE(max(id), 1) FROM exhibit_images));

COMMIT;
