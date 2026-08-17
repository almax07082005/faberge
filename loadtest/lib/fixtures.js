// Данные для сценариев: реальные id из каталога + генераторы идентификаторов.
//
// fixtures.json собирает scripts/loadtest_prepare.py с живого API — сценарии
// ходят по существующим залам и экспонатам, а не по выдуманным id (иначе
// меряли бы скорость 404, а не рабочий путь).
import { fail } from 'k6';

export const FIXTURES = JSON.parse(open('../fixtures.json'));

// Фото для POST /recognition. Открывается в init-контексте один раз на VU.
export const PHOTO = open('../fixtures/photo.jpg', 'b');

// ── Маркер нагрузочных данных ────────────────────────────────────────────────
// ВСЕ session_id и device_id прогона начинаются с этого префикса. Он и есть
// способ вычистить следы: props отфильтрован белым списком (schemas.py §10),
// пометить событие «своим» полем нельзя, а по префиксу UUID — можно.
// См. loadtest/cleanup.sql.
export const MARKER = '10adfe57';

function hex(n) {
  let s = '';
  for (let i = 0; i < n; i++) s += '0123456789abcdef'[Math.floor(Math.random() * 16)];
  return s;
}

// UUID с маркером в первой группе. Версия/вариант проставлены корректно, чтобы
// значение принимали и Pydantic, и тип uuid в PostgreSQL.
export function markedUuid() {
  return `${MARKER}-${hex(4)}-4${hex(3)}-8${hex(3)}-${hex(12)}`;
}

export function pick(arr) {
  return arr[Math.floor(Math.random() * arr.length)];
}

export function chance(p) {
  return Math.random() < p;
}

export function checkFixtures() {
  const f = FIXTURES;
  if (!f.halls || !f.halls.length) fail('fixtures.json: пустой список halls — перезапустите scripts/loadtest_prepare.py');
  if (!f.exhibits || !f.exhibits.length) fail('fixtures.json: пустой список exhibits');
  if (!f.queries || !f.queries.length) fail('fixtures.json: пустой список queries');
  if (!f.questions || !f.questions.length) fail('fixtures.json: пустой список questions');
  return true;
}
