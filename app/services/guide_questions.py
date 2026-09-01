"""Кэш вопросов-подсказок на экспонаты (просьба заказчика 26.08.2026).

Зачем
    Вопросы под рассказом («Кому подарили это яйцо?») считает отдельный вызов
    YandexGPT — в логах расхода это `operation=questions`. Он шёл ВТОРЫМ рядом с
    каждым рассказом (`POST /guide/story`) и рядом с каждой репликой диалога об
    экспонате (`POST /guide/chat`), то есть на один ход диалога приходилось два
    вызова LLM. При этом результат зависит ТОЛЬКО от карточки экспоната: ни от
    посетителя, ни от истории диалога, ни от заданного вопроса. Один экспонат за
    день открывают десятки раз — и каждый раз мы платили за один и тот же текст.

Как устроено
    Таблица `exhibit_questions`: (экспонат, язык) → пул вопросов. Ключ свежести
    — `source_hash`: sha256 языка и текста, который уходит в промпт
    (`llm.questions_source` — тот же самый, что и в генерации). Музей поправил
    описание — хэш разошёлся — запись перегенерируется при первом обращении.
    Ручной инвалидации нет и не нужно.

Чего кэш НЕ ловит
    Правки КОДА: промпта генерации и списка запрещённых музеем формулировок.
    `source_hash` считается по тексту карточки, а не по версии промпта, поэтому
    после релиза 31.08.2026 (пп. II-4/II-8) прогретые карточки отдавали бы
    прежний пул — включая вопросы, которые музей просил убрать. Отсюда `_serve`:
    запреты применяются НА ЧТЕНИИ, каждый раз, ко всем пяти путям выдачи. Пул в
    БД остаётся сырым; `--force`-прогрев нужен только чтобы дозаполнить пул
    новыми вопросами взамен отфильтрованных, и это ~1252 платных вызова LLM.

Пул — это то, ИЗ ЧЕГО выбираем (с 31.08.2026)
    Раньше наружу уходил ПРЕФИКС пула: `questions[:max_questions]`. Для диалога
    (`max_questions=3`) это означало всегда одни и те же первые три вопроса —
    посетитель задавал первый, получал ответ и снова видел его же первым в
    блоке подсказок (баг-репорт 31.08.2026, п. II-2: «предлагает неограниченное
    количество вопросов, которые постоянно перефразирует»). Теперь выбор
    зависит от сессии: что уже спросили и на что гид уже отказался отвечать
    (`select_questions`). На ключ свежести это НЕ влияет и в таблицу не
    попадает — в записи по-прежнему лежит сырой пул от модели, один на всех.

Чего здесь нет намеренно
    • TTL. «Протухание по времени» вернуло бы часть расхода за то, что и так не
      меняется: описания правят редко, а изменение ловится хэшем.
    • Кэша по (экспонат, max_questions). В записи лежит ПУЛ
      (`GUIDE_QUESTIONS_CACHE_SIZE`), наружу отдаётся срез: иначе диалог с его
      `max_questions=3` выбивал бы запись, сделанную рассказом для 4.
    • Кэша вопросов вне экспоната. Подсказки для зала, для списка залов и для
      общего чата (`hall_questions`, `halls_overview_questions`,
      `museum_questions`) — детерминированные константы: они не стоят ни
      токена, одинаковы для всех и кэшировать в них нечего.
"""
from __future__ import annotations

import hashlib
import logging
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from sqlalchemy.ext.asyncio import AsyncSession

from .. import crud
from .. import models as m
from ..config import settings
from . import UpstreamError, guide_style, llm
from . import location as location_text

logger = logging.getLogger(__name__)


def fingerprint(exhibit: Dict, language: str = "ru") -> str:
    """Хэш текста, из которого генерируются вопросы (+ язык) — ключ свежести записи."""
    source = llm.questions_source(exhibit)
    return hashlib.sha256(f"{language}\n{source}".encode("utf-8")).hexdigest()


def _pool_size(max_questions: int) -> int:
    """Сколько вопросов просить у модели: пул из настроек, но не меньше запрошенного."""
    return max(max_questions, settings.guide_questions_cache_size)


