"""Генерация рассказа и диалог ИИ-гида (Yandex Foundation Models + стаб).

Транспорт — OpenAI-совместимый шлюз ``ai.api.cloud.yandex.net/v1``, а не прежний
``llm.api.cloud.yandex.net/foundationModels/v1``: старый эндпоинт знает только
семейство ``yandexgpt`` и на ``deepseek-v4-flash`` отвечает 404 ``unknown model``.
Модель задаётся переменной ``YANDEXGPT_MODEL_URI`` (имя оставлено прежним, чтобы
не переписывать окружение уже развёрнутой функции).
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

import httpx

from ..config import settings
from . import UpstreamError

DEFAULT_MODEL = "yandexgpt/latest"

_STYLE_HINT = {
    "engaging": "живо и увлекательно",
    "historical": "подробно и исторически точно",
    "short": "коротко, в двух-трёх предложениях",
    "kids": "просто и понятно для детей",
    "expert": "профессионально, для знатока искусства",
}


# ── Публичный интерфейс ──────────────────────────────────────────────────────
async def generate_story(
    exhibit: Dict,
    style: str = "engaging",
    language: str = "ru",
    max_questions: int = 4,
) -> Tuple[str, List[str], str]:
    """Вернуть (текст рассказа, вопросы-подсказки, имя модели)."""
    if settings.llm_configured:
        text = await _llm_story(exhibit, style, language)
        questions = await _llm_questions(exhibit, max_questions, language)
        return text, questions, model_uri()
    text = _story_stub(exhibit, style)
    return text, _questions_stub(exhibit, max_questions), "stub/heuristic"


_SPOKEN_SYSTEM = (
    "Ты — редактор текста для синтеза речи (TTS). Твоя единственная задача — "
    "переписать русский текст так, чтобы его правильно прочитал синтезатор речи: "
    "все римские и арабские числа заменить словами В ПРАВИЛЬНОМ падеже, роде, числе "
    "и разряде (количественное/порядковое) по смыслу предложения."
)
_SPOKEN_USER_TMPL = (
    "Перепиши текст ниже, заменив ВСЕ числа (римские и арабские) словами так, как их "
    "нужно произнести вслух по-русски. Примеры: «Александр III» → «Александр Третий»; "
    "«XIX век» → «девятнадцатый век»; «в 1885 году» → «в тысяча восемьсот восемьдесят "
    "пятом году»; «3 экспоната» → «три экспоната»; «около 50 см» → «около пятидесяти "
    "сантиметров». Порядковые числительные ставь в нужном падеже и роде. Даты, века, "
    "имена монархов, единицы измерения учитывай по контексту. "
    "ВАЖНО: сохрани весь остальной текст дословно, не добавляй и не убирай слова, "
    "не меняй пунктуацию, не добавляй пояснений. Верни ТОЛЬКО переписанный текст.\n\n"
    "Текст:\n{text}"
)


async def to_spoken_text(text: Optional[str]) -> Optional[str]:
    """Вернуть версию текста для озвучки: числа прописью в нужном падеже (E15).

    Использует LLM (если настроен). Возвращает ``None``, когда:
      • входной текст пуст;
      • LLM не настроен (стаб) — тогда вызывающий код озвучивает исходный текст
        с детерминированной нормализацией (римские→арабские, ``normalize_for_tts``);
      • LLM временно недоступен — не роняем сохранение, просто оставляем ``None``.
    """
    if not text or not text.strip():
        return None
    if not settings.llm_configured:
        return None
    try:
        # temperature=0 — задача детерминированная (переписать, не сочинять);
        # запас max_tokens под длинное описание (числа прописью удлиняют текст).
        result = await _llm_complete(
            _SPOKEN_SYSTEM, _SPOKEN_USER_TMPL.format(text=text), temperature=0.0, max_tokens=2000
        )
        return result or None
    except UpstreamError:
        return None


async def chat(
    grounding: str,
    history: List[Tuple[str, str]],
    message: str,
    language: str = "ru",
    max_questions: int = 3,
    exhibit: Optional[Dict] = None,
) -> Tuple[str, List[str]]:
    """Вернуть (ответ гида, новые вопросы-подсказки)."""
    if settings.llm_configured:
        answer = await _llm_chat(grounding, history, message, language)
        questions = await _llm_questions(exhibit or {}, max_questions, language) if exhibit else []
        return answer, questions
    return _chat_stub(grounding, message), _questions_stub(exhibit or {}, max_questions)


# ── Стаб ─────────────────────────────────────────────────────────────────────
def _story_stub(exhibit: Dict, style: str) -> str:
    name = exhibit.get("name", "экспонат")
    year = exhibit.get("year_created")
    dating = (exhibit.get("dating") or "").strip()
    master = exhibit.get("master_name")
    material = exhibit.get("material")
    techniques = (exhibit.get("techniques") or "").strip()
    short = exhibit.get("short_description")
    raw = exhibit.get("raw_history")

    # Датировку берём из dating, а year_created оставляем запасным вариантом: он держит
    # только НИЖНЮЮ границу и пуст у вековых датировок, поэтому гид говорил «созданное
    # в 1899 году» там, где в путеводителе «1899–1903», а на «конец XIX века» умалчивал
    # о времени вовсе (баг-репорт 12.08.2026, п.5). Сама dating приходит именительной
    # фразой («около 1912», «конец XIX — начало XX века»), в оборот «созданное в … году»
    # её не поставить — поэтому вводная строится как подпись на музейной этикетке,
    # перечислением через запятую. По той же причине не склоняем и мастера: в поле
    # лежат и люди («Михаил Перхин»), и фирмы («Фирма К. Фаберже»).
    label = [f for f in (dating or (f"{year} год" if year else ""), master) if f]
    intro = f"Перед вами {name}"
    if label:
        intro += ", " + ", ".join(label)
    intro += "."

    parts = [intro]
    if short:
        parts.append(short)
    if raw:
        parts.append(raw)
    elif material:
        parts.append(f"В работе использованы материалы: {material}.")
    # Техники — отдельной фразой и только когда они есть: в material их больше нет
    # (всё после «;» в каталожной строке — это techniques), а посетителю как раз
    # интересно, как предмет сделан.
    if techniques:
        parts.append(f"Техники исполнения: {techniques}.")
    parts.append(f"(Рассказ подготовлен {_STYLE_HINT.get(style, 'живо и увлекательно')}.)")
    return " ".join(parts)


def _chat_stub(grounding: str, message: str) -> str:
    base = grounding.strip() or "К сожалению, подробных сведений об этом предмете немного."
    return f"Отвечая на ваш вопрос «{message}»: {base}"


def _questions_stub(exhibit: Dict, max_questions: int) -> List[str]:
    slug = (exhibit.get("label_slug") or "")
    master = exhibit.get("master_name")
    pool: List[str] = []
    if slug.startswith("faberge_egg"):
        pool += ["Кому подарили это яйцо?", "Что за сюрприз спрятан внутри?", "Сколько времени ушло на создание?"]
    if master:
        pool.append(f"Что ещё создал мастер {master}?")
    pool += [
        "Из каких материалов он сделан?",
        "Какая история скрыта за этим предметом?",
        "Что ещё посмотреть в этом зале?",
    ]
    # уникализируем, сохраняя порядок
    seen, result = set(), []
    for q in pool:
        if q not in seen:
            seen.add(q)
            result.append(q)
    return result[: max(0, max_questions)]


# ── Yandex Foundation Models ─────────────────────────────────────────────────
def model_uri() -> str:
    """URI активной модели (для поля ``model`` в ответах API)."""
    return settings.yandexgpt_model_uri or f"gpt://{settings.yandex_folder_id}/{DEFAULT_MODEL}"


async def _llm_complete(system: str, user: str, temperature: float = 0.6, max_tokens: int = 800) -> str:
    payload: Dict = {
        "model": model_uri(),
        "temperature": temperature,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    # Для не-reasoning моделей (семейство yandexgpt) параметр безвреден — шлюз
    # его принимает и игнорирует, так что ветвление по имени модели не нужно.
    if settings.llm_reasoning_effort:
        payload["reasoning_effort"] = settings.llm_reasoning_effort

    # Ключ уходит как Bearer: OpenAI-совместимый шлюз не понимает схему `Api-Key`,
    # которой требовал foundationModels/v1. Каталог — заголовком OpenAI-Project.
    headers = {"Authorization": f"Bearer {settings.yandex_api_key}"}
    if settings.yandex_folder_id:
        headers["OpenAI-Project"] = settings.yandex_folder_id

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(f"{settings.llm_api_base}/chat/completions", headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:  # noqa: BLE001
        raise UpstreamError("Сервис генерации текста временно недоступен.") from exc

    choice = (data.get("choices") or [{}])[0]
    text = ((choice.get("message") or {}).get("content") or "").strip()
    if text:
        return text
    # Пустой content — это НЕ сбой сети, поэтому диагностируем отдельно. У
    # reasoning-модели так выглядит исчерпанный бюджет: весь max_tokens ушёл в
    # reasoning_content, ответа не осталось. Молча вернуть "" нельзя — гид
    # покажет посетителю пустой пузырь, а в аналитику попадёт «ответ дан».
    if choice.get("finish_reason") == "length":
        raise UpstreamError(
            "Модель исчерпала лимит токенов на размышления и не выдала ответ. "
            "Увеличьте max_tokens или задайте LLM_REASONING_EFFORT=none."
        )
    raise UpstreamError("Сервис генерации текста вернул пустой ответ.")


def exhibit_facts(exhibit: Dict) -> str:
    """Карточка экспоната для промпта: чем предмет является и что о нём известно.

    Раньше в промпт уходил только ``raw_history``. Это техническая карточка
    (Фирма / Дата / Мастер / Материалы / Техника / Зал), и слова «яйцо» в ней нет
    у 7 экспонатов из 12 — модель не знала, ЧТО описывает, и угадывала:
    «Коронационное» превращалось в «уникальную шкатулку». Название и атрибуция
    берутся из отдельных колонок каталога, поэтому подаём их явно.

    Тот же текст идёт и в справку для ``/guide/chat``: `master_name`, `material`,
    датировка есть в БД, но в диалог не попадали, и на вопрос о мастере гид
    отвечал «не знаю» при заполненной колонке.
    """
    # Датировка — строка путеводителя как есть («1899–1903», «конец XIX — начало
    # XX века»). `year_created` держит только НИЖНЮЮ границу и пуст у вековых
    # датировок, из-за чего гид писал «созданное в 1899 году» про предмет
    # 1899–1903 (баг-репорт 12.08.2026, п.5). Оставляем его запасным вариантом.
    dating = (exhibit.get("dating") or "").strip() or (
        str(exhibit["year_created"]) if exhibit.get("year_created") else ""
    )
    parts: List[str] = []
    for label, value in (
        ("Экспонат", exhibit.get("name")),
        ("Датировка", dating),
        ("Мастер", exhibit.get("master_name")),
        ("Материалы", exhibit.get("material")),
        # Техники больше не лежат в material (всё после «;» каталожной строки —
        # это techniques), а посетителя как раз интересует, как предмет сделан.
        ("Техники исполнения", (exhibit.get("techniques") or "").strip()),
    ):
        if value:
            parts.append(f"{label}: {value}")
    for key in ("short_description", "raw_history"):
        value = exhibit.get(key)
        if value:
            parts.append(str(value))
    return "\n".join(parts)


async def _llm_story(exhibit: Dict, style: str, language: str) -> str:
    # Датировка и техники подаются через exhibit_facts вместе с остальной
    # карточкой — пустые поля туда не попадают, поэтому «датировка: » или
    # «год: None», которые модель трактовала как факт, исключены.
    user = (
        "Напиши интересную историю для посетителя музея, используя данные:\n"
        f"{exhibit_facts(exhibit)}\n"
        f"Стиль: {_STYLE_HINT.get(style, 'живо и увлекательно')}."
    )
    return await _llm_complete(
        "Ты — ИИ-гид музея Фаберже.", user, max_tokens=settings.llm_max_tokens_story
    )


# Системный промпт диалога. Прежняя формулировка подавала grounding как «Контекст
# об экспонате», и модель трактовала его как ЕДИНСТВЕННЫЙ источник: на общий
# вопрос «Пётр I», заданный после выхода из Рыцарского зала, отвечала «в
# предоставленных материалах о Рыцарском зале нет информации» (баг-репорт
# 28.07.2026, п.3). Теперь справка — подсказка, а не рамка.
_CHAT_SYSTEM = (
    "Ты — ИИ-гид музея Фаберже в Санкт-Петербурге (Шуваловский дворец). Отвечай "
    "кратко и по делу, на русском языке.\n"
    "Ниже может быть приложена справка о зале или экспонате, рядом с которым сейчас "
    "находится посетитель. Это ПОДСКАЗКА, а не граница твоих знаний: используй её, "
    "если она относится к вопросу, и просто игнорируй, если вопрос об этом не спрашивает.\n"
    "Если ответа в справке нет, отвечай по своим знаниям о музее, коллекции Фаберже, "
    "ювелирном искусстве и истории России. НЕ отвечай «в предоставленных материалах "
    "нет информации» и не ссылайся на то, что справка чего-то не содержит, — это "
    "выглядит как отказ. Если не знаешь ответа, так и скажи прямо.\n"
    "Никогда не выдумывай то, чего не знаешь наверняка: планировку музея, этажи, "
    "маршруты, номера залов, имена мастеров и даты. Если этих сведений нет в справке, "
    "прямо скажи, что не знаешь, и предложи уточнить у сотрудника музея."
)


async def _llm_chat(grounding: str, history: List[Tuple[str, str]], message: str, language: str) -> str:
    convo = "\n".join(f"{r}: {c}" for r, c in history[-6:])
    parts = []
    if grounding.strip():
        parts.append(f"Справка о текущем месте посетителя (может быть не связана с вопросом): {grounding}")
    if convo:
        parts.append(f"История диалога:\n{convo}")
    parts.append(f"Вопрос посетителя: {message}")
    return await _llm_complete(_CHAT_SYSTEM, "\n".join(parts))


async def _llm_questions(exhibit: Dict, max_questions: int, language: str) -> List[str]:
    if max_questions <= 0:
        return []
    user = (
        f"На основе данных об экспонате ({exhibit_facts(exhibit)}) предложи {max_questions} "
        "коротких вопроса, которые посетитель захотел бы задать гиду. "
        "Каждый вопрос с новой строки, без нумерации."
    )
    text = await _llm_complete("Ты помогаешь придумать вопросы для диалога с гидом.", user, temperature=0.7, max_tokens=200)
    questions = [q.strip(" -•\t") for q in text.splitlines() if q.strip()]
    return questions[:max_questions]
