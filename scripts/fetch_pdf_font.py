#!/usr/bin/env python3
"""Доставка TTF-шрифта с кириллицей для PDF-выгрузки аналитики (п. 3 баг-репорта 06.08.2026).

Заказчик сообщил, что «отчёт в формате pdf не формируется». Причина в том, что
стандартные шрифты ReportLab (Helvetica) кириллицы не содержат, и без TTF-файла
`GET /admin/analytics/export?format=pdf` честно отдаёт 503, а не лист с квадратами
(см. `app/services/analytics_export.py`).

Почему это отдельный скрипт, а не только пакет в Dockerfile. Прод развёрнут как
Yandex Cloud Function — зип-архив, а не докер-образ (`index.py` — мост API Gateway
→ ASGI). В рантайме функции нет ни apt, ни системных шрифтов, поэтому шрифт нужно
положить в сам архив. Скрипт кладёт его туда, где `analytics_export` ищет по
умолчанию: `assets/fonts/DejaVuSans.ttf`.

Сам файл в git не коммитим (см. `.gitignore`) — он добирается на сборке:

    python scripts/fetch_pdf_font.py            # перед упаковкой zip для Cloud Functions
    python scripts/fetch_pdf_font.py --force    # перекачать поверх существующего
    python scripts/fetch_pdf_font.py --dest /opt/fonts/DejaVuSans.ttf

Кладём ДВА начертания: обычное и жирное (`DejaVuSans-Bold.ttf` рядом). Жирное
необязательно — без него `analytics_export` рисует заголовки таблиц обычным
шрифтом вместо 503, — но в докер-образе пакет `fonts-dejavu-core` даёт оба, и
без второго файла PDF с прода отличался бы от PDF из контейнера.

Скрипт идемпотентен: если пригодный файл уже на месте, он ничего не качает.
Если сети нет (закрытый CI, прокси) — положите DejaVuSans.ttf руками по тому же
пути либо укажите переменную окружения `ANALYTICS_PDF_FONT_PATH` на любой другой
TTF с кириллицей; скрипт печатает обе подсказки в тексте ошибки.

Env:
  PDF_FONT_URL — переопределить источник (прямая ссылка на .ttf или на zip-архив
                 релиза), например на внутреннее зеркало без выхода в интернет.
"""
from __future__ import annotations

import argparse
import io
import os
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Optional

_ROOT = Path(__file__).resolve().parent.parent

# Официальный релиз проекта dejavu-fonts на GitHub. Версия зафиксирована, чтобы
# сборка была детерминированной: «последний релиз» может однажды приехать другим
# файлом, а шрифт в отчёте музея меняться сам по себе не должен. Лицензия DejaVu
# (Bitstream Vera) разрешает распространение и использование без ограничений.
DEFAULT_FONT_URL = (
    "https://github.com/dejavu-fonts/dejavu-fonts/releases/download/"
    "version_2_37/dejavu-fonts-ttf-2.37.zip"
)
FONT_URL = os.environ.get("PDF_FONT_URL") or DEFAULT_FONT_URL

# Путь по умолчанию — ровно тот, который `analytics_export._resolve_font_path()`
# проверяет сразу после ANALYTICS_PDF_FONT_PATH.
DEFAULT_DEST = _ROOT / "assets" / "fonts" / "DejaVuSans.ttf"

_MEMBER_NAME = "DejaVuSans.ttf"
# Жирное начертание: `analytics_export._bold_candidates()` ищет его рядом с обычным
# под именем «<имя>-Bold.ttf». Файл опциональный — потерять его не страшнее, чем
# получить заголовки обычным шрифтом, поэтому качаем best-effort и не валим прогон.
_BOLD_MEMBER_NAME = "DejaVuSans-Bold.ttf"
# DejaVuSans.ttf весит ~757 КБ. Порог отсекает не «маленький шрифт», а типовой
# мусор вместо файла: HTML-страницу ошибки прокси или оборванную закачку.
_MIN_BYTES = 200_000
# Сигнатуры sfnt: 0x00010000 — обычный TrueType (как у DejaVu), 'true' — вариант
# Apple, 'ttcf' — коллекция, 'OTTO' — OpenType с CFF-контурами (ReportLab его не
# читает, но лучше сказать об этом словами, чем отдать 503 при выгрузке).
_TTF_MAGIC = (b"\x00\x01\x00\x00", b"true", b"ttcf")
_TIMEOUT_SEC = 60


class FetchError(RuntimeError):
    """Шрифт доставить не удалось. Текст уже содержит подсказку, что делать руками."""


def _hint(dest: Path) -> str:
    return (
        f"Положите TTF с кириллицей в {dest} вручную "
        f"(официальный архив: {DEFAULT_FONT_URL}) либо укажите путь к уже имеющемуся "
        f"шрифту в переменной окружения ANALYTICS_PDF_FONT_PATH."
    )


def _check_font_bytes(blob: bytes, source: str) -> None:
    """Проверить, что скачалось именно TTF, а не страница ошибки и не обрывок."""
    if len(blob) < _MIN_BYTES:
        raise FetchError(
            f"{source}: получено всего {len(blob)} байт — это не шрифт, а обрывок или "
            f"страница ошибки (прокси/редирект). Ожидалось не меньше {_MIN_BYTES}."
        )
    if not blob.startswith(_TTF_MAGIC):
        head = blob[:4]
        extra = " (это OpenType/CFF — ReportLab такой не читает)" if head == b"OTTO" else ""
        raise FetchError(
            f"{source}: файл не похож на TrueType — первые байты {head!r}{extra}."
        )


