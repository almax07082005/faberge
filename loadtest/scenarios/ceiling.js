// Потолок одного эндпоинта: рампа по rps до отказа.
//
// Отвечает на «сколько rps держит чат», а не «сколько посетителей». Нужен,
// потому что в профиле визита узкое место (гид) размазано редкими вызовами и
// колено ловится долго.
//
//   k6 run -e BASE_URL=... -e EP=chat -e MAX_RPS=10 loadtest/scenarios/ceiling.js
//
// EP: catalog | telemetry | search | tts | recognition | chat | story
//
// Прогон намеренно доводит до отказа. Порог с abortOnFail останавливает рампу,
// как только p95 или доля ошибок вылезли — искать точку выше смысла нет, а
// внешние квоты (YandexGPT/SpeechKit) стоят денег.
import http from 'k6/http';
import { check } from 'k6';
import { BASE_URL, params, guardTarget } from '../lib/config.js';
import { FIXTURES, PHOTO, checkFixtures, markedUuid, pick } from '../lib/fixtures.js';

const EP = __ENV.EP || 'catalog';
const MAX_RPS = Number(__ENV.MAX_RPS || 20);
const STEP_SEC = Number(__ENV.STEP_SEC || 60);

// Потолок по каждому пути свой; ставим тот, при котором ещё имеет смысл мерить.
const LIMITS = {
  catalog: { p95: 1000, fail: 0.01 },
  telemetry: { p95: 1000, fail: 0.01 },
  search: { p95: 1500, fail: 0.01 },
  tts: { p95: 5000, fail: 0.02 },
  recognition: { p95: 10000, fail: 0.02 },
  chat: { p95: 15000, fail: 0.02 },
  story: { p95: 15000, fail: 0.02 },
};
const limit = LIMITS[EP] || LIMITS.catalog;

export const options = {
  scenarios: {
    ramp: {
      executor: 'ramping-arrival-rate',
      startRate: 1,
      timeUnit: '1s',
      preAllocatedVUs: 10,
      // Медленный эндпоинт при конкурентности 1 на экземпляр требует много VU:
      // 3 rps × 8 с ответа = 24 висящих запроса.
      maxVUs: Math.max(50, MAX_RPS * 30),
      stages: [
        { target: Math.max(1, Math.round(MAX_RPS * 0.1)), duration: `${STEP_SEC}s` },
        { target: Math.max(2, Math.round(MAX_RPS * 0.25)), duration: `${STEP_SEC}s` },
        { target: Math.max(3, Math.round(MAX_RPS * 0.5)), duration: `${STEP_SEC}s` },
        { target: Math.max(4, Math.round(MAX_RPS * 0.75)), duration: `${STEP_SEC}s` },
        { target: MAX_RPS, duration: `${STEP_SEC}s` },
      ],
    },
  },
  thresholds: {
    [`http_req_duration{ep:${EP}}`]: [
      { threshold: `p(95)<${limit.p95}`, abortOnFail: true, delayAbortEval: '20s' },
    ],
    [`http_req_failed{ep:${EP}}`]: [
      { threshold: `rate<${limit.fail}`, abortOnFail: true, delayAbortEval: '20s' },
    ],
  },
  summaryTrendStats: ['avg', 'min', 'med', 'p(90)', 'p(95)', 'p(99)', 'max'],
};

export function setup() {
  checkFixtures();
  console.log(`Рампа по ${EP}: 1 → ${MAX_RPS} rps, ступень ${STEP_SEC} с, отсечка p95>${limit.p95} мс`);
  return guardTarget();
}

const CALLS = {
  catalog: () => {
    const hall = pick(FIXTURES.halls);
    return http.get(`${BASE_URL}/halls/${hall.id}/exhibits`, params('catalog'));
  },

  search: () =>
    http.get(`${BASE_URL}/search?q=${encodeURIComponent(pick(FIXTURES.queries))}&limit=20`, params('search')),

  telemetry: () => {
    const ex = pick(FIXTURES.exhibits);
    const events = [];
    // Батч как у фронта: 10 событий, а не одно — иначе меряем накладные расходы
    // HTTP, а не запись.
    for (let i = 0; i < 10; i++) {
      events.push({ type: 'exhibit_view', exhibit_id: ex.id, hall_id: ex.hall_id, ts: new Date().toISOString() });
    }
    const body = JSON.stringify({ session_id: markedUuid(), device_id: markedUuid(), events: events });
    return http.post(`${BASE_URL}/telemetry/events`, body, params('telemetry'));
  },

  tts: () => {
    const body = JSON.stringify({
      exhibit_id: pick(FIXTURES.exhibits).id,
      voice: 'alena',
      format: 'mp3',
      speed: 1.0,
      emotion: 'neutral',
    });
    return http.post(`${BASE_URL}/speech`, body, params('tts'));
  },

  recognition: () => {
    const form = {
      file: http.file(PHOTO, 'photo.jpg', 'image/jpeg'),
      hall_id: String(pick(FIXTURES.halls).id),
      top_k: '3',
    };
    return http.post(`${BASE_URL}/recognition`, form, params('recognition', { headers: {} }));
  },

  chat: () => {
    const ex = pick(FIXTURES.exhibits);
    const body = JSON.stringify({
      session_id: markedUuid(),
      context: { exhibit_id: ex.id, hall_id: ex.hall_id },
      message: pick(FIXTURES.questions),
      language: 'ru',
      max_questions: 3,
    });
    return http.post(`${BASE_URL}/guide/chat`, body, params('chat'));
  },

  story: () => {
    const body = JSON.stringify({
      exhibit_id: pick(FIXTURES.exhibits).id,
      style: 'engaging',
      language: 'ru',
      include_audio: false,
      max_questions: 4,
    });
    return http.post(`${BASE_URL}/guide/story`, body, params('story'));
  },
};

export default function () {
  const call = CALLS[EP];
  if (!call) throw new Error(`Неизвестный EP=${EP}. Допустимо: ${Object.keys(CALLS).join(', ')}`);
  const res = call();
  check(res, { '2xx': (r) => r.status >= 200 && r.status < 300 });
}