def _serve(questions: List[str], max_questions: int, exclude: Tuple[str, ...] = ()) -> List[str]:
    """Что реально уходит посетителю: снять запрещённые формулировки, потом срез.

    Фильтр стоит на ЧТЕНИИ, а не только на генерации, и это главная точка задачи
    31.08.2026 по пп. II-4/II-8. `source_hash` считается по тексту карточки
    (`llm.questions_source`), а не по коду, — значит правка промпта генерации НЕ
    делает ни одну запись несвежей. Прогретые карточки (по доке — 1200+)
    продолжили бы отдавать старый пул с «Сколько времени заняло создание этого
    предмета?» вообще без похода в модель, и музей увидел бы после релиза ровно
    те же подсказки.

    Пул в БД при этом остаётся СЫРЫМ: мы не переписываем сохранённые вопросы и не
    вырезаем их перед записью. Список запретов — продуктовое решение; если музей
    передумает, вопросы возвращаются снятием шаблона или
    `GUIDE_QUESTIONS_FILTER=false`, без перегенерации каталога (~1252 платных
    вызова LLM). Обратная сторона — часть уже оплаченного пула не показывается;
    поэтому `GUIDE_QUESTIONS_CACHE_SIZE` поднят, чтобы запаса хватало после
    обоих фильтров.

    `exclude` — формулировки, которых в подсказках быть не должно (уже заданные
    в этой сессии, уже получившие отказ по этому экспонату); наполняет его
    вызывающий код.
    """
    return guide_style.clean_questions(
        questions,
        max_questions,
        drop_meaningless=settings.guide_questions_filter,
        dedupe=settings.guide_questions_dedupe,
        exclude=exclude,
    )


# ═════════════════════════════════════════════════════════════════════════════
# Наборы без LLM: блок подсказок не должен пустеть (п. II-7)
# ═════════════════════════════════════════════════════════════════════════════
# Дословно из баг-репорта: «После выбора одного вопроса и получения ответа на
# него, варианты вопросов уже не предлагаются, надо возвращаться назад». Блок
# действительно пустел по-настоящему: `questions` заполнялся ровно в одной ветке
# роутера — обычный диалог ПРИ найденном экспонате. Поиск по номеру, список
# залов, контекст только зала и общий чат отдавали `[]` гарантированно.
#
# Наборы ниже — константы, а не вызов модели, и это принципиально. Во-первых, ни
# одна из этих веток не привязана к карточке, то есть кэшировать нечего, а
# платить за подсказки к «какие есть залы» незачем. Во-вторых, они же работают
# последней страховкой в `select_questions`: набор, который не может отказать и
# не может опустеть, обязан быть детерминированным.

# Общий чат без контекста. «Какие залы есть в музее?» стоит первым намеренно:
# на этот вопрос гид отвечает СТРУКТУРОЙ каталога (`guide_intel.is_hall_listing`
# → `_describe_halls`), без модели и без риска выдумки, — то есть подсказка
# гарантированно приводит к содержательному ответу.
MUSEUM_QUESTIONS: Tuple[str, ...] = (
    "Какие залы есть в музее?",
    "Что обязательно посмотреть в коллекции?",
    "Чем знаменит Карл Фаберже?",
    "Как появился Музей Фаберже?",
)

# Контекст зала без экспоната (посетитель нажал «Спросить AI-гида» на уровне
# зала). Названия зала в тексте нет намеренно: контекст уже стоит в сессии, и
# вопрос «этот зал» модель понимает по справке, а вот подставленное название
# зала пришлось бы согласовывать по падежам.
HALL_QUESTIONS: Tuple[str, ...] = (
    "Какие предметы стоят в этом зале?",
    "Чем примечателен этот зал?",
    "Что здесь стоит посмотреть в первую очередь?",
)