def _download(url: str) -> bytes:
    try:
        # noqa: S310 — URL берётся из константы или PDF_FONT_URL, не из пользовательского ввода.
        with urllib.request.urlopen(url, timeout=_TIMEOUT_SEC) as response:  # noqa: S310
            return response.read()
    except urllib.error.HTTPError as exc:
        raise FetchError(f"источник ответил HTTP {exc.code} ({exc.reason}): {url}") from exc
    except urllib.error.URLError as exc:
        raise FetchError(f"нет доступа к {url}: {exc.reason}") from exc
    except OSError as exc:  # таймаут сокета, обрыв TLS
        raise FetchError(f"сбой сети при скачивании {url}: {exc}") from exc


def _extract_member(blob: bytes, url: str, member: str, required: bool = True) -> Optional[bytes]:
    """Достать `member` из релизного zip; прямую ссылку на .ttf вернуть как есть.

    `required=False` — для жирного начертания: его отсутствие в архиве (или ссылка
    сразу на .ttf, где второго файла взяться неоткуда) не повод ронять прогон.
    """
    if not blob.startswith(b"PK\x03\x04"):
        return blob if required else None
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            # Ищем по имени файла, а не по полному пути: смена версии релиза
            # меняет каталог верхнего уровня (dejavu-fonts-ttf-2.37/ttf/…).
            names = [n for n in archive.namelist() if n.rsplit("/", 1)[-1] == member]
            if not names:
                if not required:
                    return None
                raise FetchError(f"в архиве {url} нет {member}.")
            return archive.read(names[0])
    except zipfile.BadZipFile as exc:
        raise FetchError(f"архив {url} повреждён: {exc}") from exc


def _bold_dest(dest: Path) -> Path:
    """Путь жирного начертания рядом с обычным — ровно тот, который ищет
    `analytics_export._bold_candidates()`: `<имя>-Bold<расширение>`."""
    return dest.with_name(f"{dest.stem}-Bold{dest.suffix}")


def _usable(path: Path) -> bool:
    """Пригоден ли уже лежащий файл.

    Проверяем не только размер, но и сигнатуру: оборванная закачка или сохранённая
    страница прокси вполне может весить достаточно, и тогда идемпотентный запуск
    молча пропустил бы битый шрифт в сборку — то есть ровно тот 503 при выгрузке,
    ради которого скрипт и написан.
    """
    try:
        with path.open("rb") as handle:
            head = handle.read(4)
        return path.stat().st_size >= _MIN_BYTES and head.startswith(_TTF_MAGIC)
    except OSError:
        return False


def _write_atomic(dest: Path, blob: bytes) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Пишем через временный файл: прерванный запуск не должен оставить битый
    # шрифт, который потом молча уедет в сборку и обвалит выгрузку.
    tmp = dest.with_suffix(dest.suffix + ".part")
    tmp.write_bytes(blob)
    tmp.replace(dest)
    print(f"Готово: {dest} ({len(blob)} байт).")


def fetch(dest: Path, force: bool) -> bool:
    """Скачать шрифт в `dest` (и жирное начертание рядом).

    Возвращает False, если пригодные файлы уже были на месте.
    """
    bold_dest = _bold_dest(dest)
    if not force and _usable(dest) and _usable(bold_dest):
        print(f"Шрифт уже на месте: {dest} ({dest.stat().st_size} байт) и {bold_dest.name}. "
              f"Перекачать — с --force.")
        return False
    if not force and dest.is_file() and not _usable(dest):
        print(f"В {dest} лежит негодный файл ({dest.stat().st_size} байт, не похож на TTF) — перекачиваю.")

    print(f"Качаю {FONT_URL} …")
    archive = _download(FONT_URL)

    regular = _extract_member(archive, FONT_URL, _MEMBER_NAME)
    _check_font_bytes(regular or b"", FONT_URL)
    _write_atomic(dest, regular or b"")

    # Жирное — best-effort: сборка не должна падать из-за необязательного файла.
    bold = _extract_member(archive, FONT_URL, _BOLD_MEMBER_NAME, required=False)
    if bold is None:
        print(f"В источнике нет {_BOLD_MEMBER_NAME} — заголовки таблиц в PDF будут обычным начертанием.")
    else:
        try:
            _check_font_bytes(bold, f"{FONT_URL} ({_BOLD_MEMBER_NAME})")
            _write_atomic(bold_dest, bold)
        except FetchError as exc:
            print(f"Жирное начертание пропущено: {exc}")
    return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Скачать DejaVuSans.ttf для PDF-выгрузки аналитики.",
        epilog=(
            "Источник задаётся переменной окружения PDF_FONT_URL "
            f"(по умолчанию {DEFAULT_FONT_URL})."
        ),
    )
    parser.add_argument("--dest", type=Path, default=DEFAULT_DEST,
                        help=f"куда положить шрифт (по умолчанию {DEFAULT_DEST})")
    parser.add_argument("--force", action="store_true",
                        help="перекачать, даже если файл уже есть")
    args = parser.parse_args()

    target = args.dest.expanduser().resolve()
    try:
        fetch(target, args.force)
    except FetchError as exc:
        print(f"ERROR: {exc}\n{_hint(target)}", file=sys.stderr)
        sys.exit(1)
    except OSError as exc:
        print(f"ERROR: не удалось записать файл: {exc}\n{_hint(target)}", file=sys.stderr)
        sys.exit(1)
