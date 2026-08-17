// «Автобус на входе»: N посетителей открывают приложение одновременно.
//
// Проверяет худший момент архитектуры. Конкурентность экземпляра функции
// равна 1 (index.py:87 — run_until_complete в синхронном handler), поэтому N
// одновременных запросов = N холодных стартов, у каждого свой импорт SQLAlchemy
// и свой TLS-коннект к Managed PostgreSQL.
//
//   k6 run -e BASE_URL=... -e BURST=30 loadtest/scenarios/burst.js
//
// Смотреть надо на first_request_ms, а не на средние по прогону: посетителя
// интересует, за сколько открылся экран, а не сколько запросов в секунду прошло.
import http from 'k6/http';
import { check, sleep } from 'k6';
import { Trend } from 'k6/metrics';
import { BASE_URL, params, guardTarget } from '../lib/config.js';
import { FIXTURES, checkFixtures, pick } from '../lib/fixtures.js';
import { newVisitor, track, flush } from '../lib/journey.js';

const BURST = Number(__ENV.BURST || 30);
// Сколько раз повторить наплыв. Первый — по холодным экземплярам, дальнейшие —
// по прогретым: разница между ними и есть цена холодного старта.
const WAVES = Number(__ENV.WAVES || 3);
// Пауза между волнами. По умолчанию короткая — экземпляры ещё тёплые.
const GAP_SEC = Number(__ENV.GAP_SEC || 30);

const firstRequest = new Trend('first_request_ms', true);

export const options = {
  scenarios: {
    bus: {
      executor: 'per-vu-iterations',
      vus: BURST,
      iterations: WAVES,
      maxDuration: `${(WAVES + 1) * (GAP_SEC + 60)}s`,
    },
  },
  thresholds: {
    // Экран карты должен открыться раньше, чем посетитель решит, что приложение
    // сломалось. 5 с — граница терпения, не техническая величина.
    'first_request_ms': ['p(95)<5000'],
    'http_req_failed': ['rate<0.02'],
  },
  summaryTrendStats: ['avg', 'min', 'med', 'p(90)', 'p(95)', 'p(99)', 'max'],
};

export function setup() {
  checkFixtures();
  console.log(`Наплыв: ${BURST} посетителей одновременно, ${WAVES} волн, пауза ${GAP_SEC} с`);
  return guardTarget();
}

export default function () {
  const v = newVisitor();

  // Первый запрос приложения — карта. Его и меряем отдельно.
  const t0 = Date.now();
  const map = http.get(`${BASE_URL}/map`, params('catalog'));
  firstRequest.add(Date.now() - t0);
  check(map, { 'map 200': (r) => r.status === 200 });

  const hall = pick(FIXTURES.halls);
  const res = http.batch([
    ['GET', `${BASE_URL}/halls`, null, params('catalog')],
    ['GET', `${BASE_URL}/halls/${hall.id}/exhibits`, null, params('catalog')],
  ]);
  check(res[1], { 'hall exhibits 200': (r) => r.status === 200 });

  track(v, 'app_open', { props: { entry: 'qr' } });
  track(v, 'hall_view', { hall_id: hall.id });
  flush(v);

  sleep(GAP_SEC);
}
