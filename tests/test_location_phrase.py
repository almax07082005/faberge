"""Юнит-тесты вынесенной формулировки расположения (баг-репорт 31.08.2026, п. I-2).

Фразу «в зале 4 «Синяя гостиная», витрина 5» посетитель читает в ответе гида, а
с 31.08.2026 её же — готовой строкой — просят в карточке предмета. Поэтому текст
переехал из `app/routers/guide.py` в `app/services/location.py`, а в роутере
остались тонкие обёртки.

Переезд обязан быть БЕЗ смены поведения, и проверяется это не глазами: ниже
лежит ДОСЛОВНАЯ копия прежней реализации (слепок guide.py на 31.08.2026), и по
всем сочетаниям «зал с номером и без / витрина с номером и без / экспонат без
витрины / предложный падеж» новая фраза сравнивается со старой символ в символ.
Рядом — литеральные ожидания: сверка с копией самой себя не заметила бы, если бы
скопировали уже сломанное.

БД и сеть не нужны: зал, витрина и экспонат подменяются простыми объектами с теми
же полями — формулировке ORM не нужен, в этом и смысл выноса. Запуск:
    python -m pytest tests/test_location_phrase.py
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.routers import guide  # noqa: E402
from app.services import location  # noqa: E402


# ── Минимальные заменители ORM ───────────────────────────────────────────────
@dataclass
class Hall:
    hall_number: Optional[int] = None
    name: Optional[str] = None


@dataclass
class HallForListing(Hall):
    """Зал со служебным признаком — его читает только `_describe_halls`."""

    is_temporary: bool = False


@dataclass
class Showcase:
    showcase_number: Optional[int] = None
    hall: Optional[Hall] = None


@dataclass
class Exhibit:
    showcase: Optional[Showcase] = None
    name: str = "Экспонат"
    exhibit_number: Optional[str] = None
    short_description: Optional[str] = None


# ── Слепок прежней реализации (app/routers/guide.py, 31.08.2026, до выноса) ───
# Копия дословная — её нельзя «заодно причесать»: она здесь именно как эталон
# того, что видел посетитель до правки.
def _legacy_hall_phrase(hall, case: str = "nom") -> str:
    word = "зале" if case == "prep" else "зал"
    name = f" «{hall.name}»" if hall.name else ""
    if hall.hall_number is None:
        return f"{word}{name}" if name else word
    return f"{word} {hall.hall_number}{name}"


def _legacy_location_phrase(ex) -> str:
    if ex.showcase is None:
        return ""
    hall = ex.showcase.hall
    parts = []
    if hall is not None:
        parts.append("в " + _legacy_hall_phrase(hall, case="prep"))
    parts.append(
        f"витрина {ex.showcase.showcase_number}" if ex.showcase.showcase_number is not None
        else "вне витрин"
    )
    return ", ".join(parts)


# Залы прода: пронумерованный, «Вне постоянной экспозиции» (номера нет по
# требованию заказчика от 28.07.2026, п.5) и вырожденный без номера и названия.
HALLS = [
    Hall(4, "Синяя гостиная"),
    Hall(1, "Рыцарский зал"),
    Hall(None, "Вне постоянной экспозиции"),
    Hall(12, None),
    Hall(None, None),
]

# Экспонаты: витрина с номером и без, витрина-сирота (зал не привязан),
# экспонат вообще вне витрин.
EXHIBITS = [
    Exhibit(Showcase(5, HALLS[0])),
    Exhibit(Showcase(None, HALLS[0])),
    Exhibit(Showcase(11, HALLS[2])),
    Exhibit(Showcase(None, HALLS[2])),
    Exhibit(Showcase(7, HALLS[3])),
    Exhibit(Showcase(1, HALLS[4])),
    Exhibit(Showcase(5, None)),
    Exhibit(Showcase(None, None)),
    Exhibit(None),
]


def test_hall_phrase_is_unchanged():
    """Фраза о зале — байт-в-байт прежняя и у обёртки, и у вынесенной функции."""
    for hall in HALLS:
        for case in ("nom", "prep"):
            expected = _legacy_hall_phrase(hall, case)
            assert guide._hall_phrase(hall, case) == expected, (hall, case)
            assert location.hall_phrase(hall.hall_number, hall.name, case) == expected, (hall, case)


def test_hall_phrase_literals():
    """Те же строки, но записанные руками: эталон-копия не заметила бы общей ошибки."""
    assert location.hall_phrase(4, "Синяя гостиная") == "зал 4 «Синяя гостиная»"
    assert location.hall_phrase(4, "Синяя гостиная", "prep") == "зале 4 «Синяя гостиная»"
    # Зал без номера — никаких «зал None» (баг-репорт 28.07.2026, п.5).
    assert location.hall_phrase(None, "Вне постоянной экспозиции") == "зал «Вне постоянной экспозиции»"
    assert location.hall_phrase(None, "Вне постоянной экспозиции", "prep") == "зале «Вне постоянной экспозиции»"
    assert location.hall_phrase(12, None) == "зал 12"
    assert location.hall_phrase(None, None) == "зал"
    # Незнакомый падеж — именительный, а не исключение посреди ответа гида.
    assert location.hall_phrase(4, "Синяя гостиная", "dat") == "зал 4 «Синяя гостиная»"


def test_hall_phrase_never_prints_none():
    for hall in HALLS:
        for case in ("nom", "prep"):
            assert "None" not in location.hall_phrase(hall.hall_number, hall.name, case)


def test_showcase_phrase_literals():
    assert location.showcase_phrase(5) == "витрина 5"
    # NULL — это группа «не в витринах», а не потерянный номер.
    assert location.showcase_phrase(None) == "вне витрин"


def test_location_phrase_is_unchanged():
    """Расположение целиком — байт-в-байт прежнее на всех сочетаниях данных."""
    for ex in EXHIBITS:
        assert guide._location_phrase(ex) == _legacy_location_phrase(ex), ex


def test_location_phrase_literals():
    """Прежний (предложный) вид фразы — тот, что уходит в ответ гида."""
    assert guide._location_phrase(EXHIBITS[0]) == "в зале 4 «Синяя гостиная», витрина 5"
    assert guide._location_phrase(EXHIBITS[1]) == "в зале 4 «Синяя гостиная», вне витрин"
    assert guide._location_phrase(EXHIBITS[2]) == "в зале «Вне постоянной экспозиции», витрина 11"
    assert guide._location_phrase(EXHIBITS[4]) == "в зале 12, витрина 7"
    # Витрина-сирота: зала нет — фраза начинается сразу с витрины.
    assert guide._location_phrase(EXHIBITS[6]) == "витрина 5"
    assert guide._location_phrase(EXHIBITS[7]) == "вне витрин"
    # Экспонат не привязан к витрине — сказать нечего, и «вне витрин» тут враньё.
    assert guide._location_phrase(EXHIBITS[8]) == ""


def test_card_variant_is_the_same_phrase():
    """Карточке предмета нужен именительный падеж с заглавной — но текст тот же."""
    assert location.location_phrase(4, "Синяя гостиная", 5, case="nom", capitalize=True) == (
        "Зал 4 «Синяя гостиная», витрина 5"
    )
    assert location.location_phrase(None, "Вне постоянной экспозиции", None, case="nom", capitalize=True) == (
        "Зал «Вне постоянной экспозиции», вне витрин"
    )
    assert location.location_phrase(None, None, 5, case="nom", capitalize=True) == "Витрина 5"
    # Предложный без капитализации — ровно то, что печатает гид.
    assert location.location_phrase(4, "Синяя гостиная", 5, case="prep") == guide._location_phrase(EXHIBITS[0])


def test_has_hall_flag_keeps_degenerate_hall():
    """Зал-пустышка (ни номера, ни названия) — по-прежнему «в зале, …».

    Вывести это из полей нельзя: и «зала нет», и «зал без имени и номера» дают
    одинаковые None. Поэтому у вызывающего с ORM на руках есть явный флаг, и
    прежнее поведение сохраняется (дефект данных чинится в каталоге, а не тут).
    """
    assert location.location_phrase(None, None, 5, case="prep", has_hall=True) == "в зале, витрина 5"
    assert location.location_phrase(None, None, 5, case="prep", has_hall=False) == "витрина 5"
    # По умолчанию — вывод из полей; пустое название считается отсутствующим.
    assert location.location_phrase(None, "", 5, case="prep") == "витрина 5"


def test_guide_answers_are_unchanged():
    """Фразы, которые посетитель видит целиком, — на месте (сторож переноса)."""
    ex = Exhibit(Showcase(5, HALLS[0]), name="Ландыши", exhibit_number="12", short_description="Пасхальное яйцо")
    assert guide._describe_exhibit(ex) == (
        "№12 «Ландыши» — Пасхальное яйцо Найти его можно в зале 4 «Синяя гостиная», витрина 5."
    )
    # Подсказка-уточнение при неуникальном номере: та же фраза с заглавной.
    assert guide._where_hint(ex) == "В зале 4 «Синяя гостиная», витрина 5"
    # Экспонат без витрины — вместо расположения название (иначе пустая подсказка).
    assert guide._where_hint(Exhibit(None, name="Ландыши")) == "Ландыши"


def test_hall_listing_still_uses_the_same_phrase():
    """Список залов (B10) печатает ту же фразу — вынос его не задел."""
    halls = [
        HallForListing(1, "Рыцарский зал"),
        HallForListing(4, "Синяя гостиная"),
        HallForListing(None, "Вне постоянной экспозиции"),
    ]
    assert guide._describe_halls(halls) == (
        "В музее 2 зала. Основная экспозиция: зал 1 «Рыцарский зал»; зал 4 «Синяя гостиная». "
        "Кроме того: Вне постоянной экспозиции."
    )


# ── Имя модуля не должно затеняться локальной переменной ─────────────────────
def test_no_function_in_guide_shadows_an_imported_module():
    """Ни одна функция роутера не объявляет переменную с именем импортированного модуля.

    Ловушка не гипотетическая: модуль расположения сначала импортировали как `location`,
    а в `chat()` под этим же именем лежит поле ответа гида (`sch.GuideLocation`). Python
    считает имя локальным на ВСЮ функцию, поэтому любое обращение к модулю внутри `chat()`
    — даже в строке выше присваивания — упало бы `UnboundLocalError` на КАЖДОМ запросе
    `/guide/chat`. Сейчас модуль зовётся `location_text`, но `chat()` правят постоянно, и
    следующий, кто снова напишет `from ..services import location`, должен узнать об этом
    от теста, а не от посетителя музея.
    """
    import inspect
    import types

    modules = {name for name, value in vars(guide).items() if isinstance(value, types.ModuleType)}
    assert "location_text" in modules, "модуль расположения импортируется под безопасным именем"

    shadowed = {}
    for func_name, func in vars(guide).items():
        if not inspect.isfunction(func):
            continue
        code = getattr(func, "__code__", None)
        clash = modules & set(code.co_varnames if code else ())
        if clash:
            shadowed[func_name] = sorted(clash)
    assert shadowed == {}, f"локальные переменные затеняют модули: {shadowed}"


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
