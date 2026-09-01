"""Превью описания зала (баг-репорт 31.08.2026, п. I-3).

Музей: «Сократить видимую часть текста о зале, добавить опцию раскрытия полного текста
"подробнее о зале". Таким образом на одной странице пользователь увидит, что есть
информация о зале и о витринах.» На бэкенде это два вычисляемых поля схемы `Hall`:
`description_preview` (начало описания, обрезанное ПО ГРАНИЦЕ ПРЕДЛОЖЕНИЯ) и
`description_has_more` (показывать ли кнопку). Полный `description` остаётся на месте и
полным — правка чисто аддитивная.

Тесты проверяют ПОВЕДЕНИЕ обрезки, а не снимок каталога. Это принципиально: тексты
описаний живут в `db/hall_descriptions.json` и музей их переписывает — п. I-1 того же
баг-репорта как раз даёт «Парадной лестнице» новый, более длинный текст. Тест, зашитый
под нынешнюю формулировку конкретного зала, упал бы на чужой правке данных и ничего бы
при этом не поймал. Поэтому по реальным описаниям проверяются только ИНВАРИАНТЫ —
утверждения, верные для любого текста (`test_real_hall_texts_hold_the_invariants`).

ГРАНИЦА С `tests/test_text_normalize.py`: общие свойства самой обрезки
(`shorten_to_sentence` — границы предложения, пустой вход, нулевой лимит, дефолтный
`min_ratio`) проверяются ТАМ, у функции, вместе с остальным `text_normalize`. Здесь —
только то, что про превью зала: как схема `Hall` применяет обрезку, какой порог выбрала,
и что настройка длины делает с ответом. Дублировать проверки функции в двух файлах —
значит чинить один и тот же тест дважды и не заметить, если один из них отстанет.

Ни БД, ни сети: обрезка — чистая функция, схемы собираются напрямую. Запуск:
    python -m pytest tests/test_hall_preview.py
"""
from __future__ import annotations

import contextlib
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import schemas as sch  # noqa: E402
from app.config import settings  # noqa: E402

DESC_FILE = os.path.join(ROOT, "db", "hall_descriptions.json")


@contextlib.contextmanager
def preview_limit(chars: int):
    """Временно подменить HALL_DESCRIPTION_PREVIEW_CHARS.

    Настройки читаются из общего объекта `settings` в момент сериализации, поэтому
    подмена атрибутом — то же, что делает tests/test_llm_cost.py, и восстановление
    обязательно: объект один на весь прогон.
    """
    saved = settings.hall_description_preview_chars
    settings.hall_description_preview_chars = chars
    try:
        yield
    finally:
        settings.hall_description_preview_chars = saved


# Описание заведомо длиннее любого разумного лимита. Без хвостового пробела: обрезка
# стрипает вход, и при выключенном превью сравнение «превью == описание» иначе спотыкалось
# бы о пробел, а не о поведение кода.
LONG_TEXT = ("Первое предложение о зале. " + "Продолжение рассказа о его убранстве. " * 20).strip()


def hall(description, **extra) -> sch.Hall:
    return sch.Hall(id=1, hall_number=2, name="Рыцарский зал", description=description, **extra)


# ── Как схема зала применяет обрезку ────────────────────────────────────────────────────
def test_preview_never_breaks_a_word_in_the_middle():
    """Инвариант превью: оно либо кончается знаком конца фразы, либо явным «…».

    Обрыв посреди слова без многоточия — то, ради чего обрезку вообще вынесли на бэкенд:
    на клиенте `substring(0, N)` даёт ровно его.
    """
    for tail in ("Продолжение фразы, которое в превью не поместится никак." * 5,):
        for head in ("Рыцарский зал отделан по проекту Бернара де Симона. ", "Одно длинное предложение без точек "):
            preview = hall(head + tail).description_preview
            assert preview.endswith((".", "!", "?", ";", "…")), preview


def test_hall_preview_prefers_a_short_sentence_over_a_broken_word():
    """Какой `min_ratio` выбрало превью зала — и почему не тот, что у промпта гида.

    Первое предложение короткое, второе — длинное. С дефолтным порогом 0.5 (его берёт
    промпт диалога: там полфразы контекста хуже оборванного слова) граница отбрасывается
    как «слишком ранняя» и текст рвётся по слову с «…». Превью зала выбирает 1/3 —
    посетителю показывается ЦЕЛАЯ короткая фраза, пусть и короче лимита: цена плохого
    разреза у двух потребителей разная, поэтому порог и стал параметром.

    Само поведение `shorten_to_sentence` при разных порогах проверяется у функции, в
    tests/test_text_normalize.py; здесь — только выбор схемы `Hall`.
    """
    text = "Совсем коротко и ясно. " + "И дальше очень длинное второе предложение про убранство зала."
    with preview_limit(60):
        assert hall(text).description_preview == "Совсем коротко и ясно."
        assert hall(text).description_has_more is True     # остальное прячется под кнопку


# ── Признак «есть что раскрывать» ───────────────────────────────────────────────────────
def test_has_more_is_true_only_when_something_is_hidden():
    """Кнопка «Подробнее о зале» показывается ровно тогда, когда за превью что-то есть."""
    long_hall = hall(LONG_TEXT)
    assert long_hall.description_has_more is True
    assert len(long_hall.description_preview) < len(LONG_TEXT)

    short_hall = hall("Небольшой зал во втором этаже дворца.")
    assert short_hall.description_has_more is False
    assert short_hall.description_preview == "Небольшой зал во втором этаже дворца."


