/**
 * Автозаполнение формы «Дневник прохождения практики» (период 15.06–26.07.2026)
 * Проект: ИИ-гид музея Фаберже — развитие backend (FastAPI + PostgreSQL 17):
 * админ-панель, контент-инструменты, аналитика, TTS, деплой в Yandex Cloud.
 *
 * Как пользоваться:
 *   1. Откройте страницу с формой.
 *   2. Откройте DevTools → Console (F12).
 *   3. Вставьте содержимое этого файла целиком и нажмите Enter.
 *
 * Форма — React-управляемая (Radix UI), поэтому значения проставляются через
 * нативный сеттер value + события input/change, иначе React их «не увидит».
 */
(async () => {
  'use strict';

  const delay = (ms) => new Promise((r) => setTimeout(r, ms));

  // --- React-совместимая установка значения для input/textarea ---------------
  function setReactValue(el, value) {
    if (!el) return false;
    const proto =
      el instanceof window.HTMLTextAreaElement
        ? window.HTMLTextAreaElement.prototype
        : window.HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
    setter.call(el, value);
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
    el.dispatchEvent(new Event('blur', { bubbles: true }));
    return true;
  }

  function setByName(name, value) {
    const el = document.querySelector(`[name="${name}"]`);
    if (!el) {
      console.warn('⚠️ поле не найдено:', name);
      return false;
    }
    return setReactValue(el, value);
  }

  // --------------------------- КОНТЕНТ ----------------------------------------
  const data = {
    // Индивидуальное задание
    'journal.individualTask.ru':
      'Развитие серверной части web-приложения (PWA) «ИИ-гид музея Фаберже» и его инфраструктуры: разработка REST API административной панели (управление залами, витринами, экспонатами и медиа), инструментов наполнения контентом, модуля аналитики посещений, улучшение озвучивания (SpeechKit), а также развёртывание backend и frontend в Yandex Cloud (Cloud Functions, Serverless Containers, Managed PostgreSQL 17, Object Storage).',
    'journal.individualTask.en':
      'Evolution of the backend of the “Faberge Museum AI Guide” web application (PWA) and its infrastructure: developing the admin-panel REST API (managing halls, showcases, exhibits and media), content-loading tooling, a visit-analytics module, improvements to speech synthesis (SpeechKit), and deploying the backend and frontend to Yandex Cloud (Cloud Functions, Serverless Containers, Managed PostgreSQL 17, Object Storage).',

    // Планируемые результаты
    'journal.expectedResults.ru':
      'Рабочая административная панель (API): авторизация, полный CRUD залов, витрин, экспонатов и фотографий с загрузкой медиа в Object Storage и корректной очисткой при удалении; наполненная реальным контентом музея база данных; модуль аналитики (вопросы посетителей, сессии, маршруты); качественная озвучка описаний с нормализацией чисел и римских цифр; работающие production-развёртывания backend (Cloud Functions) и frontend (Serverless Containers) с подключением к Managed PostgreSQL 17 по TLS.',
    'journal.expectedResults.en':
      'A working admin-panel API: authorization, full CRUD for halls, showcases, exhibits and photos with media upload to Object Storage and proper cleanup on deletion; a database filled with real museum content; an analytics module (visitor questions, sessions, routes); high-quality speech synthesis with normalization of numbers and Roman numerals; production deployments of the backend (Cloud Functions) and frontend (Serverless Containers) connected to Managed PostgreSQL 17 over TLS.',

    // Краткое описание достигнутого результата
    'journal.achievedResults.ru':
      'Реализован admin-API: логин, CRUD залов/витрин/экспонатов, загрузка фотографий (id, признак is_primary, обложки залов) с автоматической очисткой Object Storage при удалении. Написаны скрейпер сайта музея и скрипты заливки контента; база наполнена описаниями залов из путеводителя. Добавлены тег «Временная выставка» с фильтрами, порядок залов и модуль аналитики (вопросы, сессии, маршруты). Озвучка улучшена нормализацией чисел и римских цифр (XIX → «девятнадцатый»). Backend развёрнут как Yandex Cloud Function, frontend (Next.js SSR) — в Serverless Containers; миграции БД применяются через psql.',
    'journal.achievedResults.en':
      'The admin API was implemented: login, CRUD for halls/showcases/exhibits, photo upload (id, is_primary flag, hall covers) with automatic Object Storage cleanup on deletion. A museum-website scraper and content-loading scripts were written; the database was filled with hall descriptions from the guidebook. A “Temporary exhibition” tag with filters, hall ordering and an analytics module (questions, sessions, routes) were added. Speech synthesis was improved with normalization of numbers and Roman numerals (XIX → “nineteenth”). The backend was deployed as a Yandex Cloud Function and the frontend (Next.js SSR) to Serverless Containers; DB migrations are applied via psql.',

    // Саморефлексия
    'journal.selfReflection.ru':
      'Практика дала опыт доведения MVP до эксплуатируемого продукта: не только новые эндпоинты, но и админ-инструменты, наполнение реальными данными и production-деплой. Наиболее полезными оказались задачи на стыке backend и DevOps: упаковка FastAPI в Cloud Functions, сборка контейнера Next.js для Serverless Containers (включая обход сегфолта qemu при кросс-сборке — сборка на хосте и образ только с COPY), TLS-подключение к Managed PostgreSQL и дисциплина ручных миграций. Понял ценность «мелочей» качества продукта — нормализация цифр в озвучке заметно улучшила восприятие. Планирую добавить CI/CD и автоматизировать миграции.',
    'journal.selfReflection.en':
      'The internship gave me experience of taking an MVP to an operable product: not only new endpoints, but admin tooling, real data loading and production deployment. The most valuable tasks were at the intersection of backend and DevOps: packaging FastAPI for Cloud Functions, building the Next.js container for Serverless Containers (including a workaround for a qemu segfault during cross-build — building on the host with a COPY-only image), TLS connection to Managed PostgreSQL and the discipline of manual migrations. I realized the value of quality “details” — number normalization in speech noticeably improved perception. Next I plan to add CI/CD and automate migrations.',

    // ---- Этап 1: Admin-API и медиа (15.06–26.06) ----
    'journal.workScheduleItems.0.task.ru':
      'Проектирование и реализация базового API административной панели: авторизация администратора, загрузка фотографий экспонатов (идентификаторы, признак главного фото is_primary, обложки залов) в Object Storage, эндпоинт GET /showcases с фильтром по залу, очистка медиа в Object Storage при удалении экспоната или фотографии.',
    'journal.workScheduleItems.0.task.en':
      'Designing and implementing the core admin-panel API: administrator login, uploading exhibit photos (ids, is_primary flag, hall covers) to Object Storage, a GET /showcases endpoint with a hall filter, and Object Storage media cleanup when an exhibit or photo is deleted.',
    'journal.workScheduleItems.0.result.ru':
      'Заработали логин администратора и загрузка медиа: фотографии получают id и признак is_primary, у залов появились обложки. Добавлен плоский список витрин GET /showcases с фильтром hall_id. При удалении экспоната или отдельного фото связанные файлы автоматически удаляются из Object Storage — хранилище не накапливает «осиротевшие» объекты.',
    'journal.workScheduleItems.0.result.en':
      'Admin login and media upload became operational: photos get an id and an is_primary flag, halls got cover images. A flat showcase list GET /showcases with a hall_id filter was added. When an exhibit or a single photo is deleted, the related files are automatically removed from Object Storage — no orphaned objects accumulate.',

    // ---- Этап 2: Контент и инструменты наполнения (27.06–08.07) ----
    'journal.workScheduleItems.1.task.ru':
      'Наполнение базы реальным контентом музея: разработка скрейпера сайта музея Фаберже и скриптов заливки данных и медиа, подготовка описаний залов из путеводителя (сид и скрипт загрузки). Расширение admin-API: удаление залов и витрин, GET /admin/exhibits/{id}, восстановление обрезанных описаний экспонатов. Ручные миграции схемы БД через psql.',
    'journal.workScheduleItems.1.task.en':
      'Filling the database with real museum content: developing a Faberge Museum website scraper and data/media loading scripts, preparing hall descriptions from the guidebook (seed and loading script). Extending the admin API: deleting halls and showcases, GET /admin/exhibits/{id}, restoring truncated exhibit descriptions. Manual DB schema migrations via psql.',
    'journal.workScheduleItems.1.result.ru':
      'Написан инструментарий наполнения: скрейпер, загрузчики данных и медиа, скрипт заливки обложек залов. База заполнена описаниями залов из путеводителя и карточками экспонатов. Admin-API дополнен удалением залов и витрин и просмотром экспоната GET /admin/exhibits/{id}; восстановлены обрезанные описания. Отработан процесс миграций Managed PostgreSQL через psql по TLS.',
    'journal.workScheduleItems.1.result.en':
      'Content tooling was written: a scraper, data and media loaders, and a hall-cover upload script. The database was filled with guidebook hall descriptions and exhibit cards. The admin API gained hall/showcase deletion and exhibit view GET /admin/exhibits/{id}; truncated descriptions were restored. The Managed PostgreSQL migration process via psql over TLS was established.',

    // ---- Этап 3: Аналитика, выставки, озвучка (09.07–17.07) ----
    'journal.workScheduleItems.2.task.ru':
      'Разработка модуля аналитики (вопросы посетителей, сессии, маршруты по залам), поддержка порядка залов в экспозиции, тег «Временная выставка» (is_temporary на зале с фильтрами в каталоге и админке). Улучшение озвучивания SpeechKit: нормализация текста перед синтезом — числа прописью и римские цифры (XIX → «девятнадцатый», Александр III).',
    'journal.workScheduleItems.2.task.en':
      'Developing the analytics module (visitor questions, sessions, hall routes), supporting hall ordering in the exposition, and a “Temporary exhibition” tag (is_temporary on a hall with filters in the catalog and admin panel). Improving SpeechKit synthesis: text normalization before synthesis — numbers as words and Roman numerals (XIX → “nineteenth”, Alexander III).',
    'journal.workScheduleItems.2.result.ru':
      'Модуль аналитики собирает вопросы, сессии и маршруты посетителей и отдаёт сводки для админки. Залы получили управляемый порядок отображения и тег «Временная выставка» с фильтрами в каталоге и админке. Озвучка стала естественной: перед синтезом числа раскрываются прописью, римские цифры нормализуются — исчезли ошибки чтения дат и имён монархов.',
    'journal.workScheduleItems.2.result.en':
      'The analytics module collects visitor questions, sessions and routes and serves summaries for the admin panel. Halls received a managed display order and a “Temporary exhibition” tag with filters in the catalog and admin panel. Speech became natural: numbers are expanded to words and Roman numerals are normalized before synthesis — misread dates and monarch names disappeared.',

    // ---- Этап 4: Развёртывание и эксплуатация (18.07–26.07) ----
    'journal.workScheduleItems.3.task.ru':
      'Production-развёртывание в Yandex Cloud: backend (FastAPI) как Cloud Function faberge-api (сборка zip-архива, публикация версий), frontend (Next.js SSR) в Serverless Containers — сборка Docker-образа с обходом сегфолта qemu при кросс-сборке (сборка на хосте, образ только с COPY). Прогон миграций, сквозное тестирование, документация и передача проекта.',
    'journal.workScheduleItems.3.task.en':
      'Production deployment to Yandex Cloud: the backend (FastAPI) as the faberge-api Cloud Function (zip build, version publishing), the frontend (Next.js SSR) to Serverless Containers — building the Docker image with a workaround for a qemu segfault during cross-build (host build, COPY-only image). Running migrations, end-to-end testing, documentation and project handover.',
    'journal.workScheduleItems.3.result.ru':
      'Backend работает как Yandex Cloud Function с подключением к Managed PostgreSQL 17 по TLS; отлажен цикл выпуска: пересборка zip → создание версии → ручные миграции psql. Frontend развёрнут в Serverless Containers по схеме «сборка на хосте + образ COPY-only», устранившей сегфолт qemu. Проведено сквозное тестирование сценария посетителя и админки; документация обновлена, проект передан музею.',
    'journal.workScheduleItems.3.result.en':
      'The backend runs as a Yandex Cloud Function connected to Managed PostgreSQL 17 over TLS; the release cycle was established: rebuild zip → create version → manual psql migrations. The frontend was deployed to Serverless Containers using the “host build + COPY-only image” scheme that eliminated the qemu segfault. End-to-end testing of the visitor and admin scenarios was performed; documentation was updated and the project was handed over to the museum.',
  };

  // --------------------------- Заполнение текстов -----------------------------
  let ok = 0;
  for (const [name, value] of Object.entries(data)) {
    if (setByName(name, value)) ok++;
  }
  console.log(`✅ Текстовых полей заполнено: ${ok}/${Object.keys(data).length}`);

  // --------------------------- Даты этапов ------------------------------------
  // Начало этапа 1 (15.06.2026) и конец этапа 4 (26.07.2026) заданы и
  // заблокированы. Заполняем разблокированные «Даты окончания» этапов 1–3;
  // начала следующих этапов форма подтягивает автоматически.
  const endDates = ['2026-06-26', '2026-07-08', '2026-07-17'];
  const editableDateInputs = [
    ...document.querySelectorAll('input[type="date"]:not([disabled])'),
  ];
  editableDateInputs.forEach((el, i) => {
    if (i < endDates.length) setReactValue(el, endDates[i]);
  });
  console.log(
    `📅 Дат окончания заполнено: ${Math.min(
      editableDateInputs.length,
      endDates.length
    )} (этапы 1–3)`
  );

  // --------------------------- Выпадающие списки (Radix) ----------------------
  // «Полученные знания» и «Качество организации практики». Пытаемся выбрать
  // максимально позитивный вариант. Radix рендерит пункты в портал только
  // после открытия списка.
  async function setRadixSelect(trigger) {
    const fire = (type) =>
      trigger.dispatchEvent(
        new PointerEvent(type, { bubbles: true, cancelable: true, pointerType: 'mouse', button: 0 })
      );
    fire('pointerdown');
    fire('pointerup');
    trigger.click();
    await delay(120);

    const options = [...document.querySelectorAll('[role="option"]')];
    if (!options.length) return false;

    const positive =
      options.find((o) => /полност/i.test(o.textContent)) ||
      options.find((o) => /отличн/i.test(o.textContent)) ||
      options.find((o) => /(^|\s)да\b/i.test(o.textContent)) ||
      options.find((o) => /удовлетвор|скорее да|высок/i.test(o.textContent)) ||
      options[0];

    positive.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true }));
    positive.dispatchEvent(new PointerEvent('pointerup', { bubbles: true }));
    positive.click();
    await delay(120);
    return positive.textContent.trim();
  }

  const combos = [...document.querySelectorAll('button[role="combobox"]')];
  for (const c of combos) {
    try {
      const picked = await setRadixSelect(c);
      console.log(picked ? `🔽 Выбрано: «${picked}»` : '⚠️ Список не открылся — выберите вручную');
    } catch (e) {
      console.warn('⚠️ Не удалось выбрать в списке, заполните вручную:', e.message);
    }
  }

  console.log('🎉 Готово. Проверьте поля и при необходимости поправьте даты/списки вручную.');
})();
