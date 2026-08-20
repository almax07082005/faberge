"""Генерация рассказа и диалог ИИ-гида (YandexGPT + стаб)."""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

import httpx

from ..config import settings
from . import UpstreamError

logger = logging.getLogger(__name__)

YANDEXGPT_URL = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"

# ── Выбор модели ─────────────────────────────────────────────────────────────
# Не все задачи гида одинаково «умные». Рассказ и диалог — творческие, их
# оставляем на основной модели; переписывание чисел прописью (to_spoken_text) —
# механическая правка текста, на ней Pro-модель просто дороже при том же
# результате, поэтому туда идёт lite.
_PRO_SEGMENT = "/yandexgpt/"
_LITE_SEGMENT = "/yandexgpt-lite/"


def _model_uri(lite: bool = False) -> str:
    """URI модели для запроса: основная или lite (см. YANDEXGPT_LITE_MODEL_URI)."""
    base = settings.yandexgpt_model_uri or (
        f"gpt://{settings.yandex_folder_id}/yandexgpt/latest" if settings.yandex_folder_id else ""
    )
    if not lite:
        return base
    if settings.yandexgpt_lite_model_uri:
        return settings.yandexgpt_lite_model_uri
    # Выводим lite из основного URI. Если основная модель дообученная (ds://…)
    # или названа иначе, подменять нечего — работаем на ней же, чтобы запрос не
    # ушёл в несуществующую модель.
    if _PRO_SEGMENT in base:
        return base.replace(_PRO_SEGMENT, _LITE_SEGMENT)
    return base