def test_description_exactly_at_the_limit_hides_nothing():
    """Описание ровно в лимит — это ещё «влезло»: превью равно описанию, кнопки нет.

    Лимит подменяем под длину текста, а не подбираем текст под 350 знаков: проверяется
    граница «<= лимита», и она не должна зависеть от дефолта.
    """
    text = "Рыцарский зал отделан по проекту Бернара де Симона."
    with preview_limit(len(text)):
        exact = hall(text)
        assert exact.description_preview == text
        assert exact.description_has_more is False
    # На один знак меньше — текст уже не помещается, и это надо показать кнопкой.
    with preview_limit(len(text) - 1):
        assert hall(text).description_has_more is True


def test_empty_and_missing_description():
    """Пустое описание не должно давать ни превью-огрызка, ни кнопки в никуда."""
    empty = hall(None)
    assert empty.description_preview is None       # null только если null само описание
    assert empty.description_has_more is False

    blank = hall("")
    assert blank.description_preview == ""
    assert blank.description_has_more is False

    # Хвостовые пробелы: обрезка стрипает вход, поэтому сравнение идёт по strip() —
    # иначе описание с пробелом на конце вечно показывало бы кнопку в никуда.
    spaced = hall("  Небольшой зал.  ")
    assert spaced.description_preview == "Небольшой зал."
    assert spaced.description_has_more is False


# ── Где превью появляется ───────────────────────────────────────────────────────────────
def test_preview_is_present_in_the_list_the_card_and_the_map():
    """Список залов, карточка зала и карта — три ручки, но схема `Hall` одна.

    Поля вычисляемые именно поэтому: `sch.Hall` собирается в четырёх местах crud, три из
    которых дублируют друг друга руками, и обычное поле пришлось бы дописывать в каждом
    (а в пятом, дописанном завтра, — забыть).
    """
    text = LONG_TEXT
    expected_preview = hall(text).description_preview

    # GET /halls — список
    listed = sch.HallListResponse(items=[hall(text)], total=1, limit=20, offset=0).model_dump()["items"][0]
    # GET /halls/{id} — карточка зала с витринами
    card = sch.HallDetail(id=1, description=text, showcases=[]).model_dump()
    # GET /map — дерево экспозиции
    mapped = sch.MapResponse(halls=[sch.MapHall(id=1, description=text, showcases=[])]).model_dump()["halls"][0]

    for payload, where in ((listed, "GET /halls"), (card, "GET /halls/{id}"), (mapped, "GET /map")):
        assert payload["description_preview"] == expected_preview, where
        assert payload["description_has_more"] is True, where
        assert payload["description"] == text, where   # полный текст на месте и в каждой ручке


def test_full_description_is_untouched():
    """Обратная совместимость: старые поля на месте, описание — байт в байт исходное."""
    text = LONG_TEXT
    payload = hall(text, level=2, showcase_count=6, exhibit_count=24).model_dump()
    assert payload["description"] == text
    legacy = {"id", "hall_number", "name", "description", "level", "cover_image_url",
              "is_temporary", "is_service", "sort_order", "showcase_count", "exhibit_count"}
    assert legacy <= set(payload), legacy - set(payload)


# ── Настройка длины ─────────────────────────────────────────────────────────────────────
def test_limit_is_configurable_and_zero_turns_the_preview_off():
    """HALL_DESCRIPTION_PREVIEW_CHARS — потолок; 0 отключает превью целиком."""
    text = LONG_TEXT
    with preview_limit(120):
        short = hall(text)
        assert len(short.description_preview) <= 120
        assert short.description_has_more is True
    with preview_limit(0):
        off = hall(text)
        assert off.description_preview == text
        assert off.description_has_more is False


def test_default_limit_is_a_sane_screenful():
    """Дефолт — не «сколько-нибудь», а подобранная под первый экран величина.

    Проверяем диапазон, а не точное число: 350 знаков — это 4–6 строк на телефоне, ниже
    ~200 превью перестаёт быть осмысленным абзацем, выше ~700 (столько уходит в промпт
    гида) теряется весь смысл сокращения.
    """
    assert 200 <= settings.hall_description_preview_chars <= 700


# ── Реальные описания каталога: только инварианты ───────────────────────────────────────
def test_real_hall_texts_hold_the_invariants():
    """Прогон по db/hall_descriptions.json — БЕЗ ожиданий по конкретным формулировкам.

    Тексты залов музей переписывает (п. I-1 меняет описание «Парадной лестницы»), поэтому
    сверять превью с зашитой строкой нельзя: тест ловил бы правку данных, а не поломку
    кода. Утверждаем то, что обязано быть верно для ЛЮБОГО описания.
    """
    halls = json.load(open(DESC_FILE, encoding="utf-8"))
    assert halls, "файл описаний пуст — проверять нечего"
    limit = settings.hall_description_preview_chars
    for key, entry in halls.items():
        text = (entry.get("description") or "").strip()
        preview = hall(entry.get("description")).description_preview
        where = f"зал {key} ({entry.get('name')})"
        # 1. Превью — это НАЧАЛО описания, а не пересказ: с точностью до многоточия оно
        #    дословный префикс. Значит, показать его вместо описания безопасно.
        assert text.startswith(preview.rstrip("…")), where
        # 2. Лимит соблюдён (+1 знак на само многоточие).
        assert len(preview) <= limit + 1, f"{where}: {len(preview)} знаков"
        # 3. Признак согласован с превью: кнопка есть тогда и только тогда, когда текст
        #    в превью не уместился.
        assert hall(entry.get("description")).description_has_more is (preview != text), where
        # 4. Длинное описание обязано сократиться — ради этого всё и затевалось.
        if len(text) > limit:
            assert len(preview) < len(text), where


if __name__ == "__main__":
    import pytest

    sys.exit(pytest.main([__file__, "-v"]))
