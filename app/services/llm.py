"""Генерация рассказа и диалог ИИ-гида (YandexGPT + стаб)."""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

import httpx

from ..config import settings
from . import UpstreamError

YANDEXGPT_URL = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"

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
        return text, questions, settings.yandexgpt_model_uri or "yandexgpt/latest"
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
        result = await _yandexgpt_complete(
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
        answer = await _yandexgpt_chat(grounding, history, message, language)
        questions = await _yandexgpt_questions(exhibit or {}, max_questions, language) if exhibit else []
        return answer, questions
    return _chat_stub(grounding, message), _questions_stub(exhibit or {}, max_questions)


# ── Стаб ─────────────────────────────────────────────────────────────────────
def _story_stub(exhibit: Dict, style: str) -> str:
    name = exhibit.get("name", "экспонат")
    year = exhibit.get("year_created")
    master = exhibit.get("master_name")
    material = exhibit.get("material")
    short = exhibit.get("short_description")
    raw = exhibit.get("raw_history")

    intro = f"Перед вами {name}"
    if year:
        intro += f", созданное в {year} году"
    if master:
        intro += f" мастером {master}"
    intro += "."

    parts = [intro]
    if short:
        parts.append(short)
    if raw:
        parts.append(raw)
    elif material:
        parts.append(f"В работе использованы материалы: {material}.")
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
async def _yandexgpt_complete(system: str, user: str, temperature: float = 0.6, max_tokens: int = 800) -> str:
    payload = {
        "modelUri": settings.yandexgpt_model_uri or f"gpt://{settings.yandex_folder_id}/yandexgpt/latest",
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
            return data["result"]["alternatives"][0]["message"]["text"].strip()
    except Exception as exc:  # noqa: BLE001
        raise UpstreamError("Сервис генерации текста временно недоступен.") from exc


async def _yandexgpt_story(exhibit: Dict, style: str, language: str) -> str:
    # Промпт из роадмапа.
    raw = exhibit.get("raw_history") or exhibit.get("short_description") or ""
    user = (
        f"Напиши интересную историю для посетителя музея, используя данные: {raw}, "
        f"стиль: {_STYLE_HINT.get(style, 'живо и увлекательно')}, год: {exhibit.get('year_created')}."
    )
    return await _yandexgpt_complete("Ты — ИИ-гид музея Фаберже.", user)


async def _yandexgpt_chat(grounding: str, history: List[Tuple[str, str]], message: str, language: str) -> str:
    convo = "\n".join(f"{r}: {c}" for r, c in history[-6:])
    user = f"Контекст об экспонате: {grounding}\nИстория диалога:\n{convo}\nВопрос посетителя: {message}"
    return await _yandexgpt_complete("Ты — ИИ-гид музея Фаберже, отвечай кратко и по делу.", user)


async def _yandexgpt_questions(exhibit: Dict, max_questions: int, language: str) -> List[str]:
    if max_questions <= 0:
        return []
    raw = exhibit.get("raw_history") or exhibit.get("short_description") or exhibit.get("name", "")
    user = (
        f"На основе данных об экспонате ({raw}) предложи {max_questions} коротких вопроса, "
        "которые посетитель захотел бы задать гиду. Каждый вопрос с новой строки, без нумерации."
    )
    text = await _yandexgpt_complete("Ты помогаешь придумать вопросы для диалога с гидом.", user, temperature=0.7, max_tokens=200)
    questions = [q.strip(" -•\t") for q in text.splitlines() if q.strip()]
    return questions[:max_questions]
