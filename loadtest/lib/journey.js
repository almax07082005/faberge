// Путь одного посетителя: те же вызовы и в том же порядке, что делает фронт.
//
// Используется и в visitor.js (профиль музея), и в burst.js (толпа на входе).
// ceiling.js работает мимо этого файла — там нужен один эндпоинт в чистом виде.
import http from 'k6/http';
import { check, sleep } from 'k6';
import { BASE_URL, PROFILE, SKIP_EXTERNAL, TIME_SCALE, params } from './config.js';
import { FIXTURES, PHOTO, markedUuid, pick, chance } from './fixtures.js';

// ── Телеметрия ───────────────────────────────────────────────────────────────
// Фронт копит события и шлёт батчем; батч ≤ 50 (schemas.py MAX_EVENTS_PER_BATCH).
export function newVisitor() {
  return {
    sessionId: markedUuid(),
    deviceId: markedUuid(),
    buffer: [],
  };
}

export function track(v, type, fields) {
  v.buffer.push(Object.assign({ type: type, ts: new Date().toISOString() }, fields || {}));
  if (v.buffer.length >= PROFILE.telemetryBatchSize) flush(v);
}

export function flush(v) {
  if (!v.buffer.length) return;
  const body = JSON.stringify({
    session_id: v.sessionId,
    device_id: v.deviceId,
    events: v.buffer.splice(0, 50),
  });
  const res = http.post(`${BASE_URL}/telemetry/events`, body, params('telemetry'));
  check(res, { 'telemetry 202': (r) => r.status === 202 });
}

// Пауза «посетитель смотрит/читает», сжатая на TIME_SCALE.
function think(seconds) {
  const s = seconds / TIME_SCALE;
  if (s > 0) sleep(s);
}

// ── Шаги ─────────────────────────────────────────────────────────────────────
function openApp(v) {
  const res = http.batch([
    ['GET', `${BASE_URL}/map`, null, params('catalog')],
    ['GET', `${BASE_URL}/halls`, null, params('catalog')],
  ]);
  check(res[0], { 'map 200': (r) => r.status === 200 });
  check(res[1], { 'halls 200': (r) => r.status === 200 });
  track(v, 'app_open', { props: { entry: 'qr' } });
}

function viewHall(v, hallId) {
  const res = http.batch([
    ['GET', `${BASE_URL}/halls/${hallId}`, null, params('catalog')],
    ['GET', `${BASE_URL}/halls/${hallId}/exhibits`, null, params('catalog')],
  ]);
  check(res[0], { 'hall 200': (r) => r.status === 200 });
  track(v, 'hall_view', { hall_id: hallId });
}

function viewExhibit(v, ex) {
  const res = http.get(`${BASE_URL}/exhibits/${ex.id}`, params('catalog'));
  check(res, { 'exhibit 200': (r) => r.status === 200 });
  track(v, 'exhibit_view', { exhibit_id: ex.id, hall_id: ex.hall_id });
}

function search(v) {
  const q = pick(FIXTURES.queries);
  const res = http.get(`${BASE_URL}/search?q=${encodeURIComponent(q)}&limit=20`, params('search'));
  check(res, { 'search 200': (r) => r.status === 200 });
  track(v, 'search_query', { props: { text: q, results_count: 0 } });
}

function recognize(v, hallId) {
  if (SKIP_EXTERNAL) return;
  const form = {
    file: http.file(PHOTO, 'photo.jpg', 'image/jpeg'),
    hall_id: String(hallId),
    top_k: '3',
  };
  const res = http.post(`${BASE_URL}/recognition`, form, params('recognition', { headers: {} }));
  const ok = check(res, { 'recognition 200': (r) => r.status === 200 });
  // props.retry — второй заход после неудачного распознавания (телеметрия C25).
  track(v, 'recognition', {
    hall_id: hallId,
    props: { recognized: ok, confidence: 0.0, retry: false },
  });
}

function story(v, ex) {
  if (SKIP_EXTERNAL) return;
  const body = JSON.stringify({
    exhibit_id: ex.id,
    style: 'engaging',
    language: 'ru',
    include_audio: false,
    max_questions: 4,
  });
  const res = http.post(`${BASE_URL}/guide/story`, body, params('story'));
  check(res, { 'story 200': (r) => r.status === 200 });
}

function chat(v, ex) {
  if (SKIP_EXTERNAL) return;
  const body = JSON.stringify({
    session_id: v.sessionId,
    context: { exhibit_id: ex.id, hall_id: ex.hall_id },
    message: pick(FIXTURES.questions),
    language: 'ru',
    max_questions: 3,
  });
  const res = http.post(`${BASE_URL}/guide/chat`, body, params('chat'));
  check(res, { 'chat 200': (r) => r.status === 200 });
  track(v, 'chat_message', { exhibit_id: ex.id, props: { text: 'нагрузочный прогон' } });
}

function speak(v, ex) {
  if (SKIP_EXTERNAL) return;
  const body = JSON.stringify({
    exhibit_id: ex.id,
    voice: 'alena',
    format: 'mp3',
    speed: 1.0,
    emotion: 'neutral',
  });
  const res = http.post(`${BASE_URL}/speech`, body, params('tts'));
  check(res, { 'speech 200': (r) => r.status === 200 });
  track(v, 'tts_play', { exhibit_id: ex.id });
}

// ── Визит целиком ────────────────────────────────────────────────────────────
export function visit() {
  const v = newVisitor();
  const p = PROFILE;

  // Бюджет действий на визит размазывается по залам: посетитель не делает
  // пять распознаваний подряд в одном зале.
  const perHall = {
    recognition: p.recognitions / p.halls,
    chat: p.chats / p.halls,
    story: p.stories / p.halls,
    tts: p.ttsPlays / p.halls,
    search: p.searches / p.halls,
  };
  // Секунды на зал: визит минус то, что съедят сами запросы.
  const dwell = (p.visitMinutes * 60) / p.halls;

  openApp(v);
  think(5);

  const halls = FIXTURES.halls.slice(0, p.halls);
  for (let i = 0; i < halls.length; i++) {
    const hallId = halls[i].id;
    viewHall(v, hallId);
    think(4);

    const inHall = FIXTURES.exhibits.filter((e) => e.hall_id === hallId);
    const pool = inHall.length ? inHall : FIXTURES.exhibits;

    for (let j = 0; j < p.exhibitViewsPerHall; j++) {
      const ex = pick(pool);
      viewExhibit(v, ex);
      think(3);

      if (chance(perHall.recognition / p.exhibitViewsPerHall)) recognize(v, hallId);
      if (chance(perHall.story / p.exhibitViewsPerHall)) story(v, ex);
      if (chance(perHall.chat / p.exhibitViewsPerHall)) chat(v, ex);
      if (chance(perHall.tts / p.exhibitViewsPerHall)) speak(v, ex);
      if (chance(perHall.search / p.exhibitViewsPerHall)) search(v);

      think(dwell / p.exhibitViewsPerHall - 3);
    }
  }

  track(v, 'session_end', { props: { reason: 'exit', last_screen: 'map' } });
  flush(v);
}
