#!/usr/bin/env python3
"""Приёмочный smoke по баг-репорту заказчика от 31.08.2026 (решение Д10 по релизу).

Что это. Один прогон, который отвечает на вопрос «то, что просил музей, реально
видно снаружи?». Юнит-тесты в tests/ проверяют функции по отдельности и на
фикстурах; здесь проверяется СОБРАННЫЙ стенд: применённая схема, залитые
описания, прогнанные бэкфиллы, поднятый код и настройки окружения. Ровно этим
smoke отличается от pytest: он ловит «код правильный, но флаг выключен /
миграция не прогнана / описание зала не залито», а такого набралось на весь
релиз (см. `docs/bugreport-2026-08-31-answers.md`).

Что проверяется, по пунктам баг-репорта:

  I-1  «Парадная лестница» есть в публичной выдаче, стоит первой в основной
       экспозиции, служебным флагом больше не помечена, у неё есть описание про
       дворец и музей (гейт из решения Д12), а гид считает её наравне с
       остальными и говорит про 12 залов;
  I-2  состав И ПОРЯДОК полей карточки предмета — тот, что музей продиктовал
       дословно; `location` и `maker` присутствуют всегда и являются объектами;
       пустые поля приходят ключами со значением null, а не пропадают;
  I-3  превью описания зала непустое, короче полного, обрывается по границе
       предложения (или по слову с многоточием, но никогда посреди слова), и
       признак «есть что раскрывать» согласован с самим превью;
  II-1 на общем вопросе, где ни один предмет не назван, блок «Упомянуто в
       ответе» ПУСТ; на вопросе про конкретный предмет в блоке именно он; ни
       одна плашка не появляется без упоминания в тексте;
  II-2/3/7/8  подсказки непустые во всех ветках диалога и под рассказом; среди
       них нет формулировок, которые музей просил не предлагать; нет
       перефразировок друг друга; заданный вопрос больше не предлагается;
  IV-1 PUT без поля не затирает изображение и описание, а PUT с явным null —
       затирает (осознанная очистка должна остаться возможной).

Куда ходит. НА СТЕНД. `BASE_URL` обязателен и умолчания не имеет — в отличие от
соседних smoke, где стоит `http://localhost:8000`. Причина простая: этот скрипт
умеет писать в каталог, и «забыл переменную — попал не туда» здесь стоит дороже
удобства. Прод (решение Д11) не трогаем: с ключом `--write` на известный
прод-адрес скрипт не идёт вовсе и говорит об этом прямым текстом.

Что пишет. ПО УМОЛЧАНИЮ — НИЧЕГО, кроме следов обычного посетителя (диалог с
гидом заводит сессию и реплики, ровно как заход с телефона). Проверка IV-1
включается ключом `--write`, выполняется на СПЕЦИАЛЬНО СОЗДАННОЙ карточке в
собственном временном зале и убирает за собой зал, витрину и экспонат. Ни одна
карточка музея при этом не редактируется — испортить правкой ровно то, про
поломку чего написан пункт IV-1, было бы обидно.

Запуск:

    BASE_URL=https://stand.example.ru python scripts/smoke_bugreport_20260831.py
    BASE_URL=https://stand.example.ru ADMIN_TOKEN=… \\
        python scripts/smoke_bugreport_20260831.py --write
    ... --only I-1,I-3          # прогнать часть проверок (когда выкатывают по частям)

Коды возврата: 0 — всё зелено, 1 — есть провалы, 2 — ошибка конфигурации
(не задан BASE_URL, нет токена для `--write`, `--write` на прод, неизвестная
секция в `--only`). Отдельный код у конфигурации нужен, чтобы в CI «скрипт
запустили неправильно» не читалось как «релиз сломан».
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from typing import Callable, Dict, List, Mapping, NamedTuple, Optional, Sequence, Tuple

import httpx

# Стоп-лист запрещённых музеем формулировок и мера «это перефразировка» берутся
# из САМОГО репозитория (app/services/guide_style.py, решение Д1), а не
# переписываются сюда списком. Тавтологии тут нет: модуль описывает, чего музей
# просил не показывать, а smoke спрашивает СТЕНД, что он показывает на самом
# деле, — расхождение бывает и при верном коде (GUIDE_QUESTIONS_FILTER=false,
# выкачен предыдущий образ, подсказки пришли из кэша, набитого до релиза).
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

try:  # pragma: no cover — ветка нужна для копии скрипта, унесённой из репозитория
    from app.services import guide_mentions as _guide_mentions
    from app.services import guide_style as _guide_style
except Exception:  # noqa: BLE001 — причина не важна: важно не падать на импорте
    _guide_mentions = None  # type: ignore[assignment]
    _guide_style = None  # type: ignore[assignment]


# ═════════════════════════════════════════════════════════════════════════════
# Контракт карточки (п. I-2). Порядок дословный, из требования музея.
# ═════════════════════════════════════════════════════════════════════════════
# «название предмета, изображение, расположение (название и номер зала, номер
# витрины), год создания, фирма и мастер, материалы, техники, описание».
#
# Списки продублированы здесь СОЗНАТЕЛЬНО, а не считаны из `app.schemas`: smoke
# проверяет ответ чужого (задеплоенного) кода, и сверка «схема сама с собой»
# не поймала бы ни выкаченный старый образ, ни ручную правку в обход схемы.
# От расхождения с текущей схемой страхует tests/test_smoke_bugreport_20260831.py:
# там эти же кортежи сверяются с `app.schemas`, и перестановка поля в схеме
# роняет тест, а не тихо расходится со smoke.
EXPECTED_CARD_FIELDS: Tuple[str, ...] = (
    "id", "exhibit_number", "label_slug", "name",
    "image_url", "images",
    "location", "hall", "showcase",
    "year_created", "origin_place",
    "maker", "master_name",
    "material", "techniques", "short_description",
    "video_url", "model_3d_url", "model_3d_embed", "audio_url", "source_url",
)
EXPECTED_SUMMARY_FIELDS: Tuple[str, ...] = (
    "id", "exhibit_number", "label_slug", "name", "thumbnail_url",
    "location", "hall_id", "showcase_id", "showcase_number",
    "year_created", "maker", "master_name", "is_temporary",
)
EXPECTED_LOCATION_FIELDS: Tuple[str, ...] = (
    "hall_id", "hall_number", "hall_name",
    "showcase_id", "showcase_number", "showcase_name",
    "text", "text_in",
)
EXPECTED_MAKER_FIELDS: Tuple[str, ...] = ("text", "firm", "master")

# Поля, которые музей потерял 31.08.2026, правя техники у яйца «Ренессанс»
# (п. IV-1), и поля, которых на скриншоте не хватало (п. I-2). Их присутствие
# КЛЮЧОМ (пусть и с null) — это и есть обещанная «прогнозируемость» карточки.
PREDICTABLE_FIELDS: Tuple[str, ...] = (
    "image_url", "short_description", "material", "techniques",
    "year_created", "origin_place", "location", "maker",
)

STAIRCASE = "Парадная лестница"
# Столько залов у музея после отмены решения прятать лестницу (docs/staircase-hall-decision.md,
# раздел «Отменено 31.08.2026»): 12 пронумерованных, как в путеводителе 2014 года.
# На демо-сиде db/seed.sql залов меньше, и проверка там честно пропускается, а не
# подгоняется под цифру.
EXPECTED_HALL_COUNT = 12

# Известный адрес прода. Взят из docs/bugreport-2026-08-12-answers.md (раздел
# «как проверить»); нужен ровно для одного — не дать `--write` уйти на прод.
PRODUCTION_HOSTS: Tuple[str, ...] = ("d5dhcivtos7rfvdfdpg2.xxg4zr82.apigw.yandexcloud.net",)

# Секции проверок для ключа --only. Названы по пунктам баг-репорта, чтобы во
# время поэтапной выкатки можно было прогнать ровно то, что уже уехало.
SECTIONS: Tuple[str, ...] = ("I-1", "I-2", "I-3", "II-1", "II-hints", "IV-1")


class ConfigError(RuntimeError):
    """Скрипт запустили неправильно. Отличается от провала проверки кодом возврата."""


class Check(NamedTuple):
    """Одна строка отчёта.

    ``ok=None`` — ПРОПУСК: проверять было нечего (демо-сид без нужных данных,
    ветка B9 вместо подсказок). Пропуск печатается отдельно и НЕ считается
    успехом: «нет данных» и «работает» — разные новости для приёмки.
    """

    name: str
    ok: Optional[bool]
    detail: str = ""


class QuestionRules(NamedTuple):
    """Правила, по которым судим подсказки (пп. II-2, II-4, II-8).

    Обёрнуты в объект, чтобы проверки оставались чистыми функциями и
    тестировались без импорта живого `guide_style`: тест подсовывает свои
    правила и смотрит, что проверка на них скажет.

    ``strict=False`` означает, что модуль репозитория недоступен и работает
    ослабленный запасной вариант — smoke об этом честно печатает, а не делает
    вид, что проверил то же самое.
    """

    is_meaningless: Callable[[str], bool]
    dedupe: Callable[[Sequence[str], Sequence[str]], List[str]]
    strict: bool


# Три вопроса со скриншота п. II-8 — дословно. Запасной вариант на случай, когда
# `app.services.guide_style` не импортировался: он ловит только эти формулировки
# буква в букву, но именно их музей и привёл как пример «подобные вопросы не нужны».
FALLBACK_BANNED: Tuple[str, ...] = (
    "сколько времени заняло создание этого предмета?",
    "какие уникальные особенности есть у этой работы михаила перхина?",
    "почему для украшения была выбрана именно желтая гильоше-эмаль?",
)


def _fallback_is_meaningless(question: str) -> bool:
    low = (question or "").strip().lower().replace("ё", "е")
    return low in FALLBACK_BANNED


def _fallback_dedupe(questions: Sequence[str], exclude: Sequence[str] = ()) -> List[str]:
    """Запасная дедупликация: только точное совпадение без регистра и пунктуации.

    Перефразировки («какие именно скифские мотивы …» против «какие именно
    скифские мотивы Эрик Коллин …») она НЕ ловит — на то и `strict=False`.
    """
    seen = {_flatten(item) for item in exclude if _flatten(item)}
    result: List[str] = []
    for item in questions:
        key = _flatten(item)
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def _flatten(text: Optional[str]) -> str:
    """Грубая нормализация для сравнения строк: регистр, ё, пунктуация, пробелы."""
    low = (text or "").lower().replace("ё", "е")
    return re.sub(r"\s+", " ", re.sub(r"[^0-9a-zа-я ]+", " ", low)).strip()


def default_rules() -> QuestionRules:
    """Правила из репозитория, если он рядом; иначе — ослабленный запасной набор."""
    if _guide_style is None:
        return QuestionRules(_fallback_is_meaningless, _fallback_dedupe, strict=False)
    return QuestionRules(
        _guide_style.is_meaningless_question,
        lambda questions, exclude=(): _guide_style.dedupe_questions(questions, exclude=exclude),
        strict=True,
    )


# ═════════════════════════════════════════════════════════════════════════════
# Разбор аргументов и окружения
# ═════════════════════════════════════════════════════════════════════════════
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--base-url", help="адрес стенда (по умолчанию — переменная BASE_URL)")
    parser.add_argument("--admin-token",
                        help="токен админки (по умолчанию — ADMIN_TOKEN или ADMIN_API_TOKEN); нужен только с --write")
    parser.add_argument("--write", action="store_true",
                        help="выполнить проверку IV-1: создать временную карточку, поправить её PUT-ом и удалить")
    parser.add_argument("--only", help="прогнать только эти секции: " + ",".join(SECTIONS))
    parser.add_argument("--timeout", type=float, default=60.0,
                        help="таймаут HTTP-запроса, секунд (умолчание 60: рассказ гида генерируется небыстро)")
    return parser


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    return build_parser().parse_args(argv)


def resolve_base_url(env: Mapping[str, str], cli: Optional[str] = None) -> str:
    """Адрес стенда. Умолчания нет — см. докстринг модуля.

    Схему требуем явно: httpx с голым хостом («stand.example.ru») соберёт
    относительный путь и запросы уедут в никуда с невнятной ошибкой.
    """
    value = (cli or env.get("BASE_URL") or "").strip()
    if not value:
        raise ConfigError(
            "Не задан BASE_URL — адрес СТЕНДА. Умолчания у этого smoke нет намеренно: "
            "он умеет писать в каталог, и промах адресом дороже удобства.\n"
            "  BASE_URL=https://stand.example.ru python scripts/smoke_bugreport_20260831.py"
        )
    if not value.startswith(("http://", "https://")):
        raise ConfigError(f"BASE_URL должен начинаться с http:// или https://, получено: {value!r}")
    return value.rstrip("/")


def resolve_admin_token(env: Mapping[str, str], cli: Optional[str] = None) -> str:
    """Токен админки. Имя переменной принимаем оба — они разошлись исторически.

    Скрипты каталога (publish_staircase_hall, restore_wiped_cards) читают
    `ADMIN_TOKEN`, соседние smoke — `ADMIN_API_TOKEN`. Требовать «правильное»
    имя от того, кто прогоняет релиз, не за что.
    """
    return (cli or env.get("ADMIN_TOKEN") or env.get("ADMIN_API_TOKEN") or "").strip()


def looks_like_production(base_url: str) -> bool:
    """Это известный адрес прода?

    Сравниваем по хосту, а не по вхождению подстроки: `apigw.yandexcloud.net`
    сам по себе ничего не значит — стенд живёт за таким же гейтвеем.
    """
    host = re.sub(r"^https?://", "", base_url).split("/")[0].split("@")[-1].split(":")[0].lower()
    return host in PRODUCTION_HOSTS


def select_sections(only: Optional[str]) -> Tuple[str, ...]:
    """Разбор `--only`. Неизвестная секция — ошибка конфигурации, а не тихий пропуск."""
    if not only or not only.strip():
        return SECTIONS
    wanted = [part.strip() for part in only.split(",") if part.strip()]
    unknown = [item for item in wanted if item not in SECTIONS]
    if unknown:
        raise ConfigError(
            f"Неизвестные секции в --only: {', '.join(unknown)}. Доступные: {', '.join(SECTIONS)}"
        )
    # Порядок держим канонический (как в SECTIONS), а не как перечислил вызывающий:
    # отчёт должен читаться сверху вниз по пунктам баг-репорта.
    return tuple(section for section in SECTIONS if section in wanted)


# ═════════════════════════════════════════════════════════════════════════════
# I-1. «Парадная лестница» — первая в основной экспозиции
# ═════════════════════════════════════════════════════════════════════════════
def is_staircase(hall: Mapping[str, object]) -> bool:
    return str(hall.get("name") or "").strip().lower() == STAIRCASE.lower()


def find_staircase(halls: Sequence[Mapping[str, object]]) -> Optional[Mapping[str, object]]:
    for hall in halls:
        if is_staircase(hall):
            return hall
    return None


def check_staircase_first(public_halls: Sequence[Mapping[str, object]]) -> List[Check]:
    """I-1: зал есть в публичной выдаче, стоит первым и с описанием про музей.

    Проверяем ИМЕННО публичную выдачу `GET /halls`: до релиза зал существовал, но
    был скрыт `is_service = true`, и «зал в базе есть» ничего не доказывает —
    музей смотрит на список в приложении.

    Описание сверяется не на «непустое», а на упоминание дворца и музея: старый
    текст зала — архитектурная справка про перила и купол, формально непустая, и
    гейт из решения Д12 («не снимать флаг, пока описание не обновлено») без этой
    проверки закрывался бы сам собой.
    """
    staircase = find_staircase(public_halls)
    if staircase is None:
        # Отличаем «зал не выкачен» от «на стенде демо-сид». Демо-сид db/seed.sql
        # такого зала не содержит вовсе, и залов в нём заведомо меньше десятка.
        if len(public_halls) < EXPECTED_HALL_COUNT:
            return [Check("I-1 «Парадная лестница» в публичной выдаче", None,
                          f"на стенде {len(public_halls)} залов — похоже на демо-сид, проверять нечего")]
        return [Check("I-1 «Парадная лестница» в публичной выдаче", False,
                      "зала нет в GET /halls: прогнан ли scripts/publish_staircase_hall.py --apply?")]

    checks = [Check("I-1 «Парадная лестница» в публичной выдаче", True)]
    checks.append(Check(
        "I-1 у «Парадной лестницы» снят служебный флаг",
        staircase.get("is_service") is False,
        f"is_service={staircase.get('is_service')!r}",
    ))
    numbered = [hall for hall in public_halls if hall.get("hall_number") is not None]
    checks.append(Check(
        "I-1 «Парадная лестница» первая среди пронумерованных залов",
        bool(numbered) and is_staircase(numbered[0]),
        "порядок: " + ", ".join(str(hall.get("name")) for hall in public_halls[:4]),
    ))
    checks.append(Check(
        "I-1 у «Парадной лестницы» публичный номер 1",
        staircase.get("hall_number") == 1,
        f"hall_number={staircase.get('hall_number')!r}",
    ))
    description = str(staircase.get("description") or "").lower().replace("ё", "е")
    checks.append(Check(
        "I-1 описание зала рассказывает о дворце и музее (гейт Д12)",
        bool(description) and ("дворц" in description or "дворец" in description) and "музе" in description,
        f"описание: {description[:120]!r}",
    ))
    return checks


def hall_count_in_answer(answer: str) -> Optional[int]:
    """Число залов, которое гид назвал в ответе: «В музее 12 залов.» → 12."""
    match = re.search(r"[Вв]\s+музее\s+(\d+)\s+зал", answer or "")
    return int(match.group(1)) if match else None


def check_guide_hall_count(answer: str, public_halls: Sequence[Mapping[str, object]]) -> List[Check]:
    """I-1: гид считает лестницу наравне с остальными и говорит «12 залов».

    Две проверки, и они про разное. Первая — на согласованность: сколько
    пронумерованных залов отдаёт каталог, столько гид и обязан назвать, и она
    верна на любом стенде, включая демо-сид. Вторая — на цифру из требования
    музея; она осмысленна только на настоящем каталоге, поэтому на неполном
    каталоге ЧЕСТНО ПРОПУСКАЕТСЯ, а не подгоняется.
    """
    said = hall_count_in_answer(answer)
    numbered = [hall for hall in public_halls if hall.get("hall_number") is not None]
    checks = [Check(
        "I-1 счётчик гида сходится с числом пронумерованных залов",
        said is not None and said == len(numbered),
        f"гид сказал {said}, в каталоге {len(numbered)}; ответ: {(answer or '')[:120]!r}",
    )]
    if find_staircase(public_halls) is None:
        checks.append(Check(f"I-1 гид говорит про {EXPECTED_HALL_COUNT} залов", None,
                            "каталог неполный (нет «Парадной лестницы») — цифра из требования тут не проверяется"))
    else:
        checks.append(Check(
            f"I-1 гид говорит про {EXPECTED_HALL_COUNT} залов",
            said == EXPECTED_HALL_COUNT,
            f"гид сказал {said}",
        ))
    return checks


# ═════════════════════════════════════════════════════════════════════════════
# I-2. Карточка предмета: состав и порядок полей
# ═════════════════════════════════════════════════════════════════════════════
def order_detail(actual: Sequence[str], expected: Sequence[str]) -> str:
    """Человекочитаемое расхождение состава/порядка ключей.

    Три причины провала лечатся по-разному, поэтому и называются раздельно:
    поля не хватает (не выкачена схема), поле лишнее (выкачена более новая),
    поля переставлены (правка схемы переставила контракт).
    """
    missing = [name for name in expected if name not in actual]
    extra = [name for name in actual if name not in expected]
    parts: List[str] = []
    if missing:
        parts.append("нет полей: " + ", ".join(missing))
    if extra:
        parts.append("лишние поля: " + ", ".join(extra))
    if not missing and not extra and list(actual) != list(expected):
        parts.append("порядок: " + " ".join(actual) + " | ожидался: " + " ".join(expected))
    return "; ".join(parts)


def check_card_contract(card: Mapping[str, object], where: str) -> List[Check]:
    """I-2: состав, порядок и прогнозируемость карточки.

    Порядок ключей в JSON — часть контракта, а не украшение: музей просил, чтобы
    расположение и дата стояли НАД названием, и фронту проще класть поля в том
    порядке, в котором они пришли. `json.loads` сохраняет порядок ключей, поэтому
    `list(card)` — это буквально порядок в теле ответа.
    """
    checks = [Check(
        f"I-2 {where}: состав и порядок полей — как просил музей",
        list(card) == list(EXPECTED_CARD_FIELDS),
        order_detail(list(card), EXPECTED_CARD_FIELDS),
    )]
    checks.extend(_check_location_and_maker(card, where))
    absent = [field for field in PREDICTABLE_FIELDS if field not in card]
    checks.append(Check(
        f"I-2 {where}: пустые поля приходят ключами, а не пропадают",
        not absent,
        "пропали ключи: " + ", ".join(absent) if absent else "",
    ))
    location = card.get("location")
    if isinstance(location, dict) and location.get("showcase_id") is not None:
        # Расположение обещано ГОТОВОЙ строкой: фронт не должен склеивать фразу
        # из трёх ручек, а гид — печатать её по-своему.
        checks.append(Check(
            f"I-2 {where}: расположение пришло готовой строкой",
            bool(str(location.get("text") or "").strip()) and bool(str(location.get("text_in") or "").strip()),
            f"text={location.get('text')!r}, text_in={location.get('text_in')!r}",
        ))
    else:
        checks.append(Check(f"I-2 {where}: расположение пришло готовой строкой", None,
                            "предмет не привязан к витрине — строку собирать не из чего"))
    return checks


def check_summary_contract(item: Mapping[str, object], where: str) -> List[Check]:
    """I-2: тот же порядок полей в списках — плашка «зал/витрина» рисуется одинаково."""
    checks = [Check(
        f"I-2 {where}: состав и порядок полей краткой карточки",
        list(item) == list(EXPECTED_SUMMARY_FIELDS),
        order_detail(list(item), EXPECTED_SUMMARY_FIELDS),
    )]
    checks.extend(_check_location_and_maker(item, where))
    return checks


def _check_location_and_maker(card: Mapping[str, object], where: str) -> List[Check]:
    """`location` и `maker` — ВСЕГДА объекты, даже когда внутри всё пусто.

    Именно это музей называл прогнозируемостью: пустеть они обязаны внутрь, а не
    исчезновением ключа, иначе фронт пишет три уровня проверок.
    """
    location = card.get("location")
    maker = card.get("maker")
    return [
        Check(
            f"I-2 {where}: location — объект с полным набором полей",
            isinstance(location, dict) and list(location) == list(EXPECTED_LOCATION_FIELDS),
            order_detail(list(location), EXPECTED_LOCATION_FIELDS) if isinstance(location, dict)
            else f"location={location!r}",
        ),
        Check(
            f"I-2 {where}: maker — объект с полным набором полей",
            isinstance(maker, dict) and list(maker) == list(EXPECTED_MAKER_FIELDS),
            order_detail(list(maker), EXPECTED_MAKER_FIELDS) if isinstance(maker, dict) else f"maker={maker!r}",
        ),
    ]


# ═════════════════════════════════════════════════════════════════════════════
# I-3. Превью описания зала
# ═════════════════════════════════════════════════════════════════════════════
# Чем закончилось превью. Разбор нужен потому, что «обрыв по границе
# предложения» — требование не абсолютное: первая фраза описания бывает длиннее
# лимита, и тогда обрезка честно режет по слову и ставит многоточие. Недопустим
# ровно один исход — обрыв ПОСРЕДИ СЛОВА, его и ловим отдельно от остальных.
BREAK_FULL = "полное"        # ничего не скрыто, превью равно описанию
BREAK_SENTENCE = "предложение"
BREAK_WORD = "слово+…"
BREAK_BROKEN = "посреди слова"
BREAK_ALIEN = "не начало описания"

_SENTENCE_END = ".!?;"


def preview_break_kind(preview: str, full: str) -> str:
    """Как оборвано превью относительно полного описания."""
    preview = (preview or "").strip()
    full = (full or "").strip()
    if preview == full:
        return BREAK_FULL
    head = preview[:-1] if preview.endswith("…") else preview
    if not head or not full.startswith(head):
        # Превью — не начало описания: значит, это не обрезка, а другой текст.
        return BREAK_ALIEN
    if preview.endswith("…"):
        rest = full[len(head):]
        # Резать по слову можно только там, где слово кончилось: следующий символ
        # описания не должен быть буквой или цифрой. Пробела мало: обрезка сама
        # снимает с хвоста « ,;:—-», и после «слово,» в описании остаётся запятая,
        # а не пробел, — такое превью оборвано корректно.
        return BREAK_WORD if (not rest or not rest[0].isalnum()) else BREAK_BROKEN
    if preview[-1] in _SENTENCE_END:
        return BREAK_SENTENCE
    return BREAK_BROKEN


def check_hall_preview(hall: Mapping[str, object], where: str) -> List[Check]:
    """I-3: превью непустое, короче полного, обрывается по границе, признак согласован."""
    full = str(hall.get("description") or "")
    preview = hall.get("description_preview")
    has_more = hall.get("description_has_more")
    if not full.strip():
        return [Check(f"I-3 {where}: превью описания зала", None, "у зала нет описания — резать нечего")]

    checks = [Check(
        f"I-3 {where}: превью непустое",
        isinstance(preview, str) and bool(preview.strip()),
        f"description_preview={preview!r}",
    )]
    if not isinstance(preview, str):
        return checks

    kind = preview_break_kind(preview, full)
    checks.append(Check(
        f"I-3 {where}: превью короче полного описания",
        len(preview) < len(full.strip()),
        f"{len(preview)} против {len(full.strip())} знаков (обрыв: {kind})",
    ))
    checks.append(Check(
        f"I-3 {where}: превью обрывается по границе, а не посреди слова",
        kind in (BREAK_SENTENCE, BREAK_WORD),
        f"обрыв: {kind}; хвост превью: {preview[-40:]!r}",
    ))
    # Признак «есть что раскрывать» обязан описывать ЭТО превью, а не соседнее:
    # рассогласование даёт кнопку «Подробнее», которая ничего не открывает, —
    # ровно ту болячку, от которой музей и просил избавиться.
    checks.append(Check(
        f"I-3 {where}: признак «есть что раскрывать» согласован с превью",
        isinstance(has_more, bool) and has_more == (preview.strip() != full.strip()),
        f"description_has_more={has_more!r}, превью "
        + ("равно описанию" if preview.strip() == full.strip() else "короче описания"),
    ))
    return checks


def pick_hall_with_long_description(
    halls: Sequence[Mapping[str, object]]
) -> Optional[Mapping[str, object]]:
    """Зал, на котором превью вообще имеет смысл: у которого есть что скрывать."""
    candidates = [hall for hall in halls if hall.get("description_has_more") is True]
    if candidates:
        # Самое длинное описание — самый показательный разрез.
        return max(candidates, key=lambda hall: len(str(hall.get("description") or "")))
    with_text = [hall for hall in halls if str(hall.get("description") or "").strip()]
    return max(with_text, key=lambda hall: len(str(hall.get("description") or ""))) if with_text else None


# ═════════════════════════════════════════════════════════════════════════════
# II-1. Блок «Упомянуто в ответе»
# ═════════════════════════════════════════════════════════════════════════════
def plaque_names(body: Mapping[str, object]) -> List[str]:
    plaques = body.get("referenced_exhibits") or []
    return [str(plaque.get("name") or "") for plaque in plaques if isinstance(plaque, dict)]


def check_referenced_empty(body: Mapping[str, object], question: str) -> List[Check]:
    """II-1: на общем вопросе блок пуст.

    Дословная жалоба музея: «упоминаются предметы, которые не упомянуты. Нужен ли
    этот раздел вообще в таком случае?». Ответ релиза — раздел нужен, но пустой
    блок фронт не рисует; значит на вопросе, где ни один предмет не назван,
    сервер обязан вернуть именно пустой список.
    """
    plaques = body.get("referenced_exhibits")
    names = plaque_names(body)
    checks = [Check(
        "II-1 общий вопрос: блок «Упомянуто в ответе» пуст",
        isinstance(plaques, list) and not plaques,
        f"вопрос {question!r}, в блоке: {', '.join(names) or '—'}; ответ: {str(body.get('answer'))[:160]!r}",
    )]
    checks.append(Check(
        "II-1 блок всегда список, а не null",
        isinstance(plaques, list),
        f"referenced_exhibits={plaques!r}",
    ))
    return checks


def check_referenced_named(
    body: Mapping[str, object], exhibit_id: int, name: str, searchable: Optional[bool] = None
) -> List[Check]:
    """II-1: на вопросе про конкретный предмет в блоке именно он."""
    plaques = [p for p in (body.get("referenced_exhibits") or []) if isinstance(p, dict)]
    ids = [p.get("id") for p in plaques]
    hint = "" if searchable is None else (
        "" if searchable else "; при этом и GET /search его не находит — дело в поиске, а не в проверке упоминания"
    )
    checks = [Check(
        "II-1 вопрос про предмет: он и стоит в блоке",
        exhibit_id in ids,
        f"спрашивали про {name!r} (id={exhibit_id}), в блоке: {', '.join(plaque_names(body)) or '—'}{hint}",
    )]
    strangers = [p for p in plaques if p.get("id") != exhibit_id]
    checks.append(Check(
        "II-1 в блоке нет посторонних предметов",
        not strangers,
        "лишние плашки: " + ", ".join(str(p.get("name")) for p in strangers) if strangers else "",
    ))
    return checks


def check_plaques_are_really_mentioned(body: Mapping[str, object], question: str) -> List[Check]:
    """II-1: ни одна плашка не появилась без упоминания в тексте.

    Проверяем НЕ повторением серверной логики, а её же кодом
    (`app/services/guide_mentions.py`) — это единственный способ спросить стенд
    «а этот предмет вообще назван?» независимо от того, какой образ на нём
    выкачен. Плашки с пометкой `context` пропускаем: контекстный экспонат стоит
    в блоке по другому основанию (посетитель смотрит именно на него), и упоминания
    от него не требуется. Плашки без пометки приходят из детерминированных веток
    (поиск по номеру, список залов) — их тоже не судим упоминанием.
    """
    plaques = [p for p in (body.get("referenced_exhibits") or []) if isinstance(p, dict)]
    judged = [p for p in plaques if p.get("mentioned_in") in ("answer", "question")]
    unlabelled = [p for p in plaques if p.get("mentioned_in") is None]
    checks: List[Check] = []
    if _guide_mentions is None:
        checks.append(Check("II-1 плашки подтверждены упоминанием в тексте", None,
                            "app/services/guide_mentions.py недоступен — проверять нечем"))
    elif not judged:
        checks.append(Check("II-1 плашки подтверждены упоминанием в тексте", None,
                            "в блоке нет плашек, построенных проверкой упоминания"))
    else:
        answer = str(body.get("answer") or "")
        cards = [(str(p.get("name") or ""), p.get("exhibit_number")) for p in judged]
        mentioned = {m.index for m in _guide_mentions.mentioned_in_dialogue(answer, question, cards)}
        ghosts = [cards[i][0] for i in range(len(cards)) if i not in mentioned]
        checks.append(Check(
            "II-1 плашки подтверждены упоминанием в тексте",
            not ghosts,
            "не названы ни в ответе, ни в вопросе: " + ", ".join(ghosts) if ghosts else "",
        ))
    if unlabelled:
        # Пустой `mentioned_in` в обычном диалоге означает, что стенд собрал блок
        # старым способом — первыми результатами поиска без порога.
        checks.append(Check(
            "II-1 плашки помечены источником (mentioned_in)",
            False,
            f"без пометки: {', '.join(str(p.get('name')) for p in unlabelled)} — "
            "проверьте GUIDE_REFERENCED_REQUIRE_MENTION на стенде",
        ))
    return checks


def pick_exhibit_with_proper_name(
    items: Sequence[Mapping[str, object]]
) -> Optional[Mapping[str, object]]:
    """Предмет с собственным именем в кавычках — на нём проверка упоминания честнее всего.

    «Пасхальное яйцо „Ландыши“» узнаётся по имени в кавычках, а «Ваза» или
    «Портрет» — слова из любого текста; на таких названиях проверка упоминания
    ничего бы не доказала.
    """
    for item in items:
        name = str(item.get("name") or "")
        match = re.search(r"[«„\"]([^»“\"]{4,})[»“\"]", name)
        if match:
            return item
    return None


# ═════════════════════════════════════════════════════════════════════════════
# II-2/3/7/8. Подсказки
# ═════════════════════════════════════════════════════════════════════════════
def check_suggestions(questions: object, where: str, rules: QuestionRules) -> List[Check]:
    """Подсказки: непустые, без запрещённых формулировок, без перефразировок.

    Три жалобы музея закрываются тремя проверками, и ни одна не выводится из
    другой: пустой блок — это п. II-7 («после ответа варианты уже не
    предлагаются»), запрещённая формулировка — пп. II-4/II-8 («подобные вопросы
    не нужны»), перефразировка — п. II-2 («постоянно перефразирует»).
    """
    if not isinstance(questions, list):
        return [Check(f"II-7 {where}: подсказки непустые", False, f"suggested_questions={questions!r}")]

    texts = [str(item) for item in questions]
    checks = [Check(
        f"II-7 {where}: подсказки непустые",
        bool(texts) and all(text.strip() for text in texts),
        f"suggested_questions={texts!r}",
    )]
    banned = [text for text in texts if rules.is_meaningless(text)]
    checks.append(Check(
        f"II-4/II-8 {where}: нет формулировок, которые музей просил не предлагать",
        not banned,
        "запрещённые: " + " | ".join(banned) if banned else "",
    ))
    kept = rules.dedupe(texts, ())
    checks.append(Check(
        f"II-2 {where}: подсказки не перефразируют друг друга",
        len(kept) == len(texts),
        "склеились: " + " | ".join(text for text in texts if text not in kept) if len(kept) != len(texts) else "",
    ))
    return checks


def check_asked_question_is_gone(
    questions: object, asked: str, where: str, rules: QuestionRules
) -> List[Check]:
    """II-2/II-3: только что заданный вопрос не предлагается снова.

    На скриншотах музея это выглядит так: посетитель спросил про скифские мотивы
    в браслете, получил ответ — и следующей подсказкой ему предлагают тот же
    вопрос другими словами. Сравниваем не строками, а той же мерой похожести,
    какой склеиваются подсказки между собой: точное сравнение перефразировку не
    поймает, а музей жаловался именно на неё.
    """
    if not isinstance(questions, list) or not questions:
        return [Check(f"II-2 {where}: заданный вопрос больше не предлагается", None,
                      "подсказок нет — сравнивать не с чем")]
    texts = [str(item) for item in questions]
    kept = rules.dedupe(texts, (asked,))
    dropped = [text for text in texts if text not in kept]
    return [Check(
        f"II-2 {where}: заданный вопрос больше не предлагается",
        not dropped,
        f"спрашивали {asked!r}, снова предлагают: " + " | ".join(dropped) if dropped else "",
    )]


def is_ambiguous_number_reply(body: Mapping[str, object]) -> bool:
    """Уточняющий диалог B9: в `suggested_questions` лежат не вопросы, а варианты витрин.

    Единственная ветка контракта, где судить это поле правилами подсказок нельзя.
    """
    return "встречается в нескольких витринах" in str(body.get("answer") or "")


# ═════════════════════════════════════════════════════════════════════════════
# IV-1. PUT карточки не затирает непереданные поля
# ═════════════════════════════════════════════════════════════════════════════
def check_put_kept(
    before: Mapping[str, object], after: Mapping[str, object], changed: Mapping[str, object]
) -> List[Check]:
    """IV-1: поля, которых не было в теле PUT, остались как были.

    Дословно: «Попробовали внести информацию по техникам, после чего из карточки
    пропало изображение и описание». Проверяем ровно этот сценарий: PUT с
    обязательными полями и одними техниками.
    """
    kept = [field for field in ("image_url", "short_description", "material", "year_created", "master_name")
            if before.get(field) is not None]
    wiped = [field for field in kept if after.get(field) != before.get(field)]
    checks = [Check(
        "IV-1 PUT без поля не затирает изображение, описание и материалы",
        not wiped,
        "затёрты: " + ", ".join(f"{field}: {before.get(field)!r} → {after.get(field)!r}" for field in wiped)
        if wiped else f"сохранены: {', '.join(kept)}",
    )]
    # Обратная ошибка тоже возможна и тоже ломает админку: если «не затирать
    # непереданное» реализовать слишком широко, PUT перестанет записывать и то,
    # что прислали. Поэтому сразу же проверяем, что правка доехала.
    not_applied = [field for field, value in changed.items() if after.get(field) != value]
    checks.append(Check(
        "IV-1 переданное поле всё-таки записалось",
        not not_applied,
        "не записались: " + ", ".join(f"{field}={after.get(field)!r}" for field in not_applied)
        if not_applied else "",
    ))
    return checks


def check_put_cleared(after: Mapping[str, object], fields: Sequence[str]) -> List[Check]:
    """IV-1: явный null по-прежнему стирает поле.

    Обратная сторона того же контракта. Без этой проверки «починка» пункта IV-1
    легко превращается в противоположный дефект: карточку становится нельзя
    очистить вовсе, и админка молча игнорирует осознанную правку.
    """
    left = [field for field in fields if after.get(field) is not None]
    return [Check(
        "IV-1 PUT с явным null очищает поле",
        not left,
        "не очистились: " + ", ".join(f"{field}={after.get(field)!r}" for field in left) if left else "",
    )]


# ═════════════════════════════════════════════════════════════════════════════
# Отчёт
# ═════════════════════════════════════════════════════════════════════════════
class Report:
    """Копилка результатов. Печатает строки по мере поступления, как соседние smoke."""

    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0
        self.skipped = 0
        self.failures: List[Check] = []

    def add(self, checks: Sequence[Check]) -> None:
        for check in checks:
            if check.ok is None:
                self.skipped += 1
                print(f"SKIP {check.name}" + (f" — {check.detail}" if check.detail else ""))
            elif check.ok:
                self.passed += 1
                print(f"PASS {check.name}")
            else:
                self.failed += 1
                self.failures.append(check)
                print(f"FAIL {check.name}" + (f" — {check.detail}" if check.detail else ""))

    def summary(self) -> str:
        return f"пройдено: {self.passed}, провалено: {self.failed}, пропущено: {self.skipped}"


# ═════════════════════════════════════════════════════════════════════════════
# Сетевая часть
# ═════════════════════════════════════════════════════════════════════════════
def _chat(client: httpx.Client, **payload) -> Dict[str, object]:
    response = client.post("/guide/chat", json=payload)
    response.raise_for_status()
    return response.json()


def run_i1(client: httpx.Client, report: Report, public_halls: List[Dict[str, object]]) -> None:
    report.add(check_staircase_first(public_halls))
    body = _chat(client, message="какие есть залы")
    report.add(check_guide_hall_count(str(body.get("answer") or ""), public_halls))


def run_i2(client: httpx.Client, report: Report, exhibits: List[Dict[str, object]]) -> None:
    if not exhibits:
        report.add([Check("I-2 карточка предмета", None, "в каталоге стенда нет экспонатов")])
        return
    report.add(check_summary_contract(exhibits[0], "GET /exhibits"))
    # Карточку берём ту же, что и в списке: расхождение между списком и карточкой
    # музей увидит первым — он смотрит на них подряд.
    card = client.get(f"/exhibits/{exhibits[0]['id']}")
    card.raise_for_status()
    report.add(check_card_contract(card.json(), "GET /exhibits/{id}"))


def run_i3(client: httpx.Client, report: Report, public_halls: List[Dict[str, object]]) -> None:
    hall = pick_hall_with_long_description(public_halls)
    if hall is None:
        report.add([Check("I-3 превью описания зала", None, "ни у одного зала нет описания")])
        return
    where = f"GET /halls (зал «{hall.get('name')}»)"
    report.add(check_hall_preview(hall, where))
    detail = client.get(f"/halls/{hall['id']}")
    detail.raise_for_status()
    report.add(check_hall_preview(detail.json(), "GET /halls/{id}"))


def run_ii1(client: httpx.Client, report: Report, exhibits: List[Dict[str, object]]) -> None:
    # Вопрос заведомо «ни про какой предмет»: ни названия, ни номера. Именно на
    # таком блок и добивался до четырёх чужими карточками.
    general = "Расскажите, пожалуйста, об истории самого музея и о том, как он появился"
    body = _chat(client, message=general)
    report.add(check_referenced_empty(body, general))
    report.add(check_plaques_are_really_mentioned(body, general))

    target = pick_exhibit_with_proper_name(exhibits)
    if target is None:
        report.add([Check("II-1 вопрос про предмет: он и стоит в блоке", None,
                          "в каталоге стенда нет предмета с именем в кавычках")])
        return
    name = str(target["name"])
    question = f"Расскажите про {name}"
    # Заранее спрашиваем поиск: если и он предмет не находит, дело не в проверке
    # упоминания, и провал нужно чинить не там. Диагностика уезжает в detail.
    found = client.get("/search", params={"q": name})
    searchable = None
    if found.status_code == 200:
        searchable = any(item.get("id") == target["id"] for item in found.json().get("exhibits", []))
    body = _chat(client, message=question)
    report.add(check_referenced_named(body, int(target["id"]), name, searchable))
    report.add(check_plaques_are_really_mentioned(body, question))


def run_hints(
    client: httpx.Client,
    report: Report,
    rules: QuestionRules,
    public_halls: List[Dict[str, object]],
    exhibits: List[Dict[str, object]],
) -> None:
    """Подсказки во ВСЕХ ветках, где они гарантированно пустели (решение Д7).

    Веток пять, и проверять их надо порознь: пустой блок был не общим сбоем, а
    свойством конкретных веток — общий чат без контекста, контекст зала, список
    залов, поиск по номеру и экран рассказа.
    """
    report.add(check_suggestions(_chat(client, message="Что интересного есть в музее?").get("suggested_questions"),
                                 "общий чат без контекста", rules))
    report.add(check_suggestions(_chat(client, message="какие есть залы").get("suggested_questions"),
                                 "список залов", rules))

    numbered = [hall for hall in public_halls if hall.get("hall_number") is not None]
    if numbered:
        body = _chat(client, message="Что тут можно посмотреть?", context={"hall_id": numbered[0]["id"]})
        report.add(check_suggestions(body.get("suggested_questions"), "контекст зала", rules))
    else:
        report.add([Check("II-7 контекст зала: подсказки непустые", None, "на стенде нет пронумерованных залов")])

    numbered_exhibits = [item for item in exhibits if str(item.get("exhibit_number") or "").strip().isdigit()]
    if numbered_exhibits:
        number = str(numbered_exhibits[0]["exhibit_number"]).strip()
        body = _chat(client, message=number)
        if is_ambiguous_number_reply(body):
            # Контракт B9: тут в поле лежат варианты витрин, а не вопросы.
            report.add([Check(f"II-7 поиск по номеру ({number}): подсказки непустые", None,
                              "номер неуникален — ответ уточняющий, в поле варианты витрин")])
        else:
            report.add(check_suggestions(body.get("suggested_questions"), f"поиск по номеру ({number})", rules))
    else:
        report.add([Check("II-7 поиск по номеру: подсказки непустые", None,
                          "в каталоге стенда нет числовых номеров экспонатов")])

    if not exhibits:
        report.add([Check("II-7 контекст экспоната: подсказки непустые", None, "в каталоге стенда нет экспонатов")])
        return

    exhibit_id = int(exhibits[0]["id"])
    first = _chat(client, message="Расскажите про этот предмет", context={"exhibit_id": exhibit_id})
    report.add(check_suggestions(first.get("suggested_questions"), "контекст экспоната", rules))

    # П. II-7 дословно: «После выбора одного вопроса и получения ответа на него,
    # варианты вопросов уже не предлагаются, надо возвращаться назад». Тапаем по
    # первой подсказке в той же сессии и смотрим, что осталось.
    hints = [str(item) for item in (first.get("suggested_questions") or [])]
    if hints:
        second = _chat(client, message=hints[0], session_id=str(first["session_id"]))
        report.add(check_suggestions(second.get("suggested_questions"), "после ответа на подсказку", rules))
        report.add(check_asked_question_is_gone(second.get("suggested_questions"), hints[0],
                                                "после ответа на подсказку", rules))
    else:
        report.add([Check("II-7 после ответа на подсказку: подсказки непустые", None,
                          "первый ответ пришёл без подсказок — сравнивать не с чем")])

    story = client.post("/guide/story", json={"exhibit_id": exhibit_id, "max_questions": 4})
    if story.status_code == 200:
        report.add(check_suggestions(story.json().get("suggested_questions"), "рассказ об экспонате", rules))
    else:
        report.add([Check("II-7 рассказ об экспонате: подсказки непустые", False,
                          f"POST /guide/story → {story.status_code}: {story.text[:160]}")])


def run_iv1(client: httpx.Client, report: Report, token: str) -> None:
    """IV-1 на СВОЕЙ карточке в СВОЁМ зале, с уборкой за собой.

    Карточки музея не трогаем принципиально: проверять «правка не портит
    карточку» правкой настоящей карточки — способ однажды закрыть пункт IV-1 его
    же воспроизведением. `short_description_spoken` передаём всегда: без него
    админка полезет в LLM пересчитывать озвучку (E15), а платить за это в smoke
    незачем.
    """
    auth = {"Authorization": f"Bearer {token}"}
    hall_id = showcase_id = exhibit_id = None
    try:
        hall = client.post("/admin/halls", headers=auth,
                           json={"name": "Smoke 31.08: временный зал", "hall_number": None})
        if hall.status_code != 201:
            report.add([Check("IV-1 подготовка: временный зал", False,
                              f"POST /admin/halls → {hall.status_code}: {hall.text[:200]}")])
            return
        hall_id = hall.json()["id"]
        showcase = client.post("/admin/showcases", headers=auth,
                               json={"hall_id": hall_id, "showcase_number": 1, "name": "Smoke 31.08: витрина"})
        showcase.raise_for_status()
        showcase_id = showcase.json()["id"]

        created = client.post("/admin/exhibits", headers=auth, json={
            "showcase_id": showcase_id,
            "name": "Smoke 31.08: карточка для проверки PUT",
            "year_created": "1894",
            "master_name": "Фирма К. Фаберже, мастер М. Перхин",
            "material": "золото, эмаль",
            "techniques": "литьё, чеканка",
            "short_description": "Описание, которое PUT не имеет права потерять.",
            "short_description_spoken": "Описание, которое PUT не имеет права потерять.",
            "image_url": "https://cdn.example/smoke-20260831.jpg",
        })
        created.raise_for_status()
        before = created.json()
        exhibit_id = before["id"]

        # Ровно тот сценарий, на котором музей потерял данные: правим техники,
        # присылая только обязательные поля и их.
        changed = {"techniques": "литьё, чеканка, гравировка, полировка"}
        put = client.put(f"/admin/exhibits/{exhibit_id}", headers=auth,
                         json={"showcase_id": showcase_id, "name": before["name"], **changed})
        put.raise_for_status()
        report.add(check_put_kept(before, put.json(), changed))

        # И обратная сторона контракта: явный null обязан очищать.
        cleared = client.put(f"/admin/exhibits/{exhibit_id}", headers=auth, json={
            "showcase_id": showcase_id, "name": before["name"],
            "image_url": None, "short_description": None, "short_description_spoken": None,
        })
        cleared.raise_for_status()
        report.add(check_put_cleared(cleared.json(), ("image_url", "short_description")))
    finally:
        # Уборка best-effort и в обратном порядке создания: карточка, витрина, зал.
        if exhibit_id is not None:
            client.delete(f"/admin/exhibits/{exhibit_id}", headers=auth)
        if showcase_id is not None:
            client.delete(f"/admin/showcases/{showcase_id}", headers=auth, params={"force": True})
        if hall_id is not None:
            client.delete(f"/admin/halls/{hall_id}", headers=auth, params={"force": True})


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    try:
        base_url = resolve_base_url(os.environ, args.base_url)
        sections = select_sections(args.only)
        token = resolve_admin_token(os.environ, args.admin_token)
        if args.write:
            if looks_like_production(base_url):
                raise ConfigError(
                    f"--write на прод ({base_url}) запрещён: по решению Д11 релиза 31.08.2026 прод мы не трогаем. "
                    "Прогоните проверку IV-1 на стенде."
                )
            if not token:
                raise ConfigError("--write требует токен админки: ADMIN_TOKEN=… (или --admin-token)")
    except ConfigError as exc:
        print(f"ОШИБКА КОНФИГУРАЦИИ: {exc}", file=sys.stderr)
        return 2

    rules = default_rules()
    report = Report()
    print(f"Стенд: {base_url}")
    print(f"Секции: {', '.join(sections)}")
    print("Режим: " + ("запись включена (IV-1 на временной карточке)" if args.write
                       else "только чтение — IV-1 пропускается, включается ключом --write"))
    if not rules.strict:
        print("ВНИМАНИЕ: app/services/guide_style.py не импортировался — подсказки проверяются "
              "ослабленными правилами (только три формулировки со скриншота п. II-8).")
    print("—" * 72)

    with httpx.Client(base_url=base_url, timeout=args.timeout) as client:
        # Каталог читаем один раз: он нужен почти всем секциям, а лишние запросы
        # к стенду только замедляют прогон.
        halls_response = client.get("/halls", params={"limit": 100})
        halls_response.raise_for_status()
        public_halls: List[Dict[str, object]] = halls_response.json()["items"]
        exhibits: List[Dict[str, object]] = []
        if {"I-2", "II-1", "II-hints"} & set(sections):
            exhibits_response = client.get("/exhibits", params={"limit": 100})
            exhibits_response.raise_for_status()
            exhibits = exhibits_response.json()["items"]

        if "I-1" in sections:
            run_i1(client, report, public_halls)
        if "I-2" in sections:
            run_i2(client, report, exhibits)
        if "I-3" in sections:
            run_i3(client, report, public_halls)
        if "II-1" in sections:
            run_ii1(client, report, exhibits)
        if "II-hints" in sections:
            run_hints(client, report, rules, public_halls, exhibits)
        if "IV-1" in sections:
            if args.write:
                run_iv1(client, report, token)
            else:
                report.add([Check("IV-1 PUT не затирает непереданные поля", None,
                                  "нужна запись в каталог — запустите с --write")])

    print("—" * 72)
    if report.failures:
        print("Провалы:")
        for check in report.failures:
            print(f"  • {check.name}" + (f" — {check.detail}" if check.detail else ""))
    print(report.summary())
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())
