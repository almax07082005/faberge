"""Юнит-тесты восстановителя обнулённых карточек (баг-репорт 31.08.2026, IV-1).

Скрипт трогает прод, поэтому проверяется в первую очередь то, чем он может навредить:

  • стадия image берёт URL ТОЛЬКО из строки галереи с `is_primary` и только когда такая
    строка ровно одна: при двух первичных неизвестно, какая лежала в `image_url`;
  • непустое поле не перезаписывается никогда — отсюда идемпотентность, второй прогон
    даёт пустой план (правило `backfill_catalog_fields.py`);
  • источник описания выбирается в порядке raw_history → сайт музея → сид, и при отсутствии
    всех трёх карточка уходит в отчёт музею, а не получает выдуманный текст;
  • тело PATCH при восстановлении описания всегда несёт `short_description_spoken` — иначе
    `admin._autofill_spoken` погонит десятки карточек в LLM и перепишет ручную озвучку (E15);
  • откат рапортует ПРАВДУ: считает после успешного запроса, сверяет тело ответа (200 с
    прежним значением — это не откат) и печатает возвращено / пропущено / ошибок;
  • сайт музея опрашивается с паузой, неудачи считаются, и карточка, чей источник просто не
    ответил, попадает в отчёт с другой формулировкой, чем карточка без источника;
  • разбор ключей CLI: сухой прогон и безопасная стадия — значения по умолчанию.

Ни сети, ни БД: сетевой слой скрипта — функции `api`/`fetch_records`/`load_site`, ядро от них
не зависит и работает на словарях-фикстурах, как в tests/test_backfill_catalog.py.
`load_site` проверяется с подменёнными `time.sleep` и `scrape_faberge.fetch` — наружу тест
не ходит.

Запуск: python -m pytest tests/test_restore_wiped_cards.py
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import restore_wiped_cards_20260831 as restore  # noqa: E402

PHOTO = "https://cdn.example/exhibits/renessans/01.jpg"

# Карточка «Ренессанса» в состоянии ПОСЛЕ поломки: описание и материалы обнулены PUT-ом,
# image_url пуст, а галерея цела и ровно с одной строкой is_primary. Именно это расхождение
# и есть отпечаток дефекта — штатным путём image_url при живой галерее пустым не бывает.
WIPED = {
    "id": 7,
    "name": "Пасхальное яйцо-шкатулка «Ренессанс»",
    "label_slug": "faberge_pasxalnoe_yajczo_shkatulka_renessans",
    "source_url": "https://fabergemuseum.ru/kollekczii/shedevryi-kollekczii/pasxalnoe-yajczo-shkatulka-renessans",
    "image_url": None,
    "material": None,
    "short_description": None,
    "short_description_spoken": "Ручная озвучка музея.",
    "raw_history": None,
    "images": [
        {"id": 80, "url": PHOTO, "is_primary": True},
        {"id": 81, "url": "https://cdn.example/exhibits/renessans/02.jpg", "is_primary": False},
    ],
}


def _card(**overrides) -> dict:
    card = {**WIPED, "images": [dict(img) for img in WIPED["images"]]}
    card.update(overrides)
    return card


# ── Стадия image ────────────────────────────────────────────────────────────────────────────
def test_image_stage_takes_url_from_primary_gallery_row():
    plan = restore.plan_image_stage([_card()])
    assert [(r.exhibit_id, r.field_name, r.value, r.source) for r in plan.restores] == [
        (7, "image_url", PHOTO, restore.SOURCE_GALLERY)
    ]
    assert plan.reviews == []


def test_image_stage_is_idempotent():
    """После применения image_url непуст — второй прогон не шлёт ни одного PATCH."""
    assert restore.plan_image_stage([_card(image_url=PHOTO)]).restores == []


def test_image_stage_ignores_card_without_gallery():
    """Пустой image_url при пустой галерее — не след PUT-а, а просто не загруженное фото.

    Карточек без фото в каталоге много; попади они в «требует глаз», отчёт стал бы
    нечитаемым, а отпечаток поломки (расхождение с галереей) в нём бы утонул.
    """
    plan = restore.plan_image_stage([_card(images=[])])
    assert plan.restores == [] and plan.reviews == []


def test_image_stage_skips_card_without_primary():
    """Ни одного первичного фото — восстанавливать не из чего, это работа человека."""
    card = _card()
    for img in card["images"]:
        img["is_primary"] = False
    plan = restore.plan_image_stage([card])
    assert plan.restores == []
    assert len(plan.reviews) == 1 and plan.reviews[0].exhibit_id == 7


def test_image_stage_skips_card_with_two_primaries():
    """Два первичных — угадывать главное фото за музей мы не будем."""
    card = _card()
    card["images"][1]["is_primary"] = True
    plan = restore.plan_image_stage([card])
    assert plan.restores == []
    assert "первичных фото" in plan.reviews[0].reason


# ── Стадия description: порядок источников ──────────────────────────────────────────────────
def test_description_prefers_own_raw_history():
    """Проза из самой карточки достовернее любого внешнего источника."""
    card = _card(raw_history="Последний пасхальный подарок Александра III.\n\nСправочно — …")
    site = {"pasxalnoe-yajczo-shkatulka-renessans": {"raw_history": "Текст с сайта.", "material": "Агат"}}
    plan = restore.plan_description_stage([card], site_lookup=site)
    description = [r for r in plan.restores if r.field_name == "short_description"][0]
    assert description.source == restore.SOURCE_RAW_HISTORY
    assert description.value == "Последний пасхальный подарок Александра III."
    # Материалов в raw_history нет отдельным полем — их добираем с сайта.
    material = [r for r in plan.restores if r.field_name == "material"][0]
    assert (material.value, material.source) == ("Агат", restore.SOURCE_SITE)


def test_description_falls_back_to_site_then_seed():
    site = {"pasxalnoe-yajczo-shkatulka-renessans": {"raw_history": "Абзац с сайта.\n\nВторой абзац."}}
    from_site = restore.plan_description_stage([_card()], site_lookup=site).restores[0]
    assert (from_site.source, from_site.value) == (restore.SOURCE_SITE, "Абзац с сайта.")

    seed = {WIPED["label_slug"]: {"short_description": "Текст из сида.", "material": "Золото, агат"}}
    plan = restore.plan_description_stage([_card()], seed_lookup=seed)
    assert [(r.field_name, r.value, r.source) for r in plan.restores] == [
        ("short_description", "Текст из сида.", restore.SOURCE_SEED),
        ("material", "Золото, агат", restore.SOURCE_SEED),
    ]


def test_description_without_any_source_goes_to_report():
    """Заполнять нечем — карточка идёт списком музею, а не получает сочинённый текст."""
    plan = restore.plan_description_stage([_card()])
    assert plan.restores == []
    assert len(plan.reviews) == 1 and "заполнять нечем" in plan.reviews[0].reason


def test_description_stage_does_not_overwrite_existing_text():
    card = _card(short_description="Живое описание.", material="Золото")
    seed = {WIPED["label_slug"]: {"short_description": "Текст из сида.", "material": "Агат"}}
    assert restore.plan_description_stage([card], seed_lookup=seed).restores == []


def test_site_text_is_not_truncated_with_ellipsis():
    """Обрезка по 400 символам с «…» — та самая поломка, которую чинит restore_descriptions.py."""
    long_paragraph = "Слово " * 200
    site = {"pasxalnoe-yajczo-shkatulka-renessans": {
        "raw_history": long_paragraph, "short_description": long_paragraph[:397] + "…",
    }}
    value = restore.plan_description_stage([_card()], site_lookup=site).restores[0].value
    assert not value.endswith("…") and len(value) > 400


# ── Гард озвучки и слаг сайта ───────────────────────────────────────────────────────────────
def test_patch_body_always_carries_spoken_guard():
    """Без явного short_description_spoken бэкенд перегенерировал бы озвучку через LLM (E15)."""
    card = _card()
    body = restore.patch_body(
        [restore.Restore(7, card["name"], "short_description", "Текст.", restore.SOURCE_SEED)], card,
    )
    assert body == {"short_description": "Текст.", "short_description_spoken": "Ручная озвучка музея."}


def test_patch_body_passes_null_spoken_explicitly():
    """Пустую озвучку тоже шлём явно — молчание бэкенд трактует как «сгенерируй заново»."""
    body = restore.patch_body(
        [restore.Restore(7, "…", "short_description", "Текст.", restore.SOURCE_SEED)],
        _card(short_description_spoken=None),
    )
    assert body["short_description_spoken"] is None


def test_patch_body_without_description_has_no_spoken():
    """Стадия image озвучки не касается — лишнее поле в теле только мешало бы откату."""
    body = restore.patch_body([restore.Restore(7, "…", "image_url", PHOTO, restore.SOURCE_GALLERY)], _card())
    assert body == {"image_url": PHOTO}


@pytest.mark.parametrize("card, expected", [
    ({"source_url": "https://fabergemuseum.ru/kollekczii/shedevryi-kollekczii/yajczo-kurochka"}, "yajczo-kurochka"),
    ({"label_slug": "faberge_pasxalnoe_yajczo_shkatulka_renessans"}, "pasxalnoe-yajczo-shkatulka-renessans"),
    ({}, None),
])
def test_site_slug(card, expected):
    """Слаг сайта: в БД он с префиксом импорта и подчёркиваниями, на сайте — через дефис."""
    assert restore.site_slug(card) == expected


# ── Разбор сида ─────────────────────────────────────────────────────────────────────────────
def test_seed_parser_reads_multiline_values():
    """Описания в сиде многострочные и с удвоенными апострофами — split по запятым тут не годится."""
    sql = (
        "INSERT INTO exhibits (id, label_slug, name, material, short_description, raw_history) VALUES\n"
        " (7, 'slug_a', 'Яйцо «Ренессанс»', 'Золото, Агат', 'Описание, с запятой.', 'Фирма: Фаберже\n"
        "Мастер: Перхин'),\n"
        " (8, 'slug_b', 'Алмазы-''розы''', NULL, 'Второе.', NULL);\n"
    )
    parsed = restore.parse_seed_exhibits(sql)
    assert set(parsed) == {"slug_a", "slug_b"}
    assert parsed["slug_a"]["short_description"] == "Описание, с запятой."
    assert parsed["slug_a"]["raw_history"].splitlines()[1] == "Мастер: Перхин"
    assert parsed["slug_b"]["name"] == "Алмазы-'розы'"
    assert parsed["slug_b"]["material"] is None


def test_seed_parser_reads_real_seed_file():
    """Сторож формата: сид в репозитории должен разбираться, иначе источник 3 молча пуст."""
    seed = restore.parse_seed_exhibits(open(restore.SEED_FILE, encoding="utf-8").read())
    assert len(seed) >= 10
    card = seed["faberge_pasxalnoe_yajczo_shkatulka_renessans"]
    assert card["name"] == "Пасхальное яйцо «Ренессанс»"
    assert card["short_description"].startswith("Последний пасхальный подарок")


# ── CLI ─────────────────────────────────────────────────────────────────────────────────────
def test_cli_defaults_are_dry_run_and_safe_stage():
    """Умолчания — сухой прогон и стадия image: описания без согласования не применяем."""
    args = restore.parse_args([])
    assert args.apply is False
    assert args.stage == restore.STAGE_IMAGE


def test_cli_rejects_unknown_stage():
    with pytest.raises(SystemExit):
        restore.parse_args(["--stage", "everything"])


def test_cli_parses_ids_and_apply():
    args = restore.parse_args(["--stage", "description", "--apply", "--ids", "7,13, 28"])
    assert (args.stage, args.apply) == (restore.STAGE_DESCRIPTION, True)
    assert restore._parse_ids(args.ids) == [7, 13, 28]


def test_cli_ids_must_be_numbers():
    with pytest.raises(SystemExit):
        restore._parse_ids("7,ренессанс")


def test_cli_rejects_rollback_mixed_with_scan_keys():
    """Откат и разбор каталога — разные прогоны; смешать их значит откатить не то."""
    with pytest.raises(SystemExit):
        restore.parse_args(["--rollback", "rollback.json", "--ids", "7"])


def test_cli_sleep_defaults_to_the_scraper_pause():
    """Пауза между запросами к сайту музея — не «когда вспомним», а значение по умолчанию.

    0.3 — та же пауза, что у `scrape_faberge.py`, который эти страницы уже качал.
    """
    assert restore.parse_args([]).sleep == 0.3
    assert restore.parse_args(["--sleep", "1.5"]).sleep == 1.5


# ── Откат (З2: рапорт об успехе должен быть правдой) ────────────────────────────────────────
def _rollback_file(items: list) -> str:
    path = os.path.join(tempfile.mkdtemp(), "rollback.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"stage": restore.STAGE_IMAGE, "items": items}, fh, ensure_ascii=False)
    return path


IMAGE_ITEM = {
    "exhibit_id": 7,
    "exhibit_name": "Пасхальное яйцо-шкатулка «Ренессанс»",
    "before": {"image_url": None},
    "after": {"image_url": PHOTO},
}


class RollbackApi:
    """Фейковый бэкенд отката: карточка в памяти + управляемое поведение PATCH."""

    def __init__(self, card: dict, patch=None) -> None:
        self.card = dict(card)
        self.patch = patch                        # None — обычный PATCH, пишущий присланное
        self.calls: list = []

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        if method == "GET":
            return 200, dict(self.card)
        if self.patch is not None:
            return self.patch(self.card, body)
        self.card.update(body)
        return 200, dict(self.card)

    def patches(self) -> list:
        return [c for c in self.calls if c[0] == "PATCH"]


def _run_rollback(api, path: str, apply: bool):
    saved, buffer = restore.api, io.StringIO()
    restore.api = api
    try:
        with contextlib.redirect_stdout(buffer):
            errors = restore.run_rollback(path, apply)
    finally:
        restore.api = saved
    return errors, buffer.getvalue()


def test_rollback_returns_the_previous_value():
    api = RollbackApi(_card(image_url=PHOTO))
    errors, out = _run_rollback(api, _rollback_file([IMAGE_ITEM]), apply=True)
    assert errors == 0
    assert api.card["image_url"] is None
    assert "Итог отката: возвращено карточек 1, пропущено полей 0, ошибок 0" in out


def test_rollback_dry_run_sends_nothing_and_says_so():
    """Сухой прогон печатал «Возвращено карточек: 1», не отправив ни одного запроса."""
    api = RollbackApi(_card(image_url=PHOTO))
    errors, out = _run_rollback(api, _rollback_file([IMAGE_ITEM]), apply=False)
    assert errors == 0 and api.patches() == []
    assert "будет возвращено" in out
    assert "Итог отката" not in out                 # «возвращено» без запросов не пишем


def test_rollback_reports_a_silent_no_op_as_an_error():
    """Гвоздь З2: бэкенд отвечает 200, а значение прежнее — это НЕ откат.

    Ровно так и было у стадии image: файл отката хранит `image_url: null`, откат слал
    `PATCH {"image_url": null}`, бэкенд возвращал URL из галереи и отдавал 200. В БД не
    менялось ничего, а скрипт печатал «Возвращено карточек: N».
    """
    def restores_from_gallery(card, body):
        return 200, {**card, "image_url": PHOTO}    # проигнорировал присланный null

    api = RollbackApi(_card(image_url=PHOTO), patch=restores_from_gallery)
    errors, out = _run_rollback(api, _rollback_file([IMAGE_ITEM]), apply=True)
    assert errors == 1
    assert "НЕ ОТКАТИЛОСЬ id=7" in out
    assert "возвращено карточек 0" in out and "ошибок 1" in out


def test_rollback_counts_a_failed_request_as_an_error():
    """Провал запроса раньше попадал в «возвращено»: счётчик рос до самого PATCH."""
    api = RollbackApi(_card(image_url=PHOTO), patch=lambda card, body: (502, {"detail": "прод прилёг"}))
    errors, out = _run_rollback(api, _rollback_file([IMAGE_ITEM]), apply=True)
    assert errors == 1
    assert "ОШИБКА отката id=7" in out
    assert "возвращено карточек 0" in out


def test_rollback_skips_a_manual_edit_and_counts_it_separately():
    """Правку, сделанную после прогона, не трогаем — и показываем её отдельным числом."""
    api = RollbackApi(_card(image_url="https://cdn.example/выбрано-вручную.jpg"))
    errors, out = _run_rollback(api, _rollback_file([IMAGE_ITEM]), apply=True)
    assert errors == 0 and api.patches() == []
    assert "разбирайтесь руками" in out
    assert "возвращено карточек 0, пропущено полей 1" in out


def test_rollback_is_idempotent():
    """Поле уже равно исходному — молча пропускаем, поэтому откат можно повторять."""
    api = RollbackApi(_card(image_url=None))
    errors, out = _run_rollback(api, _rollback_file([IMAGE_ITEM]), apply=True)
    assert errors == 0 and api.patches() == []
    assert "возвращено карточек 0, пропущено полей 0, ошибок 0" in out


def test_rollback_body_decides_what_to_return():
    """Чистое ядро отката: что вернём и что пропустим — без сети."""
    body, skipped = restore.rollback_body(IMAGE_ITEM, _card(image_url=PHOTO))
    assert body == {"image_url": None} and skipped == []
    body, skipped = restore.rollback_body(IMAGE_ITEM, _card(image_url="https://cdn.example/чужое.jpg"))
    assert body == {} and skipped == ["image_url"]
    body, skipped = restore.rollback_body(IMAGE_ITEM, _card(image_url=None))
    assert body == {} and skipped == []              # уже как было


def test_unrolled_fields_checks_the_body_not_the_status():
    """«Поля нет в ответе» и «пришло не то» — одинаково не успех."""
    assert restore.unrolled_fields({"image_url": None}, {"image_url": None}) == []
    assert restore.unrolled_fields({"image_url": None}, {"image_url": PHOTO}) == ["image_url"]
    assert restore.unrolled_fields({"image_url": None}, {"id": 7}) == ["image_url"]
    assert restore.unrolled_fields({"image_url": None}, "502 Bad Gateway") == ["image_url"]


# ── Сайт музея: пауза и честность отчёта (З7) ───────────────────────────────────────────────
def test_load_site_pauses_between_requests(monkeypatch):
    """Прогон без --ids — сотни страниц подряд; сайт музея чужой и живой."""
    slept: list = []
    monkeypatch.setattr(restore.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(restore.scrape_faberge, "fetch", lambda slug: f"<html>{slug}</html>")
    monkeypatch.setattr(restore.scrape_faberge, "parse", lambda slug, html: {"short_description": slug})

    records = [_card(id=i, source_url=f"https://fabergemuseum.ru/kollekczii/x/slug-{i}") for i in (1, 2, 3)]
    lookup, failed = restore.load_site(records, sleep=0.3)
    assert set(lookup) == {"slug-1", "slug-2", "slug-3"} and failed == set()
    assert slept == [0.3, 0.3]                        # пауза между запросами, но не перед первым


def test_load_site_collects_failures_and_sums_them_up(monkeypatch):
    """Неудачи не растворяются в строчках лога: их считают и передают дальше."""
    monkeypatch.setattr(restore.time, "sleep", lambda s: None)

    def flaky(slug):
        if slug == "slug-2":
            raise OSError("429 Too Many Requests")
        return f"<html>{slug}</html>"

    monkeypatch.setattr(restore.scrape_faberge, "fetch", flaky)
    monkeypatch.setattr(restore.scrape_faberge, "parse", lambda slug, html: {"short_description": slug})

    records = [_card(id=i, source_url=f"https://fabergemuseum.ru/kollekczii/x/slug-{i}") for i in (1, 2, 3)]
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        lookup, failed = restore.load_site(records, sleep=0)
    assert failed == {"slug-2"} and set(lookup) == {"slug-1", "slug-3"}
    assert "не скачалось 1 из 3" in buffer.getvalue()


def test_load_site_retries_once_before_giving_up(monkeypatch):
    """Разовая сетевая ошибка не должна отправлять живую карточку в отчёт «текста нет»."""
    monkeypatch.setattr(restore.time, "sleep", lambda s: None)
    attempts: list = []

    def once_failing(slug):
        attempts.append(slug)
        if len(attempts) == 1:
            raise OSError("connection reset")
        return f"<html>{slug}</html>"

    monkeypatch.setattr(restore.scrape_faberge, "fetch", once_failing)
    monkeypatch.setattr(restore.scrape_faberge, "parse", lambda slug, html: {"short_description": slug})

    with contextlib.redirect_stdout(io.StringIO()):
        lookup, failed = restore.load_site([_card(source_url="https://fabergemuseum.ru/k/slug-1")], sleep=0)
    assert failed == set() and set(lookup) == {"slug-1"}
    assert len(attempts) == 2


def test_report_tells_a_dead_site_from_a_missing_source():
    """Отчёт музей разбирает руками: «источник не ответил» нельзя выдавать за «текста нет».

    Иначе карточка с живым описанием на сайте попадёт в список «заполнять нечем», и человек
    вычеркнет её из работы, поверив отчёту.
    """
    card = _card(raw_history=None, short_description=None, material=None,
                 source_url="https://fabergemuseum.ru/kollekczii/x/slug-1")

    unreachable = restore.plan_description_stage([card], unreachable={"slug-1"})
    assert unreachable.reviews[0].reason == restore.REVIEW_SITE_DOWN
    assert "НЕ ОТВЕТИЛА" in unreachable.reviews[0].reason

    missing = restore.plan_description_stage([card], unreachable=set())
    assert missing.reviews[0].reason == restore.REVIEW_NO_SOURCE


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
