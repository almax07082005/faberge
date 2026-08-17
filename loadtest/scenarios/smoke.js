// Прогон-проверка оснастки: 1 посетитель, 1 проход, ноль нагрузки.
//
// Гонять ПЕРЕД каждым настоящим прогоном: ловит битые fixtures.json, неверный
// BASE_URL, 415 на фото, отвалившийся стенд — до того, как на них потрачен час.
//
//   k6 run -e BASE_URL=http://localhost:8000 -e TIME_SCALE=1000 loadtest/scenarios/smoke.js
import { guardTarget } from '../lib/config.js';
import { checkFixtures } from '../lib/fixtures.js';
import { visit } from '../lib/journey.js';

export const options = {
  vus: 1,
  iterations: 1,
  // Пороги здесь не про производительность: одиночный проход ничего не меряет.
  // Единственное требование — ни один запрос не должен упасть.
  thresholds: { 'http_req_failed': ['rate==0'] },
};

export function setup() {
  checkFixtures();
  return guardTarget();
}

export default function () {
  visit();
}