# Подсказки, построенные от ЗАПОЛНЕННЫХ полей карточки. Пустое поле вопроса не
# порождает: спрашивать «в какой технике он выполнен» у карточки, где техник
# нет, — это ровно жалоба п. II-3 («предлагает вопросы, ответы на которые не
# знает»), только устроенная нами самими.
#
# Формулировки намеренно НЕ подставляют значение поля («Кто такой Фирма
# К. Фаберже, мастер М. Перхин?» — так выглядела бы подстановка `master_name`,
# в котором фирма и мастер лежат одной строкой, см. п. I-2).
_FALLBACK_BY_FIELD: Tuple[Tuple[str, str], ...] = (
    ("raw_history", "Какая история связана с этим предметом?"),
    ("material", "Из чего сделан этот предмет?"),
    ("techniques", "В какой технике он выполнен?"),
    ("master_name", "Кто его создал?"),
    ("year_created", "Когда он появился?"),
)
# Последняя строка запаса: она не зависит ни от одного поля и отвечается тем же
# рассказом, который посетитель уже видит на экране.
_FALLBACK_UNIVERSAL = "Расскажи об этом предмете подробнее."


def fallback_questions(exhibit: Optional[Dict]) -> List[str]:
    """Запасные подсказки по карточке — без похода в модель и без кэша.

    Нужны там, где пул кончился: после исключения уже заданных вопросов и
    вопросов с отказом (пп. II-2, II-3) на бедной карточке не остаётся ничего, а
    пустой блок — это исходная жалоба п. II-7. Набор стоит ноль токенов и не
    может отказать, поэтому годится и как аварийный ответ при недоступной LLM.
    """
    if not exhibit:
        return []
    out = [text for field, text in _FALLBACK_BY_FIELD if str(exhibit.get(field) or "").strip()]
    out.append(_FALLBACK_UNIVERSAL)
    return out


def _never_empty(questions: Sequence[str], max_questions: int, exclude: Iterable[str] = ()) -> List[str]:
    """Отфильтровать набор, но не отдать пустой список.

    Наборы здесь маленькие (три-четыре строки), и `exclude` выедает их за пару
    реплик: посетитель, задавший в общем чате четыре вопроса подряд, иначе
    остался бы ровно с той пустотой, на которую жалуется п. II-7. Повторить
    общий вопрос про музей не страшно — в отличие от вопроса, на который гид
    отказался отвечать: сюда `refused` не передаётся вообще.
    """
    if max_questions <= 0:
        return []
    filtered = _serve(list(questions), max_questions, tuple(exclude))
    return filtered or _serve(list(questions), max_questions) or list(questions)[:max_questions]


def museum_questions(max_questions: int, exclude: Iterable[str] = ()) -> List[str]:
    """Подсказки для общего чата (контекста нет) — п. II-7, сценарий 4."""
    return _never_empty(MUSEUM_QUESTIONS, max_questions, exclude)


def hall_questions(max_questions: int, exclude: Iterable[str] = ()) -> List[str]:
    """Подсказки, когда в контексте только зал — п. II-7, сценарий 3."""
    return _never_empty(HALL_QUESTIONS, max_questions, exclude)


def halls_overview_questions(
    halls: Sequence[Tuple[Optional[int], Optional[str]]],
    max_questions: int,
    exclude: Iterable[str] = (),
) -> List[str]:
    """Подсказки после ответа со списком залов — п. II-7, сценарий 2.

    `halls` — пары (номер, название) в порядке показа; фразу зала строит общий
    `services.location`, чтобы «зал 4 «Синяя гостиная»» и здесь читался ровно
    так же, как в самом ответе гида и в карточке предмета.

    Залы без номера («Вне постоянной экспозиции») пропускаем: это не зал
    экспозиции, а группа для предметов вне неё (баг-репорт 28.07.2026, п.5), и
    приглашать в неё посетителя незачем.
    """
    if max_questions <= 0:
        return []
    asks = [
        f"Что интересного в {location_text.hall_phrase(number, name, 'prep')}?"
        for number, name in halls
        if number is not None
    ]
    # Дедупликация здесь не нужна (номера у залов разные, и `_question_key`
    # разводит их по числам), но воронка одна на все наборы — см. `_serve`.
    # Пустой каталог залов (или все уже спрошены) — не повод показать пустой
    # блок: тогда предлагаем общие вопросы про музей.
    return _serve(asks, max_questions, tuple(exclude)) or _never_empty(MUSEUM_QUESTIONS, max_questions, exclude)