# ── Учёт расхода ─────────────────────────────────────────────────────────────
def _as_int(value: Any) -> Optional[int]:
    """usage в ответе YandexGPT приходит строками ("inputTextTokens": "412")."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _log_usage(operation: str, model_uri: str, result: Dict) -> None:
    """Строка расхода на каждый вызов LLM — по ней считается стоимость ответа.

    Формат намеренно plain-text key=value: логи Cloud Functions ищутся
    подстрокой, а грепать `llm_usage operation=chat` проще, чем JSON.
    """
    if not settings.llm_log_usage:
        return
    usage = result.get("usage") or {}
    logger.info(
        "llm_usage operation=%s model=%s model_version=%s input_tokens=%s output_tokens=%s total_tokens=%s",
        operation,
        model_uri or "-",
        result.get("modelVersion") or "-",
        _as_int(usage.get("inputTextTokens")),
        _as_int(usage.get("completionTokens")),
        _as_int(usage.get("totalTokens")),
    )


def _shorten(text: str, limit: int) -> str:
    """Обрезать справку до limit знаков по границе предложения.

    Нужна ровно для промпта: в диалог уходит не весь raw_history (бывает
    несколько тысяч знаков и оплачивается на КАЖДОМ уточняющем вопросе), а его
    начало — там всегда главное о предмете. Режем по концу фразы, чтобы модель
    не получала оборванное полуслово и не додумывала его.
    """
    text = (text or "").strip()
    if limit <= 0 or len(text) <= limit:
        return text
    head = text[:limit]
    cut = max(head.rfind(". "), head.rfind("! "), head.rfind("? "), head.rfind("; "))
    if cut >= limit // 2:
        return head[: cut + 1].rstrip()
    space = head.rfind(" ")
    return (head[:space] if space > 0 else head).rstrip(" ,;:—-") + "…"


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
        text = await _yandexgpt_story(exhibit, style, language)
        questions = await _yandexgpt_questions(exhibit, max_questions, language)
        return text, questions, _model_uri() or "yandexgpt/latest"
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

    Использует YandexGPT (если настроен). Возвращает ``None``, когда:
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
        # lite=True: заменить цифры словами — работа механическая, разницы с
        # Pro на ней нет, а токены дешевле в разы. Качество подстраховано с двух
        # сторон: вызывающий код в tts.prepare_for_tts отбрасывает явно
        # «разговорившийся» ответ, а без LLM остаётся normalize_for_tts.
        result = await _yandexgpt_complete(
            _SPOKEN_SYSTEM,
            _SPOKEN_USER_TMPL.format(text=text),
            temperature=0.0,
            max_tokens=2000,
            operation="tts_spoken",
            lite=True,
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
        answer = await _yandexgpt_chat(grounding, history, message, language)
        questions = await _yandexgpt_questions(exhibit or {}, max_questions, language) if exhibit else []
        return answer, questions
    return _chat_stub(grounding, message), _questions_stub(exhibit or {}, max_questions)


# ── Стаб ─────────────────────────────────────────────────────────────────────
def _story_stub(exhibit: Dict, style: str) -> str:
    name = exhibit.get("name", "экспонат")
    dating = (exhibit.get("year_created") or "").strip()
    master = exhibit.get("master_name")
    material = exhibit.get("material")
    techniques = (exhibit.get("techniques") or "").strip()
    short = exhibit.get("short_description")
    raw = exhibit.get("raw_history")

    # year_created — датировка строкой как в путеводителе (с 17.08.2026 поле датировки
    # одно). Она приходит именительной фразой («около 1912», «конец XIX — начало XX
    # века»), в оборот «созданное в … году» её не поставить — поэтому вводная строится
    # как подпись на музейной этикетке, перечислением через запятую. По той же причине
    # не склоняем и мастера: в поле лежат и люди («Михаил Перхин»), и фирмы
    # («Фирма К. Фаберже»).
    label = [f for f in (dating, master) if f]
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


# ── YandexGPT ────────────────────────────────────────────────────────────────
async def _yandexgpt_complete(
    system: str,
    user: str,
    temperature: float = 0.6,
    max_tokens: int = 800,
    operation: str = "complete",
    lite: bool = False,
) -> str:
    model_uri = _model_uri(lite)
    payload = {
        "modelUri": model_uri,
        "completionOptions": {"stream": False, "temperature": temperature, "maxTokens": str(max_tokens)},
        "messages": [
            {"role": "system", "text": system},
            {"role": "user", "text": user},
        ],
    }
    headers = {"Authorization": f"Api-Key {settings.yandex_api_key}"}
    if settings.yandex_folder_id:
        headers["x-folder-id"] = settings.yandex_folder_id
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(YANDEXGPT_URL, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
            result = data["result"]
            # Расход — в лог до возврата ответа: по строкам llm_usage считается
            # стоимость и видно, какая операция её создаёт.
            _log_usage(operation, model_uri, result)
            return result["alternatives"][0]["message"]["text"].strip()
    except Exception as exc:  # noqa: BLE001
        raise UpstreamError("Сервис генерации текста временно недоступен.") from exc


async def _yandexgpt_story(exhibit: Dict, style: str, language: str) -> str:
    # Промпт из роадмапа. «Год» в нём заменён на «датировку»: раньше в промпт уходил
    # числовой year_created — нижняя граница диапазона, — и модель уверенно писала
    # «созданное в 1899 году» про предмет, датированный «1899–1903», а у вековых
    # датировок получала «год: None» и придумывала дату сама (баг-репорт 12.08.2026,
    # п.5). Теперь year_created и есть строка путеводителя — отдаём её как есть.
    raw = exhibit.get("raw_history") or exhibit.get("short_description") or ""
    dating = (exhibit.get("year_created") or "").strip()
    techniques = (exhibit.get("techniques") or "").strip()
    user = (
        f"Напиши интересную историю для посетителя музея, используя данные: {raw}, "
        f"стиль: {_STYLE_HINT.get(style, 'живо и увлекательно')}"
    )
    # Пустое поле в промпт не подаём: «датировка: » или «год: None» модель трактует
    # как факт и дорисовывает недостающее.
    if dating:
        user += f", датировка: {dating}"
    if techniques:
        user += f", техники исполнения: {techniques}"
    user += "."
    # Объём. Раньше ограничения не было вовсе, и модель выдавала ~2500 знаков:
    # столько у витрины не слушают, а платим мы за каждый выходной токен.
    # Ориентир задаём промптом, а не обрезкой — обрезка рвёт фразу на полуслове.
    # max_tokens при этом держим с запасом над целью (GUIDE_STORY_MAX_TOKENS),
    # чтобы он оставался страховкой от «разговорившейся» модели, а не резал
    # нормальный рассказ.
    user += (
        f" Объём — не больше {settings.guide_story_max_chars} знаков, это примерно "
        "5–7 предложений. Не пересказывай данные подряд: выбери главное — кто, когда, "
        "чем предмет примечателен — и не повторяйся. Без вступлений вроде «сегодня мы "
        "поговорим» и без перечня характеристик списком. Опирайся ТОЛЬКО на данные "
        "выше и на общеизвестные факты о фирме Фаберже: не придумывай имён, владельцев, "
        "заказчиков, событий и дат, которых в данных нет. Чего не знаешь — не пиши."
    )
    return await _yandexgpt_complete(
        "Ты — ИИ-гид музея Фаберже.",
        user,
        max_tokens=settings.guide_story_max_tokens,
        operation="story",
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
    "выглядит как отказ. Если не знаешь ответа, так и скажи прямо."
)


async def _yandexgpt_chat(grounding: str, history: List[Tuple[str, str]], message: str, language: str) -> str:
    # Входной контекст уточняющего вопроса — самая частая статья расхода: он
    # уходит в модель на КАЖДУЮ реплику. Режем его с двух сторон — историю до
    # GUIDE_HISTORY_TURNS последних реплик (было 6) и справку до
    # GUIDE_GROUNDING_MAX_CHARS знаков по границе фразы. Обе величины в env:
    # если качество ответов просядет, откатывается без релиза.
    turns = max(0, settings.guide_history_turns)
    convo = "\n".join(f"{r}: {c}" for r, c in (history[-turns:] if turns else []))
    parts = []
    grounding = _shorten(grounding, settings.guide_grounding_max_chars)
    if grounding:
        parts.append(f"Справка о текущем месте посетителя (может быть не связана с вопросом): {grounding}")
    if convo:
        parts.append(f"История диалога:\n{convo}")
    parts.append(f"Вопрос посетителя: {message}")
    return await _yandexgpt_complete(_CHAT_SYSTEM, "\n".join(parts), operation="chat")


async def _yandexgpt_questions(exhibit: Dict, max_questions: int, language: str) -> List[str]:
    if max_questions <= 0:
        return []
    raw = exhibit.get("raw_history") or exhibit.get("short_description") or exhibit.get("name", "")
    user = (
        f"На основе данных об экспонате ({raw}) предложи {max_questions} коротких вопроса, "
        "которые посетитель захотел бы задать гиду. Каждый вопрос с новой строки, без нумерации."
    )
    text = await _yandexgpt_complete(
        "Ты помогаешь придумать вопросы для диалога с гидом.",
        user,
        temperature=0.7,
        max_tokens=200,
        operation="questions",
    )
    questions = [q.strip(" -•\t") for q in text.splitlines() if q.strip()]
    return questions[:max_questions]
