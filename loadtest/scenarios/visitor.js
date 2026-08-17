// Профиль музея: лестница по числу одновременных посетителей.
//
// Модель — не «столько-то rps», а «столько-то человек одновременно ходят по
// залам с открытым приложением». Каждая итерация = один визит целиком, с
// паузами на разглядывание. rps получается как следствие профиля, а не задаётся
// руками — иначе меряли бы выдуманную нагрузку.
//
//   k6 run -e BASE_URL=... -e VISITORS=150 -e TIME_SCALE=6 \
//       --out json=loadtest/artifacts/visitor.json loadtest/scenarios/visitor.js
//
// Лестница: 25% → 50% → 100% → 150% от VISITORS. Колено (где p95 уходит вверх,
// а http_req_failed отрывается от нуля) и есть ответ «сколько выдержит».
import { PROFILE, THRESHOLDS, TIME_SCALE, guardTarget } from '../lib/config.js';
import { checkFixtures } from '../lib/fixtures.js';
import { visit } from '../lib/journey.js';

// Целевое число одновременных посетителей на 100%-ступени.
const PEAK = Number(__ENV.VISITORS || 150);

// Визит в секундах с учётом сжатия времени.
const VISIT_SEC = (PROFILE.visitMinutes * 60) / TIME_SCALE;

// Чтобы держать N человек одновременно при визите длиной T, приходить должны
// N/T человек в секунду. Ставим timeUnit в минуту — целые числа читаемее.
const ratePerMin = (share) => Math.max(1, Math.round((PEAK * share * 60) / VISIT_SEC));

// Ступень должна быть длиннее визита, иначе система не успеет выйти на полку:
// первые посетители ещё не ушли, когда ступень кончилась.
const STAGE = `${Math.round(VISIT_SEC * 1.5)}s`;
const RAMP = `${Math.round(VISIT_SEC * 0.3)}s`;

export const options = {
  scenarios: {
    museum: {
      executor: 'ramping-arrival-rate',
      startRate: ratePerMin(0.25),
      timeUnit: '1m',
      // Один VU = один посетитель в визите. Запас 1.6× — на разъезд длительностей
      // (посетитель, попавший в холодный старт, живёт дольше среднего).
      preAllocatedVUs: Math.ceil(PEAK * 0.5),
      maxVUs: Math.ceil(PEAK * 1.6) + 20,
      stages: [
        { target: ratePerMin(0.25), duration: RAMP },
        { target: ratePerMin(0.25), duration: STAGE },
        { target: ratePerMin(0.5), duration: RAMP },
        { target: ratePerMin(0.5), duration: STAGE },
        { target: ratePerMin(1.0), duration: RAMP },
        { target: ratePerMin(1.0), duration: STAGE },
        { target: ratePerMin(1.5), duration: RAMP },
        { target: ratePerMin(1.5), duration: STAGE },
        { target: 0, duration: RAMP },
      ],
    },
  },
  thresholds: THRESHOLDS,
  // Сводка по каждому эндпоинту отдельно — иначе быстрый каталог размажет
  // медленный чат по общему p95.
  summaryTrendStats: ['avg', 'min', 'med', 'p(90)', 'p(95)', 'p(99)', 'max'],
};

export function setup() {
  checkFixtures();
  const target = guardTarget();
  console.log(
    `Профиль: пик ${PEAK} одновременных посетителей, визит ${Math.round(VISIT_SEC / 60)} мин ` +
      `(TIME_SCALE=${TIME_SCALE}), приход на 100%-ступени ${ratePerMin(1.0)}/мин`
  );
  return target;
}

export default function () {
  visit();
}
