"""Юнит-тесты приёмочного смоука релиза 31.08.2026 (scripts/smoke_bugreport_20260831.py).

Смоук — последняя инстанция перед сдачей музею, и цена его ошибки несимметрична:
зелёный прогон на сломанном стенде хуже красного на исправном. Поэтому проверяем
именно то, чем смоук может соврать:

  • состав и ПОРЯДОК полей карточки в смоуке сверяются с `app/schemas.py` —
    списки там продублированы намеренно (смоук судит чужой, задеплоенный код),
    и без этого теста они разъехались бы со схемой при первой же правке;
  • «нет данных» не выдаётся за «работает»: пропуск (`Check.ok is None`) считается
    отдельно от успеха, и каждая ветка пропуска проверена поимённо;
  • разбор ответа: перестановка полей, пропавший ключ, `location: null`, обрыв
    превью посреди слова, плашка без упоминания, перефразировка в подсказках —
    всё это обязано ронять проверку, а не проходить мимо;
  • конфигурация: без BASE_URL и с `--write` на прод смоук завершается кодом 2 и
    НЕ ходит в сеть (решение Д11 — прод не трогаем).

Ни сети, ни БД: все проверяемые функции чистые, на входе словари-фикстуры (форма
ответов взята из `app/schemas.py`), на выходе список `Check`.

Запуск: python -m pytest tests/test_smoke_bugreport_20260831.py
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

from app import schemas as sch  # noqa: E402
import smoke_bugreport_20260831 as smoke  # noqa: E402


# ── Фикстуры: формы ответов стенда ───────────────────────────────────────────
def hall(**over) -> dict:
    """Зал в том виде, в каком его отдаёт GET /halls (порядок ключей неважен)."""
    data = {
        "id": 1, "hall_number": 1, "name": "Парадная лестница",
        "description": "Музей Фаберже расположен в Шуваловском дворце. Лестница выстроена в 1840-х годах.",
        "level": 1, "cover_image_url": None, "is_temporary": False, "is_service": False,
        "sort_order": 1, "showcase_count": 0, "exhibit_count": 0,
        "description_preview": "Музей Фаберже расположен в Шуваловском дворце.",
        "description_has_more": True,
    }
    data.update(over)
    return data


def card(**over) -> dict:
    """Карточка предмета: ключи ровно в том порядке, в каком их отдаёт схема."""
    values = {
        "id": 101, "exhibit_number": "12", "label_slug": "faberge_egg_landyshi",
        "name": "Пасхальное яйцо «Ландыши»", "image_url": "https://cdn.example/1.jpg", "images": [],
        "location": location(), "hall": None, "showcase": None,
        "year_created": "1898", "origin_place": "Санкт-Петербург",
        "maker": {"text": "Фирма К. Фаберже, мастер М. Перхин", "firm": "Фирма К. Фаберже",
                  "master": "мастер М. Перхин"},
        "master_name": "Фирма К. Фаберже, мастер М. Перхин",
        "material": "золото", "techniques": "эмаль по гильошированному фону",
        "short_description": "Описание.", "video_url": None, "model_3d_url": None,
        "model_3d_embed": None, "audio_url": None, "source_url": None,
    }
    values.update(over)
    return {field: values[field] for field in smoke.EXPECTED_CARD_FIELDS}


def location(**over) -> dict:
    values = {
        "hall_id": 4, "hall_number": 4, "hall_name": "Синяя гостиная",
        "showcase_id": 32, "showcase_number": 5, "showcase_name": None,
        "text": "Зал 4 «Синяя гостиная», витрина 5",
        "text_in": "в зале 4 «Синяя гостиная», витрина 5",
    }
    values.update(over)
    return {field: values[field] for field in smoke.EXPECTED_LOCATION_FIELDS}


def summary(**over) -> dict:
    values = {
        "id": 101, "exhibit_number": "12", "label_slug": "faberge_egg_landyshi",
        "name": "Пасхальное яйцо «Ландыши»", "thumbnail_url": None, "location": location(),
        "hall_id": 3, "showcase_id": 7, "showcase_number": 2, "year_created": "1898",
        "maker": {"text": None, "firm": None, "master": None}, "master_name": None,
        "is_temporary": False,
    }
    values.update(over)
    return {field: values[field] for field in smoke.EXPECTED_SUMMARY_FIELDS}


def full_catalog() -> list:
    """Двенадцать пронумерованных залов + группа без номера — как на проде."""
    halls = [hall()]
    halls += [hall(id=n, hall_number=n, name=f"Зал {n}", sort_order=n,
                   description=None, description_preview=None, description_has_more=False)
              for n in range(2, 13)]
    halls.append(hall(id=99, hall_number=None, name="Вне постоянной экспозиции", sort_order=99,
                      description=None, description_preview=None, description_has_more=False))
    return halls


def named(checks, part: str) -> smoke.Check:
    """Единственная проверка, в имени которой встречается `part`."""
    found = [check for check in checks if part in check.name]
    assert len(found) == 1, f"ожидалась одна проверка про {part!r}, найдено: {[c.name for c in found]}"
    return found[0]


def results(checks) -> list:
    return [check.ok for check in checks]


# ── Контракт карточки живёт в app/schemas.py ─────────────────────────────────
def test_expected_fields_match_the_schema():
    """Списки полей в смоуке — копия схемы; разъехались бы молча, если бы не этот тест.

    Дублирование в смоуке сознательное: он проверяет ответ ЗАДЕПЛОЕННОГО кода, и
    сверка схемы с самой собой не поймала бы ни старый образ на стенде, ни правку
    в обход схемы. Но копия обязана быть верной — это и проверяется здесь.
    """
    assert list(smoke.EXPECTED_CARD_FIELDS) == list(sch.Exhibit.model_fields)
    assert list(smoke.EXPECTED_SUMMARY_FIELDS) == list(sch.ExhibitSummary.model_fields)
    assert list(smoke.EXPECTED_LOCATION_FIELDS) == list(sch.ExhibitLocation.model_fields)
    assert list(smoke.EXPECTED_MAKER_FIELDS) == list(sch.ExhibitMaker.model_fields)


def test_predictable_fields_are_all_in_the_card():
    """Поля, потерянные музеем 31.08.2026, должны существовать в схеме карточки."""
    for field in smoke.PREDICTABLE_FIELDS:
        assert field in sch.Exhibit.model_fields, field


def test_hall_preview_fields_are_the_ones_the_schema_computes():
    """Превью зала смоук читает из тех же ключей, которые схема вычисляет."""
    assert "description_preview" in sch.Hall.model_computed_fields
    assert "description_has_more" in sch.Hall.model_computed_fields


# ── Конфигурация: BASE_URL, токен, прод, секции ──────────────────────────────
def test_base_url_is_required():
    with pytest.raises(smoke.ConfigError) as exc:
        smoke.resolve_base_url({})
    assert "BASE_URL" in str(exc.value)


def test_base_url_needs_a_scheme():
    """Голый хост httpx превратит в относительный путь — падать лучше сразу."""
    with pytest.raises(smoke.ConfigError):
        smoke.resolve_base_url({"BASE_URL": "stand.example.ru"})


def test_base_url_from_env_and_cli():
    assert smoke.resolve_base_url({"BASE_URL": "https://stand.example.ru/"}) == "https://stand.example.ru"
    assert smoke.resolve_base_url({"BASE_URL": "https://env"}, "https://cli") == "https://cli"


def test_admin_token_accepts_both_historic_names():
    """Скрипты каталога читают ADMIN_TOKEN, соседние смоуки — ADMIN_API_TOKEN."""
    assert smoke.resolve_admin_token({"ADMIN_TOKEN": "a"}) == "a"
    assert smoke.resolve_admin_token({"ADMIN_API_TOKEN": "b"}) == "b"
    assert smoke.resolve_admin_token({"ADMIN_TOKEN": "a", "ADMIN_API_TOKEN": "b"}) == "a"
    assert smoke.resolve_admin_token({}, "cli") == "cli"
    assert smoke.resolve_admin_token({}) == ""


def test_production_is_recognised_by_host_not_by_substring():
    """Стенд живёт за таким же гейтвеем — сравнение по подстроке запретило бы и его."""
    assert smoke.looks_like_production("https://d5dhcivtos7rfvdfdpg2.xxg4zr82.apigw.yandexcloud.net")
    assert smoke.looks_like_production("https://d5dhcivtos7rfvdfdpg2.xxg4zr82.apigw.yandexcloud.net/halls")
    assert not smoke.looks_like_production("https://d0stand00000000000.xxg4zr82.apigw.yandexcloud.net")
    assert not smoke.looks_like_production("http://localhost:8000")


def test_sections_default_to_everything_and_keep_canonical_order():
    assert smoke.select_sections(None) == smoke.SECTIONS
    assert smoke.select_sections("IV-1, I-1") == ("I-1", "IV-1")


def test_unknown_section_is_a_configuration_error():
    with pytest.raises(smoke.ConfigError) as exc:
        smoke.select_sections("I-1,V-9")
    assert "V-9" in str(exc.value)


def test_write_is_off_by_default():
    """По умолчанию смоук ничего не пишет в каталог — это его главное обещание."""
    assert smoke.parse_args([]).write is False
    assert smoke.parse_args(["--write"]).write is True


def test_main_refuses_without_base_url_and_never_opens_a_connection(monkeypatch, capsys):
    monkeypatch.delenv("BASE_URL", raising=False)
    monkeypatch.setattr(smoke.httpx, "Client", _forbidden_client)
    assert smoke.main([]) == 2
    assert "BASE_URL" in capsys.readouterr().err


def test_main_refuses_to_write_to_production(monkeypatch, capsys):
    monkeypatch.setenv("BASE_URL", "https://d5dhcivtos7rfvdfdpg2.xxg4zr82.apigw.yandexcloud.net")
    monkeypatch.setenv("ADMIN_TOKEN", "secret")
    monkeypatch.setattr(smoke.httpx, "Client", _forbidden_client)
    assert smoke.main(["--write"]) == 2
    assert "прод" in capsys.readouterr().err.lower()


def test_main_refuses_to_write_without_a_token(monkeypatch, capsys):
    monkeypatch.setenv("BASE_URL", "https://stand.example.ru")
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    monkeypatch.delenv("ADMIN_API_TOKEN", raising=False)
    monkeypatch.setattr(smoke.httpx, "Client", _forbidden_client)
    assert smoke.main(["--write"]) == 2
    assert "ADMIN_TOKEN" in capsys.readouterr().err


def _forbidden_client(*args, **kwargs):  # pragma: no cover — вызов означал бы поход в сеть
    raise AssertionError("смоук не должен открывать соединение при ошибке конфигурации")


# ── I-1. «Парадная лестница» ─────────────────────────────────────────────────
def test_staircase_is_first_and_public():
    assert all(check.ok for check in smoke.check_staircase_first(full_catalog()))


def test_staircase_still_hidden_by_the_service_flag_fails():
    """Флаг сняли не везде — самый вероятный исход недокатанного релиза."""
    catalog = full_catalog()
    catalog[0] = hall(is_service=True)
    assert named(smoke.check_staircase_first(catalog), "служебный флаг").ok is False


def test_staircase_not_first_fails_and_names_the_order():
    catalog = full_catalog()
    catalog.insert(0, hall(id=2, hall_number=2, name="Рыцарский зал", sort_order=0))
    check = named(smoke.check_staircase_first(catalog), "первая среди пронумерованных")
    assert check.ok is False and "Рыцарский зал" in check.detail


def test_old_architectural_description_does_not_pass_the_gate():
    """Гейт Д12: описание про перила и купол формально непустое, но это не рассказ о музее."""
    catalog = full_catalog()
    catalog[0] = hall(description="Перила лестницы выполнены по рисункам архитектора Иеронима Корсини.")
    assert named(smoke.check_staircase_first(catalog), "рассказывает о дворце").ok is False


def test_missing_staircase_fails_on_a_full_catalog_but_skips_on_the_demo_seed():
    """«Зала нет» — провал на настоящем каталоге и пропуск на демо-сиде, где его и не бывает."""
    catalog = [hall(id=n, hall_number=n, name=f"Зал {n}") for n in range(2, 14)]
    assert named(smoke.check_staircase_first(catalog), "публичной выдаче").ok is False
    demo = [hall(id=n, hall_number=n, name=f"Зал {n}") for n in range(2, 5)]
    assert named(smoke.check_staircase_first(demo), "публичной выдаче").ok is None


def test_guide_hall_count_is_read_out_of_the_answer():
    assert smoke.hall_count_in_answer("В музее 12 залов. Основная экспозиция: зал 1 «Парадная лестница».") == 12
    assert smoke.hall_count_in_answer("Пока в каталоге нет залов.") is None


def test_guide_counts_the_staircase_too():
    answer = "В музее 12 залов. Основная экспозиция: зал 1 «Парадная лестница»."
    assert all(check.ok for check in smoke.check_guide_hall_count(answer, full_catalog()))


def test_guide_still_saying_eleven_halls_fails():
    """Ровно тот побочный эффект старого решения, который музей отменил."""
    checks = smoke.check_guide_hall_count("В музее 11 залов.", full_catalog())
    assert results(checks) == [False, False]


def test_the_number_twelve_is_not_checked_on_an_incomplete_catalog():
    """Непроверяемых цифр не бывает: на демо-сиде цифра из требования пропускается."""
    demo = [hall(id=n, hall_number=n, name=f"Зал {n}") for n in range(2, 5)]
    checks = smoke.check_guide_hall_count("В музее 3 зала.", demo)
    assert named(checks, "сходится").ok is True
    assert named(checks, f"{smoke.EXPECTED_HALL_COUNT} залов").ok is None


# ── I-2. Карточка предмета ───────────────────────────────────────────────────
def test_card_in_the_expected_shape_passes():
    assert all(check.ok is not False for check in smoke.check_card_contract(card(), "карточка"))


def test_reordered_card_fails_and_says_it_is_the_order():
    """Порядок полей — контракт: музей просил расположение и дату НАД названием."""
    shuffled = card()
    reordered = {"name": shuffled.pop("name"), **shuffled}
    check = named(smoke.check_card_contract(reordered, "карточка"), "состав и порядок полей — как просил")
    assert check.ok is False and "порядок" in check.detail


def test_missing_key_is_reported_as_missing_not_as_reordering():
    broken = card()
    broken.pop("origin_place")
    checks = smoke.check_card_contract(broken, "карточка")
    assert "нет полей: origin_place" in named(checks, "состав и порядок полей — как просил").detail
    assert named(checks, "пустые поля приходят ключами").ok is False


def test_null_values_are_fine_as_long_as_the_keys_are_there():
    """Прогнозируемость — это ключ со значением null, а не отсутствие ключа."""
    empty = card(image_url=None, short_description=None, material=None, techniques=None,
                 year_created=None, origin_place=None, master_name=None)
    checks = smoke.check_card_contract(empty, "карточка")
    assert named(checks, "пустые поля приходят ключами").ok is True
    assert named(checks, "состав и порядок полей — как просил").ok is True


def test_location_as_null_fails_even_though_the_key_is_there():
    """`location: null` — ровно то, чего просили не делать: объект пустеет внутрь."""
    checks = smoke.check_card_contract(card(location=None), "карточка")
    assert named(checks, "location — объект").ok is False


def test_maker_without_the_split_fails():
    checks = smoke.check_card_contract(card(maker={"text": "Фирма К. Фаберже"}), "карточка")
    assert named(checks, "maker — объект").ok is False


def test_location_string_must_be_ready_to_show():
    """Расположение обещано готовой строкой — пустой text это обещание нарушает."""
    checks = smoke.check_card_contract(card(location=location(text="", text_in="")), "карточка")
    assert named(checks, "готовой строкой").ok is False


def test_location_string_is_skipped_for_an_exhibit_outside_any_showcase():
    checks = smoke.check_card_contract(card(location=location(showcase_id=None, text=None, text_in=None)),
                                       "карточка")
    assert named(checks, "готовой строкой").ok is None


def test_summary_shape_is_checked_too():
    assert all(check.ok for check in smoke.check_summary_contract(summary(), "список"))
    broken = summary()
    broken.pop("location")
    assert named(smoke.check_summary_contract(broken, "список"), "краткой карточки").ok is False


# ── I-3. Превью описания зала ────────────────────────────────────────────────
def test_preview_break_kinds():
    assert smoke.preview_break_kind("Всё описание.", "Всё описание.") == smoke.BREAK_FULL
    assert smoke.preview_break_kind("Первое.", "Первое. Второе.") == smoke.BREAK_SENTENCE
    assert smoke.preview_break_kind("Очень длин…", "Очень длинное слово.") == smoke.BREAK_BROKEN
    assert smoke.preview_break_kind("Очень длинное слов", "Очень длинное слово.") == smoke.BREAK_BROKEN
    assert smoke.preview_break_kind("Слово за словом…", "Слово за словом и дальше.") == smoke.BREAK_WORD
    # Обрезка снимает с хвоста « ,;:—-», поэтому после среза в описании остаётся
    # запятая, а не пробел, — это по-прежнему корректный обрыв по слову.
    assert smoke.preview_break_kind("Слово за словом…", "Слово за словом, и дальше.") == smoke.BREAK_WORD
    assert smoke.preview_break_kind("Совершенно другой текст.", "Первое. Второе.") == smoke.BREAK_ALIEN


def test_good_preview_passes():
    assert all(check.ok for check in smoke.check_hall_preview(hall(), "зал"))


def test_preview_equal_to_the_description_is_not_a_preview():
    """Если превью равно описанию, скрывать нечего — и кнопки «Подробнее» быть не должно."""
    text = "Короткое описание."
    checks = smoke.check_hall_preview(
        hall(description=text, description_preview=text, description_has_more=False), "зал")
    assert named(checks, "короче полного").ok is False
    assert named(checks, "признак «есть что раскрывать»").ok is True


def test_has_more_out_of_sync_with_the_preview_fails():
    """Кнопка «Подробнее», которая ничего не открывает, — та же болячка с другой стороны."""
    checks = smoke.check_hall_preview(hall(description_has_more=False), "зал")
    assert named(checks, "признак «есть что раскрывать»").ok is False


def test_preview_broken_in_the_middle_of_a_word_fails():
    checks = smoke.check_hall_preview(
        hall(description="Музей Фаберже расположен в Шуваловском дворце.",
             description_preview="Музей Фаберже распол…"), "зал")
    assert named(checks, "по границе").ok is False


def test_empty_preview_fails_and_a_hall_without_description_is_skipped():
    assert named(smoke.check_hall_preview(hall(description_preview=""), "зал"), "непустое").ok is False
    checks = smoke.check_hall_preview(hall(description=None, description_preview=None), "зал")
    assert results(checks) == [None]


def test_pick_hall_takes_the_one_with_something_to_hide():
    catalog = full_catalog()
    assert smoke.pick_hall_with_long_description(catalog)["name"] == "Парадная лестница"
    assert smoke.pick_hall_with_long_description([hall(description=None, description_has_more=False)]) is None


# ── II-1. Блок «Упомянуто в ответе» ──────────────────────────────────────────
def test_general_question_with_an_empty_block_passes():
    body = {"answer": "В музее собрано более четырёх тысяч произведений.", "referenced_exhibits": []}
    assert all(check.ok for check in smoke.check_referenced_empty(body, "как появился музей"))


def test_general_question_with_ghost_plaques_fails():
    """Дословная жалоба музея: в блоке предметы, которых в ответе нет."""
    body = {
        "answer": "Яйцо «Орден Святого Георгия» вывезла из России в 1919 году императрица Мария Фёдоровна.",
        "referenced_exhibits": [
            {"id": 5, "name": "Блюдо с монограммами", "exhibit_number": None, "mentioned_in": "answer"},
            {"id": 6, "name": "Бювар с монограммами", "exhibit_number": None, "mentioned_in": "answer"},
        ],
    }
    question = "как появился музей"
    assert named(smoke.check_referenced_empty(body, question), "блок «Упомянуто в ответе» пуст").ok is False
    check = named(smoke.check_plaques_are_really_mentioned(body, question), "подтверждены упоминанием")
    assert check.ok is False and "Блюдо с монограммами" in check.detail


def test_plaque_without_a_source_means_the_old_behaviour_is_still_on():
    """Пустой mentioned_in в обычном диалоге = блок собран поиском без порога."""
    body = {"answer": "Ответ.", "referenced_exhibits": [{"id": 5, "name": "Ваза", "mentioned_in": None}]}
    check = named(smoke.check_plaques_are_really_mentioned(body, "вопрос"), "помечены источником")
    assert check.ok is False and "GUIDE_REFERENCED_REQUIRE_MENTION" in check.detail


def test_context_plaque_is_not_judged_by_mentions():
    """Экспонат, у которого стоит посетитель, в блоке законен и без упоминания."""
    body = {"answer": "Ответ без имён.",
            "referenced_exhibits": [{"id": 5, "name": "Ваза", "mentioned_in": "context"}]}
    checks = smoke.check_plaques_are_really_mentioned(body, "вопрос")
    assert named(checks, "подтверждены упоминанием").ok is None


def test_named_exhibit_must_be_in_the_block():
    body = {
        "answer": "Пасхальное яйцо «Ландыши» подарили императрице Александре Фёдоровне.",
        "referenced_exhibits": [{"id": 101, "name": "Пасхальное яйцо «Ландыши»",
                                 "exhibit_number": "12", "mentioned_in": "answer"}],
    }
    assert all(check.ok for check in smoke.check_referenced_named(body, 101, "Пасхальное яйцо «Ландыши»"))


def test_named_exhibit_missing_from_the_block_fails_and_blames_search_when_it_is_search():
    body = {"answer": "Ответ.", "referenced_exhibits": []}
    check = named(smoke.check_referenced_named(body, 101, "Пасхальное яйцо «Ландыши»", searchable=False),
                  "он и стоит в блоке")
    assert check.ok is False and "GET /search" in check.detail


def test_stranger_next_to_the_named_exhibit_fails():
    body = {
        "answer": "Пасхальное яйцо «Ландыши».",
        "referenced_exhibits": [
            {"id": 101, "name": "Пасхальное яйцо «Ландыши»", "mentioned_in": "answer"},
            {"id": 7, "name": "Ваза с изображением цветов", "mentioned_in": "answer"},
        ],
    }
    assert named(smoke.check_referenced_named(body, 101, "Пасхальное яйцо «Ландыши»"),
                 "нет посторонних").ok is False


def test_pick_exhibit_prefers_a_name_in_quotes():
    """На «Вазе» проверка упоминания ничего бы не доказала — слово встречается везде."""
    items = [{"id": 1, "name": "Ваза"}, {"id": 2, "name": "Пасхальное яйцо «Ландыши»"}]
    assert smoke.pick_exhibit_with_proper_name(items)["id"] == 2
    assert smoke.pick_exhibit_with_proper_name([{"id": 1, "name": "Ваза"}]) is None


# ── II-2/3/7/8. Подсказки ────────────────────────────────────────────────────
RULES = smoke.default_rules()


def test_good_suggestions_pass():
    questions = ["Кому подарили это яйцо?", "Что за сюрприз спрятан внутри?", "Что ещё посмотреть в этом зале?"]
    assert all(check.ok for check in smoke.check_suggestions(questions, "ветка", RULES))


def test_empty_suggestions_fail():
    """П. II-7 дословно: «после ответа варианты вопросов уже не предлагаются»."""
    assert named(smoke.check_suggestions([], "ветка", RULES), "подсказки непустые").ok is False
    assert named(smoke.check_suggestions(None, "ветка", RULES), "подсказки непустые").ok is False
    assert named(smoke.check_suggestions(["Вопрос?", "   "], "ветка", RULES), "подсказки непустые").ok is False


@pytest.mark.parametrize("question", [
    "Сколько времени заняло создание этого предмета?",
    "Какие уникальные особенности есть у этой работы Михаила Перхина?",
    "Почему для украшения была выбрана именно жёлтая гильоше-эмаль?",
])
def test_the_three_questions_from_the_screenshot_fail(question):
    """Дословные формулировки со скриншота п. II-8 («подобные вопросы не нужны»)."""
    checks = smoke.check_suggestions(["Кому подарили это яйцо?", question], "ветка", RULES)
    check = named(checks, "не предлагать")
    assert check.ok is False and question in check.detail


def test_rephrasings_of_one_question_fail():
    """Скриншот п. II-2: тот же вопрос про скифские мотивы, сформулированный иначе."""
    questions = [
        "Какие именно скифские мотивы использованы в браслете?",
        "Какие именно скифские мотивы Эрик Коллин использовал в дизайне браслета?",
    ]
    assert named(smoke.check_suggestions(questions, "ветка", RULES), "не перефразируют").ok is False


def test_the_question_just_asked_is_not_offered_again():
    asked = "Какие именно скифские мотивы использованы в браслете?"
    same = "Какие именно скифские мотивы Эрик Коллин использовал в дизайне браслета?"
    assert named(smoke.check_asked_question_is_gone([same], asked, "ветка", RULES),
                 "больше не предлагается").ok is False
    assert named(smoke.check_asked_question_is_gone(["Кому подарили это яйцо?"], asked, "ветка", RULES),
                 "больше не предлагается").ok is True
    assert named(smoke.check_asked_question_is_gone([], asked, "ветка", RULES),
                 "больше не предлагается").ok is None


def test_fallback_rules_still_catch_the_screenshot_questions():
    """Копия скрипта без репозитория судит слабее — но три формулировки ловит.

    Ослабление обязано быть ЗАМЕТНЫМ: `strict=False` печатается в шапке прогона,
    чтобы «зелено» не читалось как «проверено тем же фильтром, что на стенде».
    """
    rules = smoke.QuestionRules(smoke._fallback_is_meaningless, smoke._fallback_dedupe, strict=False)
    checks = smoke.check_suggestions(["Сколько времени заняло создание этого предмета?"], "ветка", rules)
    assert named(checks, "не предлагать").ok is False
    # Перефразировку запасной вариант не ловит — и не притворяется, что ловит.
    checks = smoke.check_suggestions(
        ["Какие именно скифские мотивы использованы в браслете?",
         "Какие именно скифские мотивы Эрик Коллин использовал в дизайне браслета?"], "ветка", rules)
    assert named(checks, "не перефразируют").ok is True


def test_ambiguous_number_reply_is_recognised():
    """Ветка B9 кладёт в suggested_questions варианты витрин — судить их правилами нельзя."""
    assert smoke.is_ambiguous_number_reply(
        {"answer": "Экспонат №12 встречается в нескольких витринах. Уточните, пожалуйста, зал или витрину:"})
    assert not smoke.is_ambiguous_number_reply({"answer": "Пасхальное яйцо «Ландыши», зал 4, витрина 5."})


# ── IV-1. PUT карточки ───────────────────────────────────────────────────────
BEFORE_PUT = {
    "id": 7, "name": "Пасхальное яйцо-шкатулка «Ренессанс»",
    "image_url": "https://cdn.example/renessans.jpg",
    "short_description": "Описание, которое PUT не имеет права потерять.",
    "material": "золото, эмаль", "techniques": "литьё, чеканка",
    "year_created": "1894", "master_name": "Фирма К. Фаберже, мастер М. Перхин",
}


def test_put_that_keeps_everything_passes():
    after = {**BEFORE_PUT, "techniques": "литьё, чеканка, гравировка"}
    assert all(check.ok for check in
               smoke.check_put_kept(BEFORE_PUT, after, {"techniques": "литьё, чеканка, гравировка"}))


def test_put_that_wipes_image_and_description_fails_and_names_the_fields():
    """Ровно то, что музей увидел у яйца «Ренессанс»: остались техники, пропало остальное."""
    after = {**BEFORE_PUT, "techniques": "литьё, чеканка, гравировка",
             "image_url": None, "short_description": None, "material": None}
    check = named(smoke.check_put_kept(BEFORE_PUT, after, {"techniques": "литьё, чеканка, гравировка"}),
                  "не затирает")
    assert check.ok is False
    assert "image_url" in check.detail and "short_description" in check.detail and "material" in check.detail


def test_put_that_ignores_the_field_it_was_given_fails():
    """Обратная крайность: «не затирать непереданное» не должно съесть переданное."""
    check = named(smoke.check_put_kept(BEFORE_PUT, dict(BEFORE_PUT), {"techniques": "гравировка"}),
                  "всё-таки записалось")
    assert check.ok is False


def test_explicit_null_still_clears_the_field():
    after = {**BEFORE_PUT, "image_url": None, "short_description": None}
    assert smoke.check_put_cleared(after, ("image_url", "short_description"))[0].ok is True
    check = smoke.check_put_cleared(BEFORE_PUT, ("image_url", "short_description"))[0]
    assert check.ok is False and "image_url" in check.detail


# ── Отчёт ────────────────────────────────────────────────────────────────────
def test_report_counts_skips_apart_from_passes(capsys):
    """«Нет данных» не должно читаться как «работает» — иначе приёмка врёт."""
    report = smoke.Report()
    report.add([smoke.Check("а", True), smoke.Check("б", False, "деталь"), smoke.Check("в", None, "нечего")])
    out = capsys.readouterr().out
    assert (report.passed, report.failed, report.skipped) == (1, 1, 1)
    assert report.summary() == "пройдено: 1, провалено: 1, пропущено: 1"
    assert "PASS а" in out and "FAIL б — деталь" in out and "SKIP в — нечего" in out
    assert [check.name for check in report.failures] == ["б"]
