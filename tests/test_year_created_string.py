"""year_created — строка датировки (таска фронта 17.08.2026).

Поле было INT (нижняя граница диапазона, NULL у вековых датировок) и жило в паре
с дублем dating; теперь датировка одна и хранится строкой как в путеводителе.
Здесь контракт схем и то, что датировка доезжает до промпта гида:

  • admin create/patch принимают датировку как есть — «1899–1903» и
    «конец XIX века» не режутся валидацией «это должен быть год»;
  • int тоже принимается и приводится к строке: старый фронт до обновления типов
    шлёт Number, и ловить 422 на переходе он не должен;
  • поля dating в схемах больше нет — Pydantic его молча игнорирует, а не пишет;
  • рассказ гида берёт датировку из year_created напрямую (раньше — из dating с
    фолбэком на число).

Запуск:
    python -m pytest tests/test_year_created_string.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import schemas as sch  # noqa: E402
from app.services import llm  # noqa: E402


def test_admin_create_accepts_dating_strings_verbatim():
    for dating in ("1899–1903", "1880-е", "конец XIX — начало XX века", "около 1912"):
        data = sch.ExhibitCreate(showcase_id=1, name="Портсигар", year_created=dating)
        assert data.year_created == dating


def test_admin_create_coerces_legacy_int_to_string():
    """Старые клиенты (фронт до обновления, разовые скрипты) шлют год числом."""
    data = sch.ExhibitCreate(showcase_id=1, name="Портсигар", year_created=1899)
    assert data.year_created == "1899"


def test_patch_distinguishes_missing_from_null():
    """PATCH без поля не трогает датировку, PATCH с null — стирает (exclude_unset)."""
    untouched = sch.ExhibitPatch(name="Портсигар")
    assert "year_created" not in untouched.model_dump(exclude_unset=True)
    erased = sch.ExhibitPatch(year_created=None)
    assert erased.model_dump(exclude_unset=True) == {"year_created": None}


def test_dating_field_is_gone_from_the_api():
    """Поле dating выпилено (п.5 таски, вариант «а»): его нет ни во входе, ни в выдаче.
    Старый клиент, приславший dating, получает не 422, а молчаливое игнорирование."""
    for model in (sch.ExhibitSummary, sch.Exhibit, sch.ExhibitAdmin,
                  sch.ExhibitCreate, sch.ExhibitPatch):
        assert "dating" not in model.model_fields, model.__name__
    patched = sch.ExhibitPatch(year_created="1899–1903", dating="мусор")
    assert patched.model_dump(exclude_unset=True) == {"year_created": "1899–1903"}


def test_summary_serialises_dating_string():
    summary = sch.ExhibitSummary(id=1, name="Портсигар", year_created="конец XIX века")
    assert summary.year_created == "конец XIX века"


def test_story_stub_uses_year_created_as_the_dating():
    """Вводная рассказа — подпись этикетки: датировка берётся из year_created напрямую."""
    text = llm._story_stub(
        {"name": "портсигар", "year_created": "1899–1903", "master_name": "Михаил Перхин"},
        "engaging",
    )
    assert "1899–1903" in text
    assert "None" not in text


def test_story_stub_without_dating_stays_silent_about_time():
    """Пустая датировка не превращается в выдумку: ни «None», ни пустой запятой."""
    text = llm._story_stub({"name": "портсигар", "year_created": None}, "engaging")
    assert "None" not in text
    assert "Перед вами портсигар." in text


if __name__ == "__main__":
    failures = 0
    for name, func in sorted(globals().items()):
        if name.startswith("test_") and callable(func):
            try:
                func()
                print(f"ok   {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
    sys.exit(1 if failures else 0)
