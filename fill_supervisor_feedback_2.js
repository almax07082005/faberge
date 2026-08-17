/**
 * Автозаполнение формы «Отзыв руководителя практики от профильной организации»
 * Период 15.06–26.07.2026. Проект: ИИ-гид музея Фаберже — развитие backend
 * (админ-панель, контент, аналитика, TTS) и развёртывание в Yandex Cloud.
 *
 * Текст написан от лица руководителя практики от организации (оценка студента).
 *
 * Как пользоваться:
 *   1. Откройте вкладку «Отзыв руководителя (организация)».
 *   2. DevTools → Console (F12), вставьте этот файл целиком, Enter.
 *
 * Форма React-управляемая (Radix UI): значения ставятся через нативный сеттер
 * value + события input/change, иначе React изменения «не увидит».
 */
(() => {
  'use strict';

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

  const data = {
    // Качество выполненной работы и удовлетворённость полученным результатом
    'organizationFeedback.qualityFeedback.ru':
      'Работа выполнена на высоком профессиональном уровне; результатом полностью удовлетворены. За период практики MVP «ИИ-гида музея Фаберже» доведён до эксплуатируемого продукта: реализован API административной панели (авторизация, управление залами, витринами, экспонатами и фотографиями с автоматической очисткой Object Storage), база наполнена реальным контентом музея с помощью разработанных студентом инструментов, добавлены модуль аналитики посещений, поддержка временных выставок и порядка залов, заметно улучшено качество озвучивания (нормализация чисел и римских цифр). Backend развёрнут в Yandex Cloud Functions, frontend — в Serverless Containers; продукт работает в production.',
    'organizationFeedback.qualityFeedback.en':
      'The work was performed at a high professional level; we are fully satisfied with the result. During the internship the “Faberge Museum AI Guide” MVP was brought to an operable product: the admin-panel API was implemented (authorization, management of halls, showcases, exhibits and photos with automatic Object Storage cleanup), the database was filled with real museum content using tooling developed by the student, a visit-analytics module, temporary-exhibition support and hall ordering were added, and speech quality was noticeably improved (normalization of numbers and Roman numerals). The backend was deployed to Yandex Cloud Functions and the frontend to Serverless Containers; the product runs in production.',

    // Сильные компетенции
    'organizationFeedback.wellDevelopedCompetencies.ru':
      'Уверенная backend-разработка на FastAPI + SQLAlchemy 2.0 (async) и проектирование admin-API с корректной работой с медиа в Object Storage. Сильные DevOps-навыки: развёртывание в Yandex Cloud Functions и Serverless Containers, включая самостоятельную диагностику и обход нетривиальной проблемы сборки контейнера (сегфолт qemu при кросс-сборке — решено сборкой на хосте и COPY-only образом), TLS-подключение к Managed PostgreSQL и дисциплина миграций. Умение писать инструменты наполнения данными (скрейпер, загрузчики) и внимание к качеству продукта — нормализация текста для озвучивания. Самостоятельность и ответственное отношение к срокам.',
    'organizationFeedback.wellDevelopedCompetencies.en':
      'Confident backend development with FastAPI + SQLAlchemy 2.0 (async) and admin-API design with correct media handling in Object Storage. Strong DevOps skills: deployment to Yandex Cloud Functions and Serverless Containers, including independent diagnosis and workaround of a non-trivial container build issue (a qemu segfault during cross-build — solved by building on the host with a COPY-only image), TLS connection to Managed PostgreSQL and migration discipline. The ability to build data-loading tooling (scraper, loaders) and attention to product quality — text normalization for speech synthesis. Independence and a responsible attitude to deadlines.',

    // Компетенции, нуждающиеся в развитии
    'organizationFeedback.toBeDevelopedCompetencies.ru':
      'Рекомендуется автоматизировать процессы, которые пока выполняются вручную: внедрить CI/CD для сборки и публикации версий backend и frontend, перевести миграции БД с ручного psql на инструмент типа Alembic. Стоит усилить покрытие автоматизированными тестами (unit/integration) и наблюдаемость сервиса в production — структурированное логирование, метрики, алёртинг. Полезно углубить навыки безопасности API (rate limiting, управление секретами) и нагрузочного тестирования под музейный трафик.',
    'organizationFeedback.toBeDevelopedCompetencies.en':
      'It is recommended to automate the processes that are still manual: introduce CI/CD for building and publishing backend and frontend versions, and move DB migrations from manual psql to a tool such as Alembic. Automated test coverage (unit/integration) and production observability — structured logging, metrics, alerting — should be strengthened. It would be useful to deepen API security skills (rate limiting, secret management) and load testing for museum traffic.',

    // Общие рекомендации студенту
    'organizationFeedback.generalRecommendations.ru':
      'Студент показал себя зрелым разработчиком, способным не только писать код, но и доводить продукт до эксплуатации: от админ-инструментов и наполнения данными до production-развёртывания и передачи проекта заказчику. Рекомендуем продолжать развитие на стыке backend и DevOps, системно внедрять автоматизацию (CI/CD, миграции, тесты) и развивать навыки эксплуатации облачных сервисов. Считаем возможным рекомендовать студента к дальнейшему сотрудничеству и сопровождению проекта.',
    'organizationFeedback.generalRecommendations.en':
      'The student proved to be a mature developer able not only to write code but to bring a product to operation: from admin tooling and data loading to production deployment and handover to the customer. We recommend continuing development at the intersection of backend and DevOps, systematically adopting automation (CI/CD, migrations, tests) and growing cloud-operations skills. We consider it possible to recommend the student for further cooperation and project maintenance.',
  };

  let ok = 0;
  for (const [name, value] of Object.entries(data)) {
    if (setByName(name, value)) ok++;
  }
  console.log(`✅ Полей заполнено: ${ok}/${Object.keys(data).length}`);
  console.log('🎉 Готово. Проверьте текст и нажмите «Сохранить».');
})();