def select_questions(
    pool: Iterable[str],
    max_questions: int,
    *,
    exhibit: Optional[Dict] = None,
    asked: Iterable[str] = (),
    refused: Iterable[str] = (),
) -> List[str]:
    """Что показать посетителю: ярусы послаблений, первый непустой побеждает.

    `asked` — вопросы, уже заданные в этой сессии (включая текущую реплику: она
    ещё не записана в БД к моменту вызова). `refused` — вопросы, на которые гид
    по этому экспонату уже отказался отвечать; память о них ГЛОБАЛЬНАЯ (решение
    Д8), их поднимает вызывающий из `crud.exhibit_refused_questions`.

    Ярусы — по одному послаблению каждый, сверху вниз:

      1. пул минус «уже спрошено» и «уже отказали» — обычный случай, ради
         которого всё и делается: пул перестаёт быть префиксом, и после ответа
         посетитель видит СЛЕДУЮЩИЕ вопросы, а не те же самые (п. II-2);
      2. запас по карточке (`fallback_questions`) с теми же исключениями — пул
         исчерпан, но карточка ещё даёт о чём спросить;
      3. пул и запас минус только «уже отказали» — снимаем `asked`: предложить
         второй раз вопрос, на который ОТВЕТИЛИ, не грех, а вот предложить тот,
         на который отказали, — это и есть жалоба п. II-3;
      4. общий набор про музей. Последняя страховка: блок подсказок не должен
         становиться пустым, иначе починка пп. II-2/II-8 своими руками
         воспроизведёт п. II-7. Отказные формулировки здесь тоже снимаются —
         они возвращаются только в вырожденном случае, когда отказали ВСЕМ
         четырём общим вопросам про музей (на «Какие залы есть в музее?» гид
         отвечает структурой каталога, без модели, так что это практически
         недостижимо); пустой блок в этом месте был бы хуже.

    Ярус 2 отделён от яруса 1 намеренно: если добирать запасом сразу, короткий
    пул («модель отдала два вопроса вместо восьми») всегда дополнялся бы
    карточными, и `max_questions` из потолка превратился бы в требование — а
    `is_fresh` специально не проверяет длину набора, чтобы за этим не гоняться.
    """
    if max_questions <= 0:
        return []
    pool = [q for q in pool]
    both = tuple(asked) + tuple(refused)
    picked = _serve(pool, max_questions, both)
    if picked:
        return picked
    spare = fallback_questions(exhibit)
    picked = _serve(spare, max_questions, both)
    if picked:
        return picked
    picked = _serve(pool + spare, max_questions, tuple(refused))
    if picked:
        return picked
    return _serve(list(MUSEUM_QUESTIONS), max_questions, tuple(refused)) or list(MUSEUM_QUESTIONS)[:max_questions]


def is_fresh(row: Optional[m.ExhibitQuestions], exhibit: Dict, language: str = "ru") -> bool:
    """Годится ли запись кэша: непустая и по тому же исходному тексту.

    Длину набора НЕ проверяем, и это важно: `max_questions` — потолок, а не
    требование. Модель регулярно отдаёт меньше, чем просили (а стаб на карточке
    без мастера — всего три вопроса), и правило «в записи должно лежать ровно
    столько, сколько попросили» означало бы вечный промах: каждый запрос заново
    платит за набор, который заведомо не вырастет. Если пул нужно расширить
    (подняли GUIDE_QUESTIONS_CACHE_SIZE) — это разовый `--force`, а не налог на
    каждое обращение.
    """
    if row is None or not row.questions:
        return False
    return row.source_hash == fingerprint(exhibit, language)


