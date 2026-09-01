"""Блок вопросов-подсказок ИИ-гида (баг-репорт музея 31.08.2026, пп. II-2/II-3/II-7/II-8).

Что здесь закрепляется — по пунктам музея:

  • II-2 «предлагает неограниченное количество вопросов, которые постоянно
    перефразирует»: пул перестал быть ПРЕФИКСОМ, и вопрос, только что заданный
    посетителем, не возвращается в блок ни дословно, ни другими словами;
  • II-3 «предлагаются вопросы, ответы на которые AI-экскурсовод не знает»:
    формулировка, на которую гид уже отказался отвечать по этому экспонату,
    больше не предлагается — и не только в той сессии, где случился отказ;
  • II-7 «после выбора одного вопроса и получения ответа варианты вопросов уже
    не предлагаются»: блок непустой во ВСЕХ ветках `/guide/chat`;
  • II-8 «подобные вопросы не нужны»: запреты применяются на выдаче (сами
    шаблоны живут и проверяются в `tests/test_guide_style.py`).

Отдельно проверяется, что починка одного пункта не ломает соседний: если
исключения выедают пул досуха, блок обязан наполниться детерминированным
запасом, а не опустеть (иначе II-2/II-8 своими руками воспроизводят II-7).

БД и сеть не нужны: слой хранения (`crud`) и вызовы модели (`llm`) подменяются,
`AsyncSession` заменён минимальным двойником. Запуск:
    python -m pytest tests/test_guide_suggestions.py
    python tests/test_guide_suggestions.py       # standalone
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import crud  # noqa: E402
from app import models as m  # noqa: E402
from app import schemas as sch  # noqa: E402
from app.config import settings  # noqa: E402
from app.routers import guide  # noqa: E402
from app.services import UpstreamError, guide_questions, llm  # noqa: E402


# ── Данные ──────────────────────────────────────────────────────────────────
EXHIBIT = {
    "id": 144,
    "exhibit_number": "12",
    "label_slug": "faberge_egg_marlborough",
    "name": "Яйцо герцогини Мальборо",
    "year_created": "1902",
    "master_name": "Фирма К. Фаберже, мастер М. Перхин",
    "material": "золото, эмаль",
    "techniques": "гильошировка",
    "short_description": "Пасхальное яйцо-часы.",
    "raw_history": "Заказано герцогиней Мальборо.",
}

# Пул из восьми РАЗНЫХ по смыслу вопросов — столько (GUIDE_QUESTIONS_CACHE_SIZE)
# лежит в записи кэша, наружу уходит срез под max_questions.
POOL = [
    "Кому подарили это яйцо?",
    "Что за сюрприз спрятан внутри?",
    "Из каких материалов он сделан?",
    "Где яйцо хранилось после революции?",
    "Что известно о его владельцах?",
    "Как оно попало в музей?",
    "Какие ещё яйца делал Перхин?",
    "Сколько яиц создала фирма Фаберже?",
]

# Дословные пары со скриншотов музея (п. II-2 и п. II-3): посетитель спросил
# левое, получил ответ — и следующей подсказкой ему предлагают правое.
PARAPHRASES = (
    (
        "Какие именно скифские мотивы использованы в браслете?",
        "Какие именно скифские мотивы Эрик Коллин использовал в дизайне браслета?",
    ),
    (
        "Как функционирует механизм со стрелкой-змейкой?",
        "Как функционирует механизм вращающейся стрелки-змейки?",
    ),
    (
        "Почему яйцо стало единственным, заказанным герцогиней Мальборо?",
        "Почему яйцо стало единственным заказом герцогини Мальборо у Фаберже?",
    ),
)


class StubHall:
    def __init__(self, hall_id=4, number=4, name="Синяя гостиная", description="Зал с витринами.",
                 is_temporary=False):
        self.id = hall_id
        self.hall_number = number
        self.name = name
        self.description = description
        self.is_temporary = is_temporary


class StubExhibit:
    """ORM-экспонат в объёме, который читают crud.exhibit_to_dict и сериализаторы гида."""

    def __init__(self, data=None, showcase=None):
        for field, value in (data or EXHIBIT).items():
            setattr(self, field, value)
        self.image_url = None
        self.showcase = showcase


class FakeResult:
    """Результат session.execute: истории диалога в этих тестах нет."""

    def scalars(self):
        return self

    def all(self):
        return []


class FakeSession:
    """Двойник AsyncSession: только то, что зовёт роутер напрямую.

    Всё, что ходит в БД через `crud`, подменяется целыми функциями — так тест
    проверяет ЛОГИКУ роутера, а не умение собрать SQL.
    """

    def __init__(self, stored=None):
        self.stored = stored or {}          # (имя модели, ключ) → объект
        self.added = []
        self.commits = 0

    async def get(self, model, ident):
        return self.stored.get((model.__name__, ident))

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        # Сессия диалога получает id только при записи в БД (default=uuid4).
        for obj in self.added:
            if isinstance(obj, m.GuideSession) and obj.id is None:
                obj.id = uuid.uuid4()

    async def execute(self, _stmt):
        return FakeResult()

    async def commit(self):
        self.commits += 1


@contextlib.contextmanager
def wired(
    *,
    pool=None,
    row=None,
    exhibit=None,
    number_matches=(),
    halls=(),
    answer="Ответ гида.",
    questions_error=False,
    session_history=(),
    global_refusals=(),
    **overrides,
):
    """Подменить хранилище и модель. Возвращает журнал вызовов.

    `session_history` — что вернёт `crud.session_asked_questions`: список
    кортежей (текст, answered, fail_reason, exhibit_id). `global_refusals` — что
    вернёт глобальная память об отказах по экспонату.
    """
    calls = {"llm_questions": [], "saved": [], "refusal_query": []}
    saved_settings = {k: getattr(settings, k) for k in overrides}
    original = {name: getattr(crud, name) for name in (
        "get_exhibit_orm", "get_exhibit_by_slug_orm", "exhibits_by_number", "all_halls_ordered",
        "search_exhibits_orm", "session_asked_questions", "exhibit_refused_questions",
        "get_exhibit_questions", "save_exhibit_questions",
    )}
    original_llm = {"chat": llm.chat, "suggested_questions": llm.suggested_questions}

    async def fake_get_exhibit(_session, _ident):
        return exhibit

    async def fake_by_number(_session, _number):
        return list(number_matches)

    async def fake_halls(_session, include_service=False):
        return list(halls)

    async def fake_search(_session, _text, limit=4):
        return []

    async def fake_asked(_session, _session_id, limit=50):
        return list(session_history)

    async def fake_refused(_session, exhibit_id, min_count=2, days=90, limit=200):
        calls["refusal_query"].append({"exhibit_id": exhibit_id, "min_count": min_count, "days": days})
        return list(global_refusals)

    async def fake_get_questions(_session, _exhibit_id, _language="ru"):
        return row

    async def fake_save(_session, exhibit_id, language, questions, source_hash, model=None):
        calls["saved"].append({"exhibit_id": exhibit_id, "questions": list(questions)})

    async def fake_suggested(_exhibit, max_questions, language="ru"):
        calls["llm_questions"].append(max_questions)
        if questions_error:
            raise UpstreamError("Сервис генерации текста временно недоступен.")
        return list(pool if pool is not None else POOL)[:max_questions], "gpt://test/yandexgpt/latest"

    async def fake_chat(_grounding, _history, _message, _language="ru"):
        return answer

    crud.get_exhibit_orm = fake_get_exhibit
    crud.get_exhibit_by_slug_orm = fake_get_exhibit
    crud.exhibits_by_number = fake_by_number
    crud.all_halls_ordered = fake_halls
    crud.search_exhibits_orm = fake_search
    crud.session_asked_questions = fake_asked
    crud.exhibit_refused_questions = fake_refused
    crud.get_exhibit_questions = fake_get_questions
    crud.save_exhibit_questions = fake_save
    llm.chat = fake_chat
    llm.suggested_questions = fake_suggested
    for key, value in overrides.items():
        setattr(settings, key, value)
    try:
        yield calls
    finally:
        for name, value in original.items():
            setattr(crud, name, value)
        for name, value in original_llm.items():
            setattr(llm, name, value)
        for key, value in saved_settings.items():
            setattr(settings, key, value)


class FakeRow:
    """Строка exhibit_questions."""

    def __init__(self, questions, source_hash=None, exhibit=None):
        self.questions = list(questions)
        self.source_hash = source_hash or guide_questions.fingerprint(exhibit or EXHIBIT)
        self.model = "gpt://test/yandexgpt/latest"


def ask(message="Расскажи об экспонате", **kwargs):
    """Тело запроса /guide/chat. `context` передаётся только если он назван явно."""
    body = {"message": message}
    body.update(kwargs)
    return sch.ChatRequest(**body)


def run_chat(req, session=None, **wiring):
    session = session or FakeSession()
    with wired(**wiring) as calls:
        response = asyncio.run(guide.chat(req, session))
    return response, calls


# ═════════════════════════════════════════════════════════════════════════════
# 1. II-2 — перефразировки и «вечный префикс пула»
# ═════════════════════════════════════════════════════════════════════════════
def test_paraphrase_is_not_offered_after_the_original():
    """Дословные пары со скриншотов: спросили левое — правое из блока исчезает."""
    for original, paraphrase in PARAPHRASES:
        picked = guide_questions.select_questions(
            [paraphrase, "Кому подарили это яйцо?"], 3, exhibit=EXHIBIT, asked=[original]
        )
        assert paraphrase not in picked, f"перефразировка осталась в подсказках: {paraphrase}"
        assert "Кому подарили это яйцо?" in picked, "заодно выбросили нормальный вопрос"


def test_pool_stops_being_a_prefix():
    """Главная механика II-2: после ответа посетитель видит СЛЕДУЮЩИЕ вопросы пула.

    Раньше наружу уходило `questions[:max_questions]` — и первый вопрос пула
    стоял в блоке первым и на пятой реплике подряд.
    """
    first = guide_questions.select_questions(POOL, 3, exhibit=EXHIBIT)
    assert first == POOL[:3]
    second = guide_questions.select_questions(POOL, 3, exhibit=EXHIBIT, asked=[POOL[0]])
    assert POOL[0] not in second
    assert second[0] == POOL[1], "блок должен сдвигаться по пулу, а не пересобираться заново"


def test_asked_question_leaves_the_block_in_the_dialogue():
    """То же, но через /guide/chat: реплики ещё нет в БД, а из подсказок она уже ушла.

    `_add_messages` пишет вопрос в конце обработки, поэтому текущее сообщение
    подставляется в исключения отдельно — без этого посетитель после ответа
    видел бы свой же вопрос первым в блоке (дословный скриншот п. II-2).
    """
    response, _ = run_chat(
        ask(POOL[0], context={"exhibit_id": 144}),
        exhibit=StubExhibit(),
        row=FakeRow(POOL),
    )
    assert POOL[0] not in response.suggested_questions
    assert response.suggested_questions, "блок при этом не должен опустеть"


def test_question_asked_at_another_exhibit_is_still_offered_here():
    """Память сессии привязана к предмету — иначе починка II-2 родила бы новый баг.

    У пасхальных яиц пулы подсказок совпадают дословно («Кому подарили это
    яйцо?»). Если исключать по всей сессии без разбора, посетитель, идущий по
    залу, к третьей витрине остался бы без единой нормальной подсказки.
    """
    response, _ = run_chat(
        ask("Расскажи про яйцо", context={"exhibit_id": 144}),
        exhibit=StubExhibit(),
        row=FakeRow(POOL),
        # тот же вопрос, но задан у СОСЕДНЕГО экспоната
        session_history=[(POOL[0], True, None, 999)],
    )
    assert POOL[0] in response.suggested_questions


def test_dedupe_keeps_the_original_order_of_the_pool():
    """Регресс на порядок: пул отдаётся в порядке модели, а не пересортированным.

    `question_cluster.cluster_questions` сортирует по частоте — для аналитики это
    правильно, для подсказок означало бы алфавитную кашу вместо порядка, в
    котором вопросы задумывала модель.
    """
    assert guide_questions.select_questions(POOL, 8, exhibit=EXHIBIT) == POOL


# ═════════════════════════════════════════════════════════════════════════════
# 2. II-3 — память об отказах
# ═════════════════════════════════════════════════════════════════════════════
def test_refused_question_is_not_offered_again():
    """Цепочка со скриншота: предложили → спросили → «не знаю» → предложили снова."""
    refused = "Как функционирует механизм со стрелкой-змейкой?"
    picked = guide_questions.select_questions(
        [refused] + POOL[:3], 3, exhibit=EXHIBIT, refused=[refused]
    )
    assert refused not in picked
    assert len(picked) == 3, "остальной пул от этого страдать не должен"


def test_refusal_is_remembered_across_sessions():
    """Решение Д8: отказ помнится глобально, а не в пределах одной сессии.

    Сессия здесь ЧИСТАЯ (`session_history` пуст) — вопрос исключается только
    потому, что гид отказывался отвечать на него по этому экспонату раньше и
    другим посетителям.
    """
    refused = "Как функционирует механизм со стрелкой-змейкой?"
    response, calls = run_chat(
        ask("Расскажи про яйцо", context={"exhibit_id": 144}),
        exhibit=StubExhibit(),
        row=FakeRow([refused] + POOL[:4]),
        global_refusals=[refused],
    )
    assert refused not in response.suggested_questions
    assert calls["refusal_query"], "глобальную память надо было спросить"
    assert calls["refusal_query"][0]["exhibit_id"] == 144


def test_refusal_memory_uses_the_configured_threshold_and_age():
    """Порог и срок берутся из настроек (требование брифа), а не зашиты в коде."""
    _response, calls = run_chat(
        ask("Расскажи про яйцо", context={"exhibit_id": 144}),
        exhibit=StubExhibit(),
        row=FakeRow(POOL),
        guide_refusal_memory_min_count=5,
        guide_refusal_memory_days=30,
    )
    assert calls["refusal_query"][0]["min_count"] == 5
    assert calls["refusal_query"][0]["days"] == 30


def test_refusal_memory_can_be_switched_off():
    """GUIDE_REFUSAL_MEMORY_ENABLED=false — запрос к глобальной памяти не идёт вовсе."""
    _response, calls = run_chat(
        ask("Расскажи про яйцо", context={"exhibit_id": 144}),
        exhibit=StubExhibit(),
        row=FakeRow(POOL),
        guide_refusal_memory_enabled=False,
    )
    assert calls["refusal_query"] == []


def test_network_failure_is_not_a_refusal():
    """`fail_reason='error'` — это упавший LLM, а не «гид не знает».

    Считать его отказом значило бы навсегда прятать нормальный вопрос из-за
    одного обрыва связи.
    """
    session = FakeSession()
    with wired(session_history=[
        ("Кому подарили это яйцо?", False, "error", 144),
        ("Где оно хранилось после революции?", False, "not_found", 144),
        ("Как работает механизм?", False, "llm_refusal", 144),
        ("Что за сюрприз внутри?", True, None, 144),
    ]):
        asked, refused = asyncio.run(guide._session_memory(session, uuid.uuid4(), "Текущий вопрос?"))
    assert guide._scoped(refused, 144) == ["Как работает механизм?"]
    assert "Текущий вопрос?" in guide._scoped(asked, 144) and len(asked) == 5
    # Текущая реплика не привязана к предмету — она исключается в любой ветке.
    assert guide._scoped(asked) == ["Текущий вопрос?"]


def _fail_reason_of(session):
    """Причина, записанная роутером в пару «вопрос — ответ» этой реплики."""
    reasons = {msg.fail_reason for msg in session.added if isinstance(msg, m.GuideMessage)}
    assert len(reasons) == 1, f"на паре строк разъехалась причина: {reasons}"
    return reasons.pop()


def test_full_refusal_feeds_the_refusal_memory():
    """Отказ ЦЕЛИКОМ — дословно со скриншота п. II-3 — получает причину из выборки памяти."""
    session = FakeSession()
    run_chat(
        ask("Как функционирует механизм со стрелкой-змейкой?", context={"exhibit_id": 144}),
        session=session,
        exhibit=StubExhibit(),
        row=FakeRow(POOL),
        answer=(
            "К сожалению, я не знаю, как работает механизм со стрелкой-змейкой "
            "в яйце герцогини Мальборо."
        ),
    )
    reason = _fail_reason_of(session)
    assert reason == "llm_refusal"
    assert reason in guide._REFUSAL_REASONS, "иначе п. II-3 перестал бы чиниться"


def test_answer_with_a_hedge_is_not_written_as_a_refusal():
    """Оговорка в содержательном ответе НЕ кормит глобальную память отказов.

    Это защита от ложных срабатываний, которые дороже пропусков: память глобальная
    (решение Д8), и одна лишняя причина `llm_refusal` прячет нормальный вопрос из
    подсказок у ВСЕХ посетителей экспоната на GUIDE_REFUSAL_MEMORY_DAYS дней —
    молча, без строки в логах. Промпт диалога при этом сам просит модель писать
    «этого точно не знаю» одной фразой, то есть на широком признаке мы наказывали
    бы её за требуемое поведение.
    """
    session = FakeSession()
    run_chat(
        ask("Кому подарили это яйцо?", context={"exhibit_id": 144}),
        session=session,
        exhibit=StubExhibit(),
        row=FakeRow(POOL),
        answer=(
            "Яйцо изготовлено в 1902 году в мастерской Михаила Перхина по заказу "
            "герцогини Мальборо. Точной даты вручения я не знаю, но известно, что "
            "оно оставалось в её собрании до 1926 года."
        ),
    )
    reason = _fail_reason_of(session)
    assert reason == "llm_hedge"
    # Главное утверждение пункта: такая причина не входит ни в сессионную, ни в
    # глобальную выборку отказов — вопрос останется в подсказках.
    assert reason not in guide._REFUSAL_REASONS


def test_refused_question_does_not_come_back_even_when_everything_else_is_gone():
    """Ярус-страховка не отменяет память об отказе.

    Блок не должен пустеть (п. II-7), но наполнять его тем самым вопросом, на
    который гид только что не ответил, — это ровно жалоба п. II-3.
    """
    refused = "Расскажи об этом предмете подробнее."
    picked = guide_questions.select_questions(
        [], 3, exhibit={"id": 1, "name": "Пустая карточка"}, refused=[refused]
    )
    assert picked, "блок пустеть не должен"
    assert refused not in picked


# ═════════════════════════════════════════════════════════════════════════════
# 3. Запас: пул кончился, но блок не пустеет
# ═════════════════════════════════════════════════════════════════════════════
def test_exhausted_pool_falls_back_to_the_card():
    """Все восемь вопросов пула уже заданы — блок наполняется запасом по карточке."""
    picked = guide_questions.select_questions(POOL, 3, exhibit=EXHIBIT, asked=POOL)
    assert picked, "исключения не имеют права оставить блок пустым"
    assert not set(picked) & set(POOL)
    assert set(picked) <= set(guide_questions.fallback_questions(EXHIBIT))


def test_fallback_mentions_only_filled_fields():
    """Запас строится от ЗАПОЛНЕННЫХ полей: спрашивать о том, чего в карточке нет,
    — это и есть «вопросы, ответа на которые гид не знает» (п. II-3)."""
    bare = {"id": 7, "name": "Витрина без описания"}
    picked = guide_questions.fallback_questions(bare)
    assert picked == ["Расскажи об этом предмете подробнее."]
    rich = guide_questions.fallback_questions(EXHIBIT)
    assert "В какой технике он выполнен?" in rich
    assert "Кто его создал?" in rich
    without_master = dict(EXHIBIT, master_name=None, techniques="")
    thin = guide_questions.fallback_questions(without_master)
    assert "Кто его создал?" not in thin and "В какой технике он выполнен?" not in thin


def test_last_resort_is_the_museum_set():
    """Даже пустая карточка и исчерпанный запас оставляют блок непустым."""
    spare = guide_questions.fallback_questions(None)
    picked = guide_questions.select_questions([], 3, exhibit=None, asked=spare)
    assert picked and set(picked) <= set(guide_questions.MUSEUM_QUESTIONS)


def test_short_pool_is_not_padded_with_fallback():
    """Регресс к кэшу: `max_questions` — потолок, а не требование.

    Если добирать запасом всегда, короткий (но актуальный) набор из кэша
    переставал бы выглядеть коротким, и договорённость 26.08.2026 «длину записи
    не проверяем» потеряла бы смысл.
    """
    assert guide_questions.select_questions(POOL[:2], 4, exhibit=EXHIBIT) == POOL[:2]


# ═════════════════════════════════════════════════════════════════════════════
# 4. II-7 — блок подсказок непустой во всех ветках /guide/chat
# ═════════════════════════════════════════════════════════════════════════════
def test_suggestions_after_a_unique_number_lookup():
    """Сценарий 1: посетитель ввёл «12» и раньше оставался без единой подсказки."""
    response, _ = run_chat(
        ask("12"),
        number_matches=[StubExhibit()],
        exhibit=StubExhibit(),
        row=FakeRow(POOL),
    )
    assert response.suggested_questions, "поиск по номеру обязан продолжать разговор"


def test_ambiguous_number_still_returns_where_hints():
    """Ветку уточнения по неуникальному номеру (B9) НЕ трогаем.

    Там в `suggested_questions` лежат не вопросы, а варианты расположения, и
    прогонять их через фильтр подсказок нельзя — уточняющий диалог сломается.
    """
    hall = StubHall()

    class StubShowcase:
        id, showcase_number, name = 1, 5, None

    showcase = StubShowcase()
    showcase.hall = hall
    matches = [StubExhibit(showcase=showcase), StubExhibit(showcase=showcase)]
    response, _ = run_chat(ask("12"), number_matches=matches)
    assert response.suggested_questions == ["В зале 4 «Синяя гостиная», витрина 5"] * 2


def test_suggestions_after_the_hall_listing():
    """Сценарий 2: «какие есть залы» — ответ приходил без продолжения."""
    halls = [StubHall(1, 1, "Рыцарский зал"), StubHall(4, 4, "Синяя гостиная")]
    response, calls = run_chat(ask("какие есть залы"), halls=halls)
    assert response.suggested_questions == [
        "Что интересного в зале 1 «Рыцарский зал»?",
        "Что интересного в зале 4 «Синяя гостиная»?",
    ]
    assert calls["llm_questions"] == [], "подсказки к списку залов не стоят вызова модели"


def test_suggestions_with_hall_context_only():
    """Сценарий 3: в контексте зал, экспоната нет — подсказок не было вообще."""
    session = FakeSession(stored={("Hall", 4): StubHall()})
    response, calls = run_chat(
        ask("Расскажи про зал", context={"hall_id": 4}), session=session, exhibit=None
    )
    assert response.suggested_questions == list(guide_questions.HALL_QUESTIONS)[:3]
    assert calls["llm_questions"] == []


def test_suggestions_in_the_general_chat():
    """Сценарий 4: общий чат без контекста — тоже пустой блок."""
    response, calls = run_chat(ask("Когда открылся музей?"))
    assert response.suggested_questions == list(guide_questions.MUSEUM_QUESTIONS)[:3]
    assert calls["llm_questions"] == []


def test_suggestions_survive_an_unavailable_model():
    """Сценарий 6 (тихий): `except UpstreamError: questions = []` опустошал блок.

    Ответ гида к этому моменту уже получен и оплачен — ронять из-за подсказок
    нечего, но и оставлять посетителя без продолжения незачем.
    """
    response, _ = run_chat(
        ask("Расскажи про яйцо", context={"exhibit_id": 144}),
        exhibit=StubExhibit(),
        row=None,
        questions_error=True,
    )
    assert response.suggested_questions
    assert set(response.suggested_questions) <= set(guide_questions.fallback_questions(EXHIBIT))


def test_general_chat_block_survives_a_long_conversation():
    """Наборы вне экспоната маленькие — исключения не должны их обнулить.

    Посетитель задал в общем чате все четыре вопроса набора: повторить общий
    вопрос про музей не страшно (отказов сюда не передаётся вовсе), а пустой
    блок — это ровно п. II-7.
    """
    history = [(q, True, None, None) for q in guide_questions.MUSEUM_QUESTIONS]
    response, _ = run_chat(ask("Когда открылся музей?"), session_history=history)
    assert response.suggested_questions == list(guide_questions.MUSEUM_QUESTIONS)[:3]


def test_zero_max_questions_still_means_zero():
    """`max_questions=0` — это явная просьба клиента, а не «пул кончился»."""
    response, _ = run_chat(ask("Когда открылся музей?", max_questions=0))
    assert response.suggested_questions == []


# ═════════════════════════════════════════════════════════════════════════════
# 5. Д7 — диагностика пустого контекста
# ═════════════════════════════════════════════════════════════════════════════
@contextlib.contextmanager
def captured_warnings():
    """Собрать предупреждения роутера гида (логгер — единственная телеметрия здесь).

    Отдельного события телеметрии не заводим намеренно: словарь `EventType` —
    контракт с фронтом (§1), и backend-only тип раздувал бы отчёты о поведении
    посетителей событием, которого посетитель не совершал.
    """
    records = []

    class Collector(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    handler = Collector()
    guide.logger.addHandler(handler)
    try:
        yield records
    finally:
        guide.logger.removeHandler(handler)


def _session_with_exhibit_context():
    sess = m.GuideSession(context={"exhibit_id": 144, "label_slug": None, "hall_id": 3})
    sess.id = uuid.uuid4()
    return sess


def test_blank_context_on_an_exhibit_session_is_reported():
    """Самая вероятная причина II-7 у заказчика — «context»: null на каждой реплике.

    Поведение сброса НЕ меняем (это фикс 28.07.2026, п.3, иначе вернётся
    «залипший» зал), но случай обязан быть НАЗЫВАЕМЫМ: без строки в логе мы не
    сможем сказать музею, почему подсказки пропадали именно у них.
    """
    sess = _session_with_exhibit_context()
    session = FakeSession(stored={("GuideSession", sess.id): sess})
    with captured_warnings() as records:
        run_chat(ask("Когда открылся музей?", session_id=sess.id, context=None), session=session)
    assert any(r.startswith("guide_context_reset") for r in records), records
    assert any("previous_exhibit_id=144" in r for r in records)


def test_reset_semantics_itself_is_unchanged():
    """Диагностика — это только лог: контекст по-прежнему сбрасывается."""
    sess = _session_with_exhibit_context()
    session = FakeSession(stored={("GuideSession", sess.id): sess})
    response, _ = run_chat(
        ask("Когда открылся музей?", session_id=sess.id, context=None), session=session
    )
    assert sess.context is None and response.context is None


def test_general_chat_without_previous_context_is_not_reported():
    """Ложных тревог быть не должно: вход в общий чат с главного экрана — норма."""
    sess = m.GuideSession(context=None)
    sess.id = uuid.uuid4()
    session = FakeSession(stored={("GuideSession", sess.id): sess})
    with captured_warnings() as records:
        run_chat(ask("Когда открылся музей?", session_id=sess.id, context=None), session=session)
    assert not [r for r in records if r.startswith("guide_context_reset")]


# ═════════════════════════════════════════════════════════════════════════════
# 6. Совместимость: кэш и прежние вызовы
# ═════════════════════════════════════════════════════════════════════════════
def test_memory_does_not_leak_into_the_cache():
    """В `exhibit_questions` уходит ПОЛНЫЙ сырой пул, а не то, что увидел посетитель.

    Иначе кэш деградировал бы от сессии к сессии: первый посетитель выел бы из
    записи свои вопросы, и второму досталось бы то, что осталось.
    """
    with wired(row=None, pool=POOL, guide_questions_cache_size=8) as calls:
        picked = asyncio.run(
            guide_questions.for_exhibit(
                None, EXHIBIT, 3, asked=[POOL[0], POOL[1]], refused=[POOL[2]]
            )
        )
    assert calls["saved"][0]["questions"] == POOL, "в таблицу пишем то, что отдала модель"
    assert picked == POOL[3:6], "а наружу — срез уже с исключениями"


def test_for_exhibit_without_memory_behaves_exactly_as_before():
    """Обратная совместимость: у прежних вызовов (без asked/refused) срез тот же."""
    with wired(row=FakeRow(POOL)) as calls:
        assert asyncio.run(guide_questions.for_exhibit(None, EXHIBIT, 4)) == POOL[:4]
    assert calls["llm_questions"] == [], "свежая запись по-прежнему не ходит в LLM"


def test_dedupe_switch_off_restores_the_plain_slice():
    """GUIDE_QUESTIONS_DEDUPE=false — прежнее поведение, но исключения остаются.

    Флаг откатывает склейку перефразировок ВНУТРИ пула (это косметика), но не
    память об отказах: «не предлагать то, на что гид уже не ответил» — это
    п. II-3. И сравнение с `refused` остаётся смысловым, а не побуквенным —
    иначе отказ обходился бы перестановкой одного слова, чего мы и добиваемся
    в п. II-2.
    """
    similar = ["Кому подарили это яйцо?", "Кому подарили яйцо?", "Что за сюрприз внутри?"]
    with wired(guide_questions_dedupe=False):
        assert guide_questions.select_questions(similar, 3, exhibit=EXHIBIT) == similar
        assert guide_questions.select_questions(
            similar, 3, exhibit=EXHIBIT, refused=[similar[0]]
        ) == ["Что за сюрприз внутри?"]


# ═════════════════════════════════════════════════════════════════════════════
# 7. Экран рассказа (п. II-7 дословно: «надо возвращаться назад»)
# ═════════════════════════════════════════════════════════════════════════════
def test_story_with_session_id_drops_the_asked_question():
    """Вернувшись на экран рассказа, посетитель не должен видеть заданный вопрос."""
    saved = llm.generate_story

    async def fake_story(_exhibit, _style, _language):
        return "Рассказ.", "gpt://test/yandexgpt/latest"

    llm.generate_story = fake_story
    try:
        with wired(exhibit=StubExhibit(), row=FakeRow(POOL),
                   session_history=[(POOL[0], True, None, 144)]):
            response = asyncio.run(
                guide.generate_story(
                    sch.StoryRequest(exhibit_id=144, session_id=uuid.uuid4()), FakeSession()
                )
            )
    finally:
        llm.generate_story = saved
    assert POOL[0] not in response.suggested_questions
    assert len(response.suggested_questions) == 4


def test_story_without_session_id_keeps_the_old_behaviour():
    """Старые клиенты поля не шлют — для них ничего не меняется."""
    saved = llm.generate_story

    async def fake_story(_exhibit, _style, _language):
        return "Рассказ.", "gpt://test/yandexgpt/latest"

    llm.generate_story = fake_story
    try:
        with wired(exhibit=StubExhibit(), row=FakeRow(POOL)):
            response = asyncio.run(
                guide.generate_story(sch.StoryRequest(exhibit_id=144), FakeSession())
            )
    finally:
        llm.generate_story = saved
    assert response.suggested_questions == POOL[:4]


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {fn.__name__}: {exc!r}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
