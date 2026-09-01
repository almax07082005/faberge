"""Юнит-тесты запретов музея в текстах гида (баг-репорт 31.08.2026, пп. II-4/II-5/II-6/II-8).

Что здесь проверяется и почему именно так:

  1. Три вопроса со скриншота п. II-8 отсекаются ДОСЛОВНО — это приёмка задачи.
  2. Нормальные вопросы НЕ отсекаются. Этот блок важнее первого: запрещённый
     вопрос посетитель просто не увидит и всегда может задать его сам, а хороший
     вопрос исчезает МОЛЧА и никто об этом не узнает. Перечень запретов узкий
     намеренно, и тест — предохранитель от его расползания.
  3. Фильтр «воды» — на дословных фразах со скриншотов п. II-5, плюс на строке
     каталога «Фирма К. Фаберже, мастер М. Перхин» (разбиение по инициалам).
  4. Дедупликация перефразировок — на паре со скриншота п. II-2.
  5. Прогретый кэш чистится НА ЧТЕНИИ. Это главный риск задачи: `source_hash`
     считается по тексту карточки, поэтому правка промпта не инвалидирует ни
     одной из 1200+ записей, и без фильтра на выдаче музей увидел бы после
     релиза ровно те же подсказки.
  6. Инварианты `_CHAT_SYSTEM` — сторож бага 28.07.2026, п.3: запрет на
     домысливание есть, а формулировки «только по справке» нет и не появится.

Сеть и БД не нужны: httpx подменяется тем же приёмом, что в tests/test_llm_cost.py,
слой хранения — харнессом из tests/test_guide_questions_cache.py. Запуск:
    python -m pytest tests/test_guide_style.py
    python tests/test_guide_style.py            # standalone
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services import guide_questions, guide_style, llm  # noqa: E402
from tests.test_guide_questions_cache import EXHIBIT, FakeExhibit, FakeRow, wired  # noqa: E402
from tests.test_llm_cost import LLM_ENV, gpt_response, patched  # noqa: E402


# ── Материал скриншотов музея ───────────────────────────────────────────────
# Блок подсказок со скриншота п. II-8 («подобные вопросы не нужны») — дословно.
SCREENSHOT_QUESTIONS = [
    "Сколько времени заняло создание этого предмета?",
    "Какие уникальные особенности есть у этой работы Михаила Перхина?",
    "Почему для украшения была выбрана именно жёлтая гильоше-эмаль?",
]

# Формулировки, названные музеем в тексте п. II-4, и их живые варианты.
MATERIAL_CHOICE_QUESTIONS = [
    "Почему мастер использовал именно эмаль-гильоше?",
    "Почему мастер использовал именно алмазы?",
    "Почему мастер использовал именно золото?",
    "Почему выбрали золото, а не серебро?",
    "Зачем мастер применил алмазы в отделке?",
    "Чем обусловлен выбор нефрита для этой вещи?",
    "Почему для корпуса выбрали именно горный хрусталь?",
    "С какой целью использовали перламутр в этой работе?",
]

DURATION_QUESTIONS = [
    "Сколько времени ушло на создание?",      # формулировка из llm._questions_stub
    "Как долго создавали яйцо?",
    "Сколько месяцев изготавливали это яйцо?",
    "Сколько времени потребовалось на изготовление этого предмета?",
    "Как долго трудились над яйцом?",
    "Сколько лет мастер работал над этим яйцом?",
]

# Вопросы о времени, которые музей НЕ запрещал и которые первая редакция шаблона
# сроков молча съедала (замечание Ж3 по второй волне проверки). Все шесть — про
# что угодно, кроме длительности изготовления: возраст, давность, биография,
# дорога, реставрация. Фильтр стоит на чтении и ничего не логирует, поэтому
# каждая такая потеря невидима — тест и есть единственный сторож.
TIME_QUESTIONS_THAT_MUST_SURVIVE = [
    "Сколько лет было Марии Фёдоровне, когда мастер создал это яйцо?",
    "Сколько лет прошло с тех пор, как создали яйцо?",
    "Сколько лет назад создали это яйцо?",
    "Сколько лет Фаберже работал над императорскими заказами?",
    "Сколько дней ушло на дорогу, когда яйцо везли из Петербурга?",
    "Сколько времени заняла реставрация этого предмета?",
    # Ю2: изготовление НЕ этого предмета. Вторая редакция шаблона держала
    # «сколько времени» и «создание» просто рядом, без объекта, — все пять
    # давали True и вырезались молча.
    "Сколько времени заняло создание всей императорской серии?",
    "Сколько времени заняло создание музейной экспозиции?",
    "Сколько времени уходило на изготовление одной миниатюры у Цейнграфа?",
    "Сколько лет ушло на создание Шуваловского дворца?",
    "Как долго изготавливали витрины для нового зала?",
    # Я2: тот же класс, но с указательным местоимением. В объекте изготовления
    # стояло голое «эт» из `_THIS_ITEM`, где местоимение привязано предлогом
    # («работал НАД этим»), — и все четыре резались молча. Прямое противоречие
    # соседним строкам этого же списка: «создание Шуваловского дворца» выживало,
    # а «создание этого дворца» — нет.
    "Сколько времени заняло создание этого зала?",
    "Сколько времени заняло создание этого дворца?",
    "Сколько времени заняло создание этой выставки?",
    "Сколько времени заняло создание этой коллекции?",
]

# Шестая формулировка из того же замечания — «Сколько месяцев делали копию яйца
# для выставки?» — остаётся зарезанной, и это ОГРАНИЧЕНИЕ, а не недосмотр: «яйца»
# там настоящий объект изготовления, отличить его от «этого яйца» можно только
# разбором связи «копию ← яйца». Тест фиксирует ограничение честно, чтобы оно
# было видно здесь, а не всплыло у музея.
TIME_QUESTION_STILL_CUT_BY_DESIGN = "Сколько месяцев делали копию яйца для выставки?"

# Вопросы, в которые слово-материал попало СЛУЧАЙНО, а не как дополнение глагола
# выбора (замечание Э1 финальной проверки). Первая редакция шаблона п. II-4
# ставила материал «где-то в окне 40 знаков» после глагола, и все три давали
# True — проверено вызовами `is_meaningless_question`. Второй из них — прямое
# нарушение того, что этот же модуль обещает в комментарии: «Почему мастер
# выбрал сюжет коронации?» под запрет попадать не должен, и «золотая карета»
# рядом ничего в вопросе не меняет.
MATERIAL_QUESTIONS_THAT_MUST_SURVIVE = [
    "Зачем в яйце использован механизм, который поднимает золотую птицу?",
    "Почему сюжет коронации выбран для яйца с золотой каретой?",
    "Почему это яйцо перестали использовать, хотя оно золотое?",
]

# Вопросы про уникальность НЕ про предмет (замечание Э5). Музей обвёл ровно один
# вопрос — «Какие уникальные особенности есть у ЭТОЙ РАБОТЫ Михаила Перхина?»;
# вопрос про музей, зал, эпоху или коллекцию под претензию не подпадает, а
# первая редакция шаблона срабатывала на любом объекте.
UNIQUENESS_QUESTIONS_THAT_MUST_SURVIVE = [
    "В чём уникальность коллекции Музея Фаберже?",
    "Чем особенно это событие для истории музея?",
    "Какие уникальные детали помогают отличить подделку Фаберже?",
    "Какие уникальные черты у русского ювелирного искусства этой эпохи?",
    "Чем уникален этот зал?",
]

# Предложения-«вода» со скриншотов п. II-5 (рассказ про яйцо-часы «Петушок»).
FILLER_SENTENCES = [
    "Перед вами — настоящее произведение искусства.",
    "Представьте себе, как восхитилась Мария Фёдоровна, получив такой изысканный "
    "и необычный подарок!",
    "Сейчас вы можете полюбоваться этим шедевром и представить себе, как он "
    "выглядел в руках императрицы.",
    "Это уникальное творение является свидетельством высокого уровня мастерства.",
    "Она служит напоминанием о том, что даже самые обычные предметы могут стать "
    "настоящими произведениями искусства, если их создать с любовью и мастерством.",
]

FACT_SENTENCES = [
    "Яйцо-часы «Петушок» создано в 1900 году в мастерской Михаила Перхина.",
    "Внутри спрятан заводной петушок, который поёт каждый час.",
    "Заказчиком выступил Николай II для матери, вдовствующей императрицы.",
]


# ── 1. Запрещённые вопросы (пп. II-4, II-8) ─────────────────────────────────
def test_screenshot_questions_are_dropped():
    """Все три вопроса со скриншота п. II-8 — дословно, это приёмка задачи."""
    for question in SCREENSHOT_QUESTIONS:
        assert guide_style.is_meaningless_question(question), question


def test_material_choice_questions_are_dropped():
    """«Почему мастер использовал именно эмаль-гильоше / алмазы / золото» (п. II-4)."""
    for question in MATERIAL_CHOICE_QUESTIONS:
        assert guide_style.is_meaningless_question(question), question


def test_duration_questions_are_dropped():
    for question in DURATION_QUESTIONS:
        assert guide_style.is_meaningless_question(question), question


def test_time_questions_about_anything_else_survive():
    """Ж3: шаблон сроков обязан ловить ТОЛЬКО «сколько длилось изготовление».

    Первая редакция держала «сколько лет/времени/дней» и глаголы
    (`созда|ушло|заня|потратил|работ над`) просто рядом, в пределах сорока знаков,
    и все шесть формулировок ниже давали True. Ложное срабатывание здесь опаснее
    пропуска: запрещённый вопрос посетитель может задать сам, а вырезанный —
    исчезает молча, и узнать о потере неоткуда.
    """
    for question in TIME_QUESTIONS_THAT_MUST_SURVIVE:
        assert not guide_style.is_meaningless_question(question), question


def test_known_limit_of_the_duration_pattern():
    """Ю2: честно закреплённое ограничение шаблона, а не желаемое поведение.

    «Сколько месяцев делали копию яйца для выставки?» отсекается: «яйца» — это
    настоящий объект изготовления, и шаблон не отличает его от «этого яйца».
    Если тест однажды упадёт «наоборот» — значит шаблон стал точнее, и строку
    надо перенести в TIME_QUESTIONS_THAT_MUST_SURVIVE.
    """
    assert guide_style.is_meaningless_question(TIME_QUESTION_STILL_CUT_BY_DESIGN)


def test_duration_questions_about_the_item_are_still_dropped():
    """Обратная сторона Я2: сужение объекта не должно распустить п. II-8.

    Указательное местоимение засчитывается в связке с вещью либо когда оно само
    и есть весь объект в конце вопроса; на претензии музея это ничего не меняет.
    """
    still_banned = [
        SCREENSHOT_QUESTIONS[0],                        # дословно со скриншота п. II-8
        "Сколько времени заняло создание этой ювелирной вещи?",
        "Сколько времени заняло создание этого?",
        "Как долго изготавливали этот экспонат?",
        "Сколько месяцев изготавливали это яйцо?",
    ]
    for question in still_banned:
        assert guide_style.is_meaningless_question(question), question


def test_material_is_bound_to_the_verb_of_choice():
    """Э1: материал обязан быть ДОПОЛНЕНИЕМ глагола выбора, а не просто рядом.

    Все три вопроса ниже вырезались молча. Фильтр стоит на чтении и ничего не
    логирует: вырезанный хороший вопрос не увидит никто, и восстановить его
    нельзя, — поэтому ложное срабатывание здесь дороже пропуска.
    """
    for question in MATERIAL_QUESTIONS_THAT_MUST_SURVIVE:
        assert not guide_style.is_meaningless_question(question), question


def test_material_choice_questions_are_still_dropped_after_narrowing():
    """Обратная сторона Э1: привязка к глаголу не должна распустить п. II-4.

    Пять формулировок — из самого замечания финальной проверки, дословно.
    """
    still_banned = [
        "Почему для украшения была выбрана именно жёлтая гильоше-эмаль?",
        "Почему мастер использовал именно эмаль-гильоше?",
        "Почему использовали именно алмазы?",
        "Зачем мастер применил золото, а не серебро?",
        "Почему выбрали жёлтую гильоше-эмаль?",
    ]
    for question in still_banned:
        assert guide_style.is_meaningless_question(question), question


def test_uniqueness_questions_about_anything_but_the_item_survive():
    """Э5: «уникальные особенности» без привязки к предмету — не претензия музея."""
    for question in UNIQUENESS_QUESTIONS_THAT_MUST_SURVIVE:
        assert not guide_style.is_meaningless_question(question), question


def test_uniqueness_questions_about_the_item_are_still_dropped():
    """Обратная сторона Э5: вопрос про ЭТУ вещь по-прежнему отсекается."""
    still_banned = [
        SCREENSHOT_QUESTIONS[1],                       # дословно со скриншота п. II-8
        "Какие уникальные особенности есть у этого предмета?",
        "Чем уникально это яйцо?",
        "В чём уникальность этого экспоната?",
        "У этой работы какие уникальные особенности?",
    ]
    for question in still_banned:
        assert guide_style.is_meaningless_question(question), question


def test_screenshot_questions_still_caught_after_narrowing():
    """Сужение шаблона не должно распустить приёмку: три вопроса п. II-8 на месте.

    Смешанный пул проходит через тот же вход, что и выдача посетителю
    (`clean_questions`), но с выключенной дедупликацией: две формулировки из
    списка «должны выживать» — «Сколько лет прошло с тех пор, как создали яйцо?»
    и «Сколько лет назад создали это яйцо?» — действительно об одном и том же, и
    их склейка здесь была бы правильной работой ДРУГОГО фильтра. Здесь
    проверяется только запрет.
    """
    must_survive = (TIME_QUESTIONS_THAT_MUST_SURVIVE
                    + MATERIAL_QUESTIONS_THAT_MUST_SURVIVE
                    + UNIQUENESS_QUESTIONS_THAT_MUST_SURVIVE)
    survivors = guide_style.clean_questions(SCREENSHOT_QUESTIONS + must_survive, dedupe=False)
    assert survivors == must_survive


def test_meaningful_questions_survive():
    """Предохранитель: перечень запретов узкий, ложное срабатывание опаснее пропуска.

    «Из каких материалов он сделан?» — прямая противоположность претензии музея:
    он возражает против «почему выбрали именно X», а не против перечня материалов.
    «Сколько лет мастер работал у Фаберже?» — проверка на то, что шаблон сроков
    не цепляет биографию (поэтому в нём «работал НАД», а не голое «работ»).
    """
    keep = [
        "Из каких материалов он сделан?",
        "Кому подарили это яйцо?",
        "Что за сюрприз спрятан внутри?",
        "Что известно о его владельцах?",
        "Почему яйцо назвали «Ландыши»?",
        "Почему мастер выбрал сюжет коронации?",
        "Где хранилось яйцо до революции?",
        "Сколько лет мастер работал у Фаберже?",
        "Сколько яиц создал Фаберже для царской семьи?",
        "Сколько времени яйцо хранилось в Оружейной палате?",
        "Какие техники использованы в этом изделии?",
        "Какие особенности конструкции у этого яйца?",
        "Что символизирует изображение на эмали?",
        "Кто такой Михаил Перхин?",
        "Какая история скрыта за этим предметом?",
        "Что ещё посмотреть в этом зале?",
        "Что ещё создал мастер Михаил Перхин?",
    ]
    for question in keep:
        assert not guide_style.is_meaningless_question(question), question


def test_drop_keeps_order_and_does_not_top_up():
    pool = ["Кому подарили это яйцо?", SCREENSHOT_QUESTIONS[0],
            "Из каких материалов он сделан?", SCREENSHOT_QUESTIONS[2]]
    assert guide_style.drop_meaningless_questions(pool) == [
        "Кому подарили это яйцо?", "Из каких материалов он сделан?",
    ]


def test_stub_pool_passes_the_filter_whole():
    """Стаб не должен сам генерировать запрещённые вопросы (regress-сторож)."""
    exhibit = dict(EXHIBIT, label_slug="faberge_egg_hen", master_name="Михаил Перхин")
    pool = llm._questions_stub(exhibit, 8)
    assert pool, "стаб обязан что-то отдать"
    assert guide_style.drop_meaningless_questions(pool) == pool


def test_filter_runs_before_the_slice():
    """Иначе запрещённые формулировки съедают слоты: было бы [A] вместо [A, B, C]."""
    pool = [SCREENSHOT_QUESTIONS[0], SCREENSHOT_QUESTIONS[2],
            "Кому подарили это яйцо?", "Что за сюрприз спрятан внутри?",
            "Из каких материалов он сделан?"]
    assert guide_style.clean_questions(pool, 3) == [
        "Кому подарили это яйцо?", "Что за сюрприз спрятан внутри?",
        "Из каких материалов он сделан?",
    ]


# ── 2. Перефразировки (п. II-2) ─────────────────────────────────────────────
def test_paraphrases_collapse():
    """Пара со скриншота п. II-2: точным сравнением строк её не поймать."""
    pool = [
        "Какие именно скифские мотивы использованы в браслете?",
        "Какие именно скифские мотивы Эрик Коллин использовал в дизайне браслета?",
        "Кому принадлежал браслет?",
    ]
    assert guide_style.dedupe_questions(pool) == [pool[0], pool[2]]


def test_dedupe_respects_exclude():
    """`exclude` — то, что уже спрашивали или на что уже отказали (пп. II-2/II-3)."""
    asked = ["Как функционирует механизм со стрелкой-змейкой?"]
    pool = ["Как функционирует механизм вращающейся стрелки-змейки?",
            "Кому подарили это яйцо?"]
    assert guide_style.dedupe_questions(pool, exclude=asked) == ["Кому подарили это яйцо?"]
    # Тот же результат через общую воронку и при выключенной дедупликации внутри
    # пула: «не предлагать то, на что уже отказали» — не косметика.
    assert guide_style.clean_questions(pool, exclude=asked, dedupe=False) == [
        "Кому подарили это яйцо?"
    ]


def test_numbers_keep_questions_apart():
    """Число в вопросе — это и есть весь смысл различия, склеивать нельзя.

    `question_cluster.keywords` отбрасывает токены короче двух символов, поэтому
    ключ здесь дополняется числами (см. `guide_style._question_key`).
    """
    pool = ["Что стоит в витрине 5?", "Что стоит в витрине 12?"]
    assert guide_style.dedupe_questions(pool) == pool


def test_dedupe_does_not_merge_different_topics():
    """Недосклейка безопасна, пересклейка — потерянная тема. Порог держим строгим."""
    pool = [
        "Кому подарили это яйцо?",
        "Что за сюрприз спрятан внутри?",
        "Из каких материалов он сделан?",
        "Где хранилось яйцо до революции?",
        "Кто такой Михаил Перхин?",
    ]
    assert guide_style.dedupe_questions(pool) == pool


def test_short_keys_are_merged_only_on_exact_match():
    """Ж4: вложенный короткий ключ давал перекрытие 1.0 и съедал соседнюю тему.

    Мера `question_cluster._similar` нормирует перекрытие на МЕНЬШИЙ ключ. У
    подсказок ключи короткие (две-три леммы), поэтому вложенность там означает не
    «тот же вопрос другими словами», а «более узкий вопрос». Проверено реальным
    вызовом: «Кому подарили это яйцо?» ({владелец, яйц}) целиком лежит внутри
    «Кому принадлежало это яйцо после революции?» ({владелец, революц, яйц}) —
    и второй вопрос из блока исчезал.
    """
    pair = [
        "Кому подарили это яйцо?",
        "Кому принадлежало это яйцо после революции?",
    ]
    assert guide_style.dedupe_questions(pair) == pair
    # Дословный повтор по-прежнему схлопывается — ужесточение не отменяет дедупликацию.
    assert guide_style.dedupe_questions([pair[0], "  кому подарили это яйцо  "]) == [pair[0]]


# Пары «общий вопрос ⊂ более узкий вопрос»: ключ первого целиком лежит внутри
# ключа второго. Планка `MIN_KEY_LEN_FOR_FUZZY` их не закрывала — она про
# `min(len) <= 2`, а здесь ключи длиной 3+, и сравнение уходило в `_similar`,
# который нормирует перекрытие на МЕНЬШИЙ ключ и потому давал ровно 1.0.
NESTED_QUESTION_PAIRS = (
    ("Из чего сделано яйцо?", "Из чего сделана подставка яйца?"),
    ("Где хранилось яйцо?", "Где хранилось яйцо до революции?"),
    ("Что означает монограмма?", "Что означает корона на монограмме?"),
)


def test_narrower_question_is_not_swallowed_by_the_broader_one():
    """Э2: строгое вложение — это СУЖЕНИЕ темы, а не перефразировка.

    Проверено реальными вызовами `dedupe_questions`: второй вопрос каждой пары
    исчезал молча. Та же мера кормит `exclude` из глобальной памяти отказов
    (решение Д8), то есть вложенный ключ прятал более узкую тему у ВСЕХ
    посетителей экспоната на GUIDE_REFUSAL_MEMORY_DAYS дней.
    """
    for broad, narrow in NESTED_QUESTION_PAIRS:
        assert guide_style.dedupe_questions([broad, narrow]) == [broad, narrow], narrow
        # И в обе стороны: «уже спросили общее» не должно прятать уточняющее.
        assert guide_style.dedupe_questions([narrow], exclude=[broad]) == [narrow], narrow
        assert guide_style.dedupe_questions([broad], exclude=[narrow]) == [broad], broad


def test_asked_broad_questions_do_not_empty_the_pool():
    """Сквозной прогон через воронку выдачи: было ОДИН вопрос вместо четырёх."""
    pool = [narrow for _broad, narrow in NESTED_QUESTION_PAIRS] + ["Кто такой Михаил Перхин?"]
    asked = [broad for broad, _narrow in NESTED_QUESTION_PAIRS]
    assert guide_questions.select_questions(pool, 4, asked=asked) == pool


# Я3: пары, которые синоним-карта кластеризатора сводит к одной лемме
# «владелец». Для частотных отчётов аналитики это правильно, для подсказок — нет:
# `exclude` кормится ГЛОБАЛЬНОЙ памятью отказов (решение Д8), и один отказ на
# «Кто подарил это яйцо?» прятал «Кто заказал это яйцо?» у ВСЕХ посетителей
# экспоната на GUIDE_REFUSAL_MEMORY_DAYS дней. Заказчик и даритель — разные люди
# и разные ответы.
DIFFERENT_ACTIONS_SAME_LEMMA = (
    ("Кто заказал это яйцо?", "Кто подарил это яйцо?"),
    ("Кто заказал этот портсигар?", "Кому подарили этот портсигар?"),
)


def test_order_and_gift_are_not_the_same_question():
    """Я3: расхождение действий отменяет похожесть, как и расхождение чисел."""
    for order, gift in DIFFERENT_ACTIONS_SAME_LEMMA:
        assert guide_style.dedupe_questions([order, gift]) == [order, gift], gift
        # И главное — через `exclude`: отказ на один вопрос не прячет другой.
        assert guide_style.dedupe_questions([order], exclude=[gift]) == [order], order
        assert guide_style.dedupe_questions([gift], exclude=[order]) == [gift], gift


def test_the_same_action_still_collapses():
    """Обратная сторона Я3: признак действия ничего не расклеивает внутри аспекта.

    Обе формулировки пары со скриншота п. II-3 несут «заказ», поэтому стоп-сигнал
    не срабатывает и перефразировка схлопывается ровно как раньше. Дословный
    повтор — тоже.
    """
    screenshot = (
        "Почему яйцо стало единственным, заказанным герцогиней Мальборо?",
        "Почему яйцо стало единственным заказом герцогини Мальборо у Фаберже?",
    )
    assert guide_style.dedupe_questions(list(screenshot)) == [screenshot[0]]
    assert guide_style.dedupe_questions([screenshot[1]], exclude=[screenshot[0]]) == []
    assert guide_style.dedupe_questions(
        ["Кто заказал это яйцо?"], exclude=["  кто заказал это яйцо  "]
    ) == []


def test_one_sided_action_is_the_documented_limit_of_the_stop_signal():
    """Х6: ОГРАНИЧЕНИЕ признака действия — по факту, а не по прежнему описанию.

    Стоп-сигнал сравнивает МНОЖЕСТВА действий, поэтому он работает только когда
    действие названо в обоих вопросах. Прежний комментарий у `_ACTION_ASPECTS`
    иллюстрировал это парой «Кто подарил это яйцо?» / «Кому принадлежало это
    яйцо?» и утверждал, что она считается одной темой. Утверждение неверно: пара
    разводится — но НЕ признаком действия, а планкой `MIN_KEY_LEN_FOR_FUZZY`
    (ключ второго вопроса состоит из двух лемм, а такие склеиваются только при
    полном совпадении). Здесь закреплены оба факта сразу, чтобы комментарий и
    поведение больше не разъезжались.
    """
    # 1. Пара из прежнего комментария РАЗВОДИТСЯ — и без признака действия тоже.
    short = ["Кто подарил это яйцо?", "Кому принадлежало это яйцо?"]
    assert guide_style.dedupe_questions(short) == short
    assert not guide_style._same_topic(
        frozenset({"владелец", "кто", "яйц"}), frozenset({"владелец", "яйц"})
    ), "разводит планка длины ключа, а не признак действия"

    # 2. А настоящая цена ограничения видна там, где обоим ключам хватает длины:
    #    одностороннее действие стоп-сигналом не является, и вопрос схлопывается.
    long_pair = [
        "Кому подарили это яйцо после революции?",
        "Кому принадлежало это яйцо после революции?",
    ]
    assert guide_style.dedupe_questions(long_pair) == [long_pair[0]]


def test_dedupe_cannot_tell_apart_what_the_common_part_belongs_to():
    """Честно закреплённое ОГРАНИЧЕНИЕ меры, а не желаемое поведение.

    Мешок лемм не различает, к чему относится общая часть: у пары ниже ни один
    ключ не вложен в другой, перекрытие 0.8 — и вопросы схлопнутся, хотя они
    разные. Порогом это не чинится: любой порог, который разведёт эту пару,
    расклеит и перефразировки со скриншотов, ради которых дедупликация заведена.
    Тест стоит здесь, чтобы ограничение было видно, а не всплыло у музея.
    """
    pair = ["Что случилось с яйцом в 1917 году?", "Что случилось с фирмой Фаберже в 1917 году?"]
    assert guide_style.dedupe_questions(pair) == [pair[0]]


# Ю3: пары, которые расклеило отсечение вложенных ключей. Лишняя лемма пришла
# не от сужения темы, а от синоним-карты кластеризатора («находится» → «место»)
# и от вопросительного слова («кто»), — то есть от ФОРМЫ вопроса, а не от его
# предмета. Обе пары посетитель увидел бы рядом: ровно жалоба п. II-2.
REPHRASINGS_UNGLUED_BY_THE_NESTING_RULE = (
    ("Что внутри яйца?", "Что находится внутри яйца?"),
    ("Кто владел яйцом до революции?", "Кому принадлежало яйцо до революции?"),
)


def test_rephrasings_with_a_weak_extra_lemma_still_collapse():
    """Ю3: вложение считается по тематическим леммам, без слабых (WEAK_LEMMAS)."""
    for original, paraphrase in REPHRASINGS_UNGLUED_BY_THE_NESTING_RULE:
        assert guide_style.dedupe_questions([original, paraphrase]) == [original], paraphrase
        assert guide_style.dedupe_questions([paraphrase], exclude=[original]) == [], paraphrase


def test_weak_lemmas_do_not_reopen_the_nesting_hole():
    """Обратная сторона Ю3: настоящее сужение темы по-прежнему не склеивается.

    Слабые леммы выброшены только из проверки вложения. Пары Э2 отличаются
    ТЕМАТИЧЕСКОЙ леммой (подставка, революция, корона), и её выбросить нечем.
    """
    for broad, narrow in NESTED_QUESTION_PAIRS:
        assert guide_style.dedupe_questions([broad, narrow]) == [broad, narrow], narrow
    pair = ["Кому подарили это яйцо?", "Кому принадлежало это яйцо после революции?"]
    assert guide_style.dedupe_questions(pair) == pair
    assert guide_style.dedupe_questions(["Что стоит в витрине 5?", "Что стоит в витрине 12?"]) == [
        "Что стоит в витрине 5?", "Что стоит в витрине 12?",
    ]


def test_paraphrases_from_the_screenshots_still_collapse():
    """Ужесточение не должно расклеить то, ради чего дедупликация заведена.

    Пары — дословно со скриншотов пп. II-2 и II-3; их ключи длиннее порога
    `MIN_KEY_LEN_FOR_FUZZY`, поэтому сравнение осталось прежним.
    """
    pairs = (
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
    for original, paraphrase in pairs:
        assert guide_style.dedupe_questions([original, paraphrase]) == [original], paraphrase
        assert guide_style.dedupe_questions([paraphrase], exclude=[original]) == [], paraphrase


# ── 3. «Вода» в рассказе (п. II-5) ──────────────────────────────────────────
def test_every_screenshot_sentence_is_recognised_as_filler():
    for sentence in FILLER_SENTENCES:
        assert guide_style.is_filler_sentence(sentence), sentence


def test_fact_sentences_are_not_filler():
    for sentence in FACT_SENTENCES:
        assert not guide_style.is_filler_sentence(sentence), sentence


def test_strip_filler_keeps_the_facts():
    story = " ".join([
        FILLER_SENTENCES[0], FACT_SENTENCES[0], FILLER_SENTENCES[1],
        FACT_SENTENCES[1], FILLER_SENTENCES[2], FACT_SENTENCES[2],
        FILLER_SENTENCES[3], FILLER_SENTENCES[4],
    ])
    cleaned, dropped = guide_style.strip_filler(story)
    assert dropped == 5
    for sentence in FACT_SENTENCES:
        assert sentence in cleaned
    for sentence in FILLER_SENTENCES:
        assert sentence not in cleaned


def test_strip_filler_keeps_the_paragraphs():
    """Ж5: чистка не должна превращать рассказ в простыню.

    Прежняя редакция собирала уцелевшие предложения через `' '.join` по всему
    тексту, и рассказ из четырёх абзацев приезжал на фронт без единого переноса —
    но только если в нём нашлась вода. Форматирование карточки зависело от
    содержимого, а музей просил ровно обратного: «прогнозируемая» карточка
    (п. I-2). Абзац, из которого вода вынесла всё, уходит вместе со своим
    разделителем — пустая строка на его месте не лучше простыни.
    """
    story = "\n\n".join([
        FACT_SENTENCES[0],
        f"{FILLER_SENTENCES[0]} {FACT_SENTENCES[1]}",
        FILLER_SENTENCES[1],                      # абзац целиком из воды
        f"{FACT_SENTENCES[2]} {FILLER_SENTENCES[3]}",
    ])
    cleaned, dropped = guide_style.strip_filler(story)

    assert dropped == 3
    paragraphs = cleaned.split("\n\n")
    assert paragraphs == [
        FACT_SENTENCES[0],
        FACT_SENTENCES[1],
        FACT_SENTENCES[2],
    ]
    assert "\n\n\n" not in cleaned, "выброшенный абзац оставил после себя пустую строку"


def test_strip_filler_returns_single_paragraph_stories_unchanged_in_shape():
    """Рассказ без переносов и остаётся без переносов — пересборка их не выдумывает."""
    story = " ".join([FILLER_SENTENCES[0], FACT_SENTENCES[0], FACT_SENTENCES[1], FACT_SENTENCES[2]])
    cleaned, dropped = guide_style.strip_filler(story)
    assert dropped == 1
    assert "\n" not in cleaned


def test_museum_data_survives_untouched():
    """Разбиение по предложениям не должно рвать инициалы (баг-репорт, п. I-2)."""
    text = ("Яйцо создано в 1900 году. Фирма К. Фаберже, мастер М. Перхин. "
            "Хранится в витрине 2.")
    assert guide_style.split_sentences(text) == [
        "Яйцо создано в 1900 году.",
        "Фирма К. Фаберже, мастер М. Перхин.",
        "Хранится в витрине 2.",
    ]
    assert guide_style.strip_filler(text) == (text, 0)


def test_sentence_with_a_number_is_kept_even_if_it_matches():
    """Осознанный компромисс: факт дороже эпитета при нём.

    «Перед вами — настоящее произведение искусства, созданное в 1900 году» —
    остаётся. Музей может увидеть на проде именно такой случай, и это надо было
    записать заранее, а не выяснять потом.
    """
    watered_fact = "Перед вами — настоящее произведение искусства, созданное в 1900 году."
    assert not guide_style.is_filler_sentence(watered_fact)
    story = " ".join([watered_fact] + FACT_SENTENCES)
    assert guide_style.strip_filler(story) == (story, 0)


def test_pure_water_is_returned_as_is():
    """Пустой рассказ хуже водянистого: посетитель у витрины должен что-то услышать."""
    story = " ".join(FILLER_SENTENCES)
    assert guide_style.strip_filler(story) == (story, 0)
    assert guide_style.strip_filler("") == ("", 0)


# ── 4. Рассказ приходит очищенным сквозь llm.generate_story ─────────────────
WATERY_STORY = " ".join([
    FILLER_SENTENCES[0], FACT_SENTENCES[0], FILLER_SENTENCES[1],
    FACT_SENTENCES[1], FILLER_SENTENCES[2], FACT_SENTENCES[2],
    FILLER_SENTENCES[3], FILLER_SENTENCES[4],
])


def test_story_is_filtered_before_it_reaches_the_router():
    """Чистка до возврата попадает и в озвучку: роутер шлёт этот же текст в TTS."""
    with patched(llm, gpt_response(WATERY_STORY), **LLM_ENV, guide_story_filler_filter=True):
        text, _ = asyncio.run(llm.generate_story(EXHIBIT, "engaging", "ru"))
    assert FILLER_SENTENCES[0] not in text
    assert FACT_SENTENCES[0] in text


def test_story_filter_can_be_switched_off():
    with patched(llm, gpt_response(WATERY_STORY), **LLM_ENV, guide_story_filler_filter=False):
        text, _ = asyncio.run(llm.generate_story(EXHIBIT, "engaging", "ru"))
    assert text == WATERY_STORY


# ── 5. Промпты ──────────────────────────────────────────────────────────────
def test_story_prompt_names_the_banned_turns_of_phrase():
    exhibit = {"name": "Яйцо", "raw_history": "История предмета", "year_created": "1900"}
    with patched(llm, gpt_response("рассказ"), **LLM_ENV, guide_story_max_chars=900) as calls:
        asyncio.run(llm._yandexgpt_story(exhibit, "engaging", "ru"))
    prompt = calls[0]["json"]["messages"][1]["text"]
    assert "не больше 900 знаков" in prompt
    assert "4–6 предложений" in prompt
    assert "представьте себе" in prompt and "шедевр" in prompt
    assert "полюбоваться" in prompt
    # Запрет на выдумывание (был и раньше) остаётся последним в промпте.
    assert prompt.rstrip().endswith("Чего не знаешь — не пиши.")


def test_short_style_no_longer_asks_for_two_budgets_at_once():
    """style="short" получал сразу «в двух-трёх предложениях» и «примерно 5–7»."""
    exhibit = {"name": "Яйцо", "raw_history": "История предмета"}
    with patched(llm, gpt_response("рассказ"), **LLM_ENV) as calls:
        asyncio.run(llm._yandexgpt_story(exhibit, "short", "ru"))
    prompt = calls[0]["json"]["messages"][1]["text"]
    assert "2–3 предложений" in prompt and "5–7" not in prompt


def test_engaging_style_no_longer_asks_for_epithets():
    """`StoryRequest.style` по умолчанию engaging — эта строка уходит в каждый рассказ."""
    assert llm._STYLE_HINT["engaging"] == "живо, но по фактам"


def test_questions_prompt_carries_the_museum_restrictions():
    with patched(llm, gpt_response("Вопрос 1?\nВопрос 2?"), **LLM_ENV) as calls:
        asyncio.run(llm._yandexgpt_questions(EXHIBIT, 4, "ru"))
    prompt = calls[0]["json"]["messages"][1]["text"]
    assert "почему мастер выбрал именно тот или иной материал" in prompt
    assert "сколько времени заняло" in prompt


def test_chat_prompt_forbids_making_things_up():
    """п. II-6: скриншот музея («известен своими инновациями») — из ДИАЛОГА."""
    assert "не приписывай" in llm._CHAT_SYSTEM
    assert "известен своими инновациями" in llm._CHAT_SYSTEM
    assert "оценочных эпитетов" in llm._CHAT_SYSTEM


def test_chat_prompt_never_reintroduces_the_28_07_refusals():
    """Буквальный сторож бага 28.07.2026, п.3.

    Прежняя формулировка подавала справку как ЕДИНСТВЕННЫЙ источник, и на общий
    вопрос («Пётр I» после выхода из Рыцарского зала) гид отвечал «в
    предоставленных материалах нет информации». Запрет на выдумывание обязан быть
    сформулирован ПО ДЕЙСТВИЮ, а не по источнику: следующая правка промпта не
    должна суметь тихо вернуть отказы.
    """
    low = llm._CHAT_SYSTEM.lower()
    assert "НЕ отвечай «в предоставленных материалах" in llm._CHAT_SYSTEM
    assert "отвечай по своим знаниям" in llm._CHAT_SYSTEM
    for forbidden in ("только по справке", "только в справке", "только на предоставленн",
                      "только на основе справки", "исключительно по справке"):
        assert forbidden not in low, forbidden
    # Разрешение отвечать по своим знаниям модель должна читать ПОСЛЕ запрета.
    assert low.index("не приписывай") < low.index("отвечай по своим знаниям")


def test_chat_actually_sends_the_updated_system_prompt():
    with patched(llm, gpt_response("ответ"), **LLM_ENV) as calls:
        asyncio.run(llm._yandexgpt_chat("справка", [], "кто такой Перхин?", "ru"))
    assert calls[0]["json"]["messages"][0]["text"] == llm._CHAT_SYSTEM


# ── 6. Прогретый кэш чистится на чтении ─────────────────────────────────────
# Главный риск задачи: `source_hash` считается по тексту карточки, поэтому правка
# промпта НЕ делает ни одну из 1200+ записей несвежей. Без фильтра на выдаче
# музей увидел бы после релиза ровно те же подсказки.
CACHED_POOL = [
    SCREENSHOT_QUESTIONS[0],
    "Кому подарили это яйцо?",
    SCREENSHOT_QUESTIONS[2],
    "Что за сюрприз спрятан внутри?",
    "Из каких материалов он сделан?",
]


def _fresh(pool=None, exhibit=EXHIBIT, language="ru"):
    return FakeRow(pool if pool is not None else CACHED_POOL,
                   guide_questions.fingerprint(exhibit, language))


def test_cache_hit_is_cleaned_without_calling_the_model():
    with wired(row=_fresh()) as calls:
        questions = asyncio.run(guide_questions.for_exhibit(None, EXHIBIT, 4))
    assert calls["llm"] == [], "чистка на чтении не должна стоить ни одного вызова LLM"
    assert questions == ["Кому подарили это яйцо?", "Что за сюрприз спрятан внутри?",
                         "Из каких материалов он сделан?"]


def test_stale_pool_on_llm_failure_is_cleaned_too():
    stale = FakeRow(CACHED_POOL, "устаревший-хэш")
    with wired(row=stale, error=True):
        questions = asyncio.run(guide_questions.for_exhibit(None, EXHIBIT, 4))
    assert SCREENSHOT_QUESTIONS[0] not in questions
    assert "Кому подарили это яйцо?" in questions


def test_warm_report_shows_what_the_visitor_will_see():
    with wired(row=None) as calls:
        outcome, questions = asyncio.run(
            guide_questions.warm_exhibit(None, FakeExhibit(EXHIBIT), _fresh())
        )
    assert outcome == "cached" and calls["llm"] == []
    assert SCREENSHOT_QUESTIONS[0] not in questions


def test_generated_pool_is_stored_raw_but_served_clean():
    """Отмена запрета не должна требовать перегенерации каталога (~1252 вызова)."""
    with wired(row=None, generated=CACHED_POOL, guide_questions_cache_size=8) as calls:
        questions = asyncio.run(guide_questions.for_exhibit(None, EXHIBIT, 4))
    assert calls["saved"][0]["questions"] == CACHED_POOL, "в БД пул кладём сырым"
    assert SCREENSHOT_QUESTIONS[0] not in questions


def test_questions_filter_can_be_switched_off_without_regeneration():
    with wired(row=_fresh(), guide_questions_filter=False) as calls:
        questions = asyncio.run(guide_questions.for_exhibit(None, EXHIBIT, 4))
    assert calls["llm"] == []
    assert questions == CACHED_POOL[:4], "откат обязан вернуть пул как есть, без похода в LLM"


def test_cache_key_did_not_change():
    """Промпты правились, `questions_source` — нет: кэш подсказок НЕ инвалидируется.

    Ключ свежести считается по тексту карточки, а не по версии промпта. Это и
    хорошо (релиз не запускает перегенерацию 1252 карточек на первых
    посетителях), и опасно (правка промпта сама по себе до прода не долетает) —
    поэтому фильтр и стоит на чтении.
    """
    # Хэш зафиксирован константой намеренно: если кто-то соберётся тронуть
    # `questions_source`, тест упадёт и заставит принять решение осознанно —
    # перегенерация каталога стоит ~1252 платных вызова LLM.
    assert guide_questions.fingerprint(EXHIBIT) == (
        "8ed649e80d6c3b7930d4ef3c739b1815188dd69e247932b5b76a65bcd08d4970"
    )
    assert llm.questions_source(EXHIBIT) == "Подарен Николаем II"


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