async def for_exhibit(
    session: AsyncSession,
    exhibit: Dict,
    max_questions: int,
    language: str = "ru",
    force: bool = False,
    asked: Iterable[str] = (),
    refused: Iterable[str] = (),
) -> List[str]:
    """Вопросы-подсказки по экспонату: из кэша, при промахе — LLM и запись в кэш.

    `exhibit` — словарь `crud.exhibit_to_dict`. Без `id` (или при выключенном
    кэше) работает как раньше: прямой вызов LLM без похода в БД.

    `asked`/`refused` — память диалога (пп. II-2/II-3), см. `select_questions`.
    Оба необязательные и по умолчанию пустые: у всех прежних вызовов поведение
    не меняется. ВАЖНО: исключения действуют только на ВОЗВРАЩАЕМЫЙ срез — в
    `crud.save_exhibit_questions` уходит полный сырой пул от модели. Иначе кэш
    деградировал бы от сессии к сессии: первый посетитель выел бы из записи свои
    вопросы, и второму досталось бы то, что осталось.
    """
    if max_questions <= 0:
        return []
    exhibit_id = exhibit.get("id")

    def picked(pool: Iterable[str]) -> List[str]:
        """Один и тот же отбор на все пять путей выдачи (кэш выключен, попадание,
        устаревший пул при сбое LLM, свежая генерация)."""
        return select_questions(pool, max_questions, exhibit=exhibit, asked=asked, refused=refused)

    if not settings.guide_questions_cache_enabled or exhibit_id is None:
        questions, _ = await llm.suggested_questions(exhibit, max_questions, language)
        return picked(questions)

    row = await crud.get_exhibit_questions(session, exhibit_id, language)
    if not force and is_fresh(row, exhibit, language):
        return picked(list(row.questions))

    try:
        questions, model = await llm.suggested_questions(exhibit, _pool_size(max_questions), language)
    except UpstreamError:
        # Подсказки — не сам ответ гида: если модель сейчас недоступна, показать
        # прошлый (пусть и устаревший) набор честнее, чем уронить рассказ в 502.
        # Когда прошлого набора нет — ведём себя как до кэша и пробрасываем сбой.
        if row is not None and row.questions:
            logger.warning("guide_questions: LLM недоступен, отдаём устаревший кэш exhibit_id=%s", exhibit_id)
            return picked(list(row.questions))
        raise

    if questions:
        await crud.save_exhibit_questions(
            session, exhibit_id, language, questions, fingerprint(exhibit, language), model
        )
    return picked(questions)


async def warm_exhibit(
    session: AsyncSession,
    ex: m.Exhibit,
    row: Optional[m.ExhibitQuestions],
    language: str = "ru",
    force: bool = False,
    dry_run: bool = False,
) -> Tuple[str, List[str]]:
    """Прогреть одну карточку. Возвращает (итог, вопросы).

    Итог: `cached` — запись уже свежая, LLM не звали; `generated` — сгенерировали
    и записали; `planned` — сухой прогон, генерации не было; `failed` — LLM
    отказал (прогрев продолжается со следующей карточки, см. скрипт).

    Возвращаемый список пропущен через `_serve` — в отчёте прогрева видно то,
    что УВИДИТ ПОСЕТИТЕЛЬ, а не то, что легло в БД. Это осознанно: после запретов
    31.08.2026 (пп. II-4/II-8) пул на части карточек худеет, и отчёт — как раз то
    место, где это должно быть заметно. В саму запись при этом сохраняется сырой
    пул, чтобы отмена запрета не требовала перегенерации.
    """
    exhibit = crud.exhibit_to_dict(ex)
    want = _pool_size(0)
    if not force and is_fresh(row, exhibit, language):
        return "cached", _serve(list(row.questions), want)
    if dry_run:
        return "planned", []
    try:
        questions, model = await llm.suggested_questions(exhibit, want, language)
    except UpstreamError as exc:
        logger.warning("guide_questions: прогрев exhibit_id=%s не удался: %s", ex.id, exc.message)
        return "failed", []
    if not questions:
        return "failed", []
    await crud.save_exhibit_questions(
        session, ex.id, language, questions, fingerprint(exhibit, language), model
    )
    return "generated", _serve(questions, want)
