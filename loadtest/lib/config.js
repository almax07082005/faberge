// Общая конфигурация нагрузочных сценариев (k6).
//
// Все параметры — через переменные окружения, чтобы один и тот же сценарий
// гонялся и по локальному стенду, и по облачному, без правки кода.
import { fail } from 'k6';

// ── Куда бьём ────────────────────────────────────────────────────────────────
export const BASE_URL = (__ENV.BASE_URL || 'http://localhost:8000').replace(/\/+$/, '');

// Боевой API Gateway. Прогон по нему пишет события в продовую БД (а события по
// требованию заказчика НЕ удаляются — README §«Аналитика посетителей»), поэтому
// защищён явным подтверждением.
const PROD_HOSTS = ['d5dhcivtos7rfvdfdpg2.xxg4zr82.apigw.yandexcloud.net'];
const ALLOW_PROD = (__ENV.ALLOW_PROD || '') === 'yes';

// Не трогать внешние сервисы (YandexGPT/SpeechKit/YOLO) — только каталог и
// телеметрия. Нужно, когда стенд настроен на боевые ключи и прогон стоил бы денег.
export const SKIP_EXTERNAL = (__ENV.SKIP_EXTERNAL || '') === 'true';

// Масштаб времени раздумий посетителя. 1 = реальное время (визит ~30 мин,
// честная модель). 10 = визит за 3 мин — быстрее, но завышает rps на посетителя,
// в отчёте это надо оговаривать.
export const TIME_SCALE = Number(__ENV.TIME_SCALE || 1);

// ── Профиль посетителя ───────────────────────────────────────────────────────
// Сколько чего делает один посетитель за визит. Значения — гипотеза; после
// первого прогона сверить с реальной аналитикой (GET /admin/analytics/engagement)
// и поправить здесь.
export const PROFILE = {
  visitMinutes: Number(__ENV.VISIT_MINUTES || 30),
  halls: Number(__ENV.P_HALLS || 5),
  exhibitViewsPerHall: Number(__ENV.P_VIEWS_PER_HALL || 3),
  recognitions: Number(__ENV.P_RECOGNITIONS || 5),
  chats: Number(__ENV.P_CHATS || 5),
  stories: Number(__ENV.P_STORIES || 2),
  ttsPlays: Number(__ENV.P_TTS || 8),
  searches: Number(__ENV.P_SEARCHES || 2),
  // Телеметрия копится на фронте и уходит батчем; MAX_EVENTS_PER_BATCH = 50.
  telemetryBatchSize: Number(__ENV.P_TELEMETRY_BATCH || 10),
};

// ── Пороги ───────────────────────────────────────────────────────────────────
// Основаны на оценке путей, а не на замере: это то, что мы СЧИТАЕМ приемлемым
// для посетителя с телефоном в зале. Первый прогон покажет, где оценка врёт.
export const THRESHOLDS = {
  // Общая доля ошибок по прогону.
  'http_req_failed': ['rate<0.01'],

  // Каталог и карта: синхронный экран, посетитель ждёт.
  'http_req_duration{ep:catalog}': ['p(95)<400', 'p(99)<1000'],
  // Телеметрия: фоновая отправка, но она же держит экземпляр функции.
  'http_req_duration{ep:telemetry}': ['p(95)<500'],
  // Поиск: полнотекстовый по каталогу.
  'http_req_duration{ep:search}': ['p(95)<600'],
  // Озвучка: SpeechKit + TLS-хендшейк на каждый вызов (tts.py:181).
  'http_req_duration{ep:tts}': ['p(95)<3000'],
  // Распознавание: YOLO, серверный таймаут 25 с (config.py:33).
  'http_req_duration{ep:recognition}': ['p(95)<6000'],
  // Гид: YandexGPT, серверный таймаут 30 с (llm.py:165).
  'http_req_duration{ep:chat}': ['p(95)<8000'],
  'http_req_duration{ep:story}': ['p(95)<10000'],

  // Ошибки по критичным путям считаем отдельно: 5xx в чате посетитель видит
  // как «гид сломался», в телеметрии — не видит вообще.
  'http_req_failed{ep:chat}': ['rate<0.02'],
  'http_req_failed{ep:catalog}': ['rate<0.005'],
};

// ── Тайминг ──────────────────────────────────────────────────────────────────
// Клиентский таймаут щедрее серверного (25/30 с), чтобы отличать «сервер отдал
// ошибку по таймауту апстрима» от «k6 не дождался».
export const HTTP_TIMEOUT = __ENV.HTTP_TIMEOUT || '45s';

export function params(ep, extra) {
  return Object.assign(
    {
      tags: { ep: ep },
      timeout: HTTP_TIMEOUT,
      headers: { 'Content-Type': 'application/json' },
    },
    extra || {}
  );
}

// ── Предохранитель ───────────────────────────────────────────────────────────
// Вызывать из setup(): fail() там останавливает весь прогон.
export function guardTarget() {
  const host = BASE_URL.replace(/^https?:\/\//, '').split('/')[0];
  if (PROD_HOSTS.indexOf(host) !== -1 && !ALLOW_PROD) {
    fail(
      `BASE_URL указывает на прод (${host}). Прогон запишет события в боевую БД, ` +
        'а события не удаляются по требованию заказчика. Если это осознанно — ' +
        'перезапустите с ALLOW_PROD=yes и потом примените loadtest/cleanup.sql.'
    );
  }
  return { host: host, prod: PROD_HOSTS.indexOf(host) !== -1 };
}
