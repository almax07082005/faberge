"""Выгрузка аналитических отчётов в .xlsx и .pdf (§11 ТЗ 03.08.2026).

В ТЗ явно указаны оба формата: музей сводит цифры в Excel сам, а PDF показывает
на совещании. Отчёт раскладывается в набор секций «заголовок + таблица»
(`Section`), из которых одинаково собираются оба файла — логика раскладки одна,
меняется только рендер.

Зависимости (`openpyxl`, `reportlab`) импортируются ЛЕНИВО, внутри функций:
модуль тянется только при обращении к /admin/analytics/export, а холодный старт
функции Yandex Cloud и остальной API от них не зависят.

Кириллица в PDF. Стандартные шрифты ReportLab (Helvetica) кириллицу не
содержат — без TTF-шрифта выходит лист с квадратами. Шрифт ищется по
`ANALYTICS_PDF_FONT_PATH` и стандартным путям систем (DejaVu на Linux, Arial
Unicode на macOS); если не найден — поднимаем `ExportError` с инструкцией, а не
отдаём заведомо нечитаемый файл.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from io import BytesIO
from typing import Any, Dict, List, Optional, Sequence

from ..config import settings


class ExportError(Exception):
    """Выгрузка невозможна (нет библиотеки или шрифта). Роутер отдаёт 503."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass
class Section:
    """Одна таблица внутри отчёта: заголовок, шапка и строки."""

    title: str
    headers: List[str]
    rows: List[List[Any]] = field(default_factory=list)


REPORTS = ("overview", "questions", "unanswered", "exhibits", "routes", "recognition")

_REPORT_TITLES = {
    "overview": "Сводная аналитика",
    "questions": "Вопросы посетителей",
    "unanswered": "Вопросы без ответа гида",
    "exhibits": "Статистика по экспонатам",
    "routes": "Маршруты по залам",
    "recognition": "Качество распознавания",
}

# Человеческие подписи скалярных метрик. Ключи, которых здесь нет, в таблицу
# «Показатели» не попадают (служебные from/to/updated_at выводятся в шапке).
_METRIC_LABELS: Dict[str, str] = {
    "total_sessions": "Сессий",
    "total_visits": "Визитов",
    "total_app_opens": "Открытий приложения",
    "total_recognitions": "Распознаваний",
    "recognition_success_rate": "Доля успешных распознаваний",
    "total_chat_messages": "Вопросов гиду",
    "total_audio_plays": "Прослушиваний озвучки",
    "total_questions": "Реплик посетителей",
    "unique_questions": "Различных формулировок",
    "total_clusters": "Смысловых групп вопросов",
    "total_unanswered": "Вопросов без ответа",
    "total_answered": "Вопросов с ответом",
    "unanswered_rate": "Доля вопросов без ответа",
    "unclassified": "Без признака (до 03.08.2026)",
    "avg_duration_sec": "Средняя длительность визита, с",
    "median_duration_sec": "Медианная длительность визита, с",
    "max_duration_sec": "Максимальная длительность визита, с",
    "avg_events_per_session": "Событий за визит",
    "avg_exhibits_per_session": "Экспонатов за визит",
    "avg_questions_per_session": "Вопросов за визит",
    "sessions_with_chat": "Визитов с открытием чата",
    "sessions_with_questions": "Визитов с вопросом гиду",
    "chat_conversion_rate": "Конверсия в чат",
    "question_conversion_rate": "Конверсия в вопрос",
    "total_sessions_with_route": "Визитов с маршрутом",
    "avg_halls_per_session": "Залов за визит",
    "total_devices": "Устройств",
    "returning_devices": "Вернувшихся устройств",
    "avg_sessions_per_device": "Сессий на устройство",
    "total_exhibits": "Экспонатов в каталоге",
    "never_viewed": "Ни разу не открывали",
    "total": "Попыток распознавания",
    "success": "Успешных",
    "success_rate": "Доля успешных",
    "fallback_shown": "Показан топ-3",
    "fallback_rate": "Доля показов топ-3",
    "fallback_converted": "Открыли карточку из топ-3",
    "fallback_conversion_rate": "Конверсия топ-3",
    "failed": "Неуспешных",
    "abandoned_after_fail": "Ушли после неудачи",
    "abandonment_rate": "Доля ушедших",
    "retry_after_fail": "Повторили съёмку",
    "avg_confidence": "Средняя уверенность",
}


def _scalars(payload: Dict[str, Any]) -> Section:
    rows = [
        [label, payload[key]]
        for key, label in _METRIC_LABELS.items()
        if key in payload and not isinstance(payload[key], (list, dict))
    ]
    return Section("Показатели", ["Показатель", "Значение"], rows)


def _top_section(title: str, items: Sequence[dict], name_header: str = "Название") -> Section:
    return Section(
        title,
        ["ID", name_header, "Количество"],
        [[item.get("id"), item.get("name"), item.get("count")] for item in items],
    )


def build_sections(report: str, payload: Dict[str, Any]) -> List[Section]:
    """Разложить JSON отчёта в таблицы для выгрузки."""
    sections: List[Section] = [_scalars(payload)]

    if report == "overview":
        sections.append(_top_section("Топ экспонатов", payload.get("top_exhibits", []), "Экспонат"))
        sections.append(_top_section("Топ залов", payload.get("top_halls", []), "Зал"))

    elif report == "questions":
        for key, title in (("frequent", "Частые вопросы"), ("rare", "Редкие вопросы")):
            sections.append(
                Section(
                    title,
                    ["Вопрос", "Количество", "Другие формулировки"],
                    [
                        [item.get("question"), item.get("count"), "; ".join(item.get("variants") or [])]
                        for item in payload.get(key, [])
                    ],
                )
            )

    elif report == "unanswered":
        sections.append(
            Section(
                "Вопросы без ответа",
                ["Вопрос", "Количество", "Причины", "Экспонаты", "Другие формулировки"],
                [
                    [
                        item.get("question"),
                        item.get("count"),
                        ", ".join(f"{k}: {v}" for k, v in (item.get("fail_reasons") or {}).items()),
                        ", ".join(
                            str(e.get("name") or e.get("id")) for e in (item.get("exhibits") or [])
                        ),
                        "; ".join(item.get("variants") or []),
                    ]
                    for item in payload.get("items", [])
                ],
            )
        )

    elif report == "exhibits":
        sections.append(
            Section(
                "Экспонаты",
                ["ID", "Название", "Зал", "Просмотры", "Вопросы", "Озвучки", "Распознавания"],
                [
                    [
                        item.get("id"), item.get("name"), item.get("hall_number"),
                        item.get("views"), item.get("questions"),
                        item.get("tts_plays"), item.get("recognitions"),
                    ]
                    for item in payload.get("items", [])
                ],
            )
        )

    elif report == "routes":
        sections.append(_top_section("Посещения залов", payload.get("top_hall_visits", []), "Зал"))
        sections.append(_top_section("Залы входа", payload.get("top_entry_halls", []), "Зал"))
        sections.append(_top_section("Залы выхода", payload.get("top_exit_halls", []), "Зал"))
        sections.append(
            Section(
                "Экраны выхода",
                ["Экран", "Количество"],
                [[item.get("name"), item.get("count")] for item in payload.get("top_exit_screens", [])],
            )
        )
        sections.append(
            Section(
                "Переходы между залами",
                ["Из зала", "В зал", "Количество"],
                [
                    [
                        item.get("from_hall_name") or item.get("from_hall_id"),
                        item.get("to_hall_name") or item.get("to_hall_id"),
                        item.get("count"),
                    ]
                    for item in payload.get("top_transitions", [])
                ],
            )
        )
        sections.append(
            Section(
                "Частые маршруты",
                ["Маршрут", "Количество"],
                [
                    [
                        " → ".join(str(h.get("name") or h.get("id")) for h in item.get("halls", [])),
                        item.get("count"),
                    ]
                    for item in payload.get("top_paths", [])
                ],
            )
        )
        sections.append(
            Section(
                "Визитов на устройство",
                ["Группа", "Устройств"],
                [
                    [item.get("label"), item.get("devices")]
                    for item in payload.get("sessions_per_device_hist", [])
                ],
            )
        )

    return [s for s in sections if s.rows]


def _period_label(payload: Dict[str, Any]) -> str:
    start, end = payload.get("from") or "начало", payload.get("to") or "сегодня"
    return f"Период: {start} — {end}"


def file_name(report: str, dfrom: Optional[date], dto: Optional[date], extension: str) -> str:
    return f"faberge-{report}-{dfrom or 'all'}-{dto or 'all'}.{extension}"


# ── XLSX ─────────────────────────────────────────────────────────────────────
def to_xlsx(report: str, payload: Dict[str, Any]) -> bytes:
    """Один лист на отчёт: шапка периода, секции таблицами, числа — числами."""
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font
    except ImportError as exc:  # pragma: no cover — зависит от окружения
        raise ExportError("Для выгрузки в .xlsx нужен пакет openpyxl.") from exc

    book = Workbook()
    sheet = book.active
    sheet.title = _REPORT_TITLES.get(report, report)[:31]

    sheet.append([_REPORT_TITLES.get(report, report)])
    sheet["A1"].font = Font(bold=True, size=14)
    sheet.append([_period_label(payload)])
    sheet.append([f"Данные на: {payload.get('updated_at') or '—'}"])
    sheet.append([])

    widths: Dict[int, int] = {}

    def _track(row: Sequence[Any]) -> None:
        for index, value in enumerate(row, start=1):
            widths[index] = max(widths.get(index, 10), min(len(str(value if value is not None else "")) + 2, 60))

    for section in build_sections(report, payload):
        sheet.append([section.title])
        sheet.cell(row=sheet.max_row, column=1).font = Font(bold=True)
        sheet.append(section.headers)
        for cell in sheet[sheet.max_row]:
            cell.font = Font(bold=True)
            cell.alignment = Alignment(vertical="center", wrap_text=True)
        _track(section.headers)
        for row in section.rows:
            # Числа кладём числами: музей сводит выгрузку в Excel формулами,
            # а строковое «12» в них не считается.
            sheet.append([value if isinstance(value, (int, float)) or value is None else str(value) for value in row])
            _track(row)
        sheet.append([])

    for index, width in widths.items():
        sheet.column_dimensions[sheet.cell(row=1, column=index).column_letter].width = width

    buffer = BytesIO()
    book.save(buffer)
    return buffer.getvalue()


# ── PDF ──────────────────────────────────────────────────────────────────────
# Шрифты с кириллицей: сначала явно заданный в конфигурации, затем стандартные
# места установки в Linux-образах (DejaVu) и на macOS (Arial Unicode).
_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/Library/Fonts/DejaVuSans.ttf",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/Library/Fonts/Arial Unicode.ttf",
)
_FONT_NAME = "AnalyticsCyrillic"


def _resolve_font_path() -> str:
    candidates = [settings.analytics_pdf_font_path] if settings.analytics_pdf_font_path else []
    candidates.append(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), "assets", "fonts", "DejaVuSans.ttf"))
    candidates += list(_FONT_CANDIDATES)
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    raise ExportError(
        "Не найден шрифт с кириллицей для PDF. Укажите путь к TTF в переменной "
        "окружения ANALYTICS_PDF_FONT_PATH (например, DejaVuSans.ttf) или положите "
        "файл в assets/fonts/DejaVuSans.ttf."
    )


def to_pdf(report: str, payload: Dict[str, Any]) -> bytes:
    """Таблицы отчёта + шапка с периодом и датой формирования."""
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4, landscape
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import mm
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    except ImportError as exc:  # pragma: no cover — зависит от окружения
        raise ExportError("Для выгрузки в .pdf нужен пакет reportlab.") from exc

    if _FONT_NAME not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont(_FONT_NAME, _resolve_font_path()))

    base = getSampleStyleSheet()
    title_style = ParagraphStyle("t", parent=base["Title"], fontName=_FONT_NAME, fontSize=16)
    meta_style = ParagraphStyle("m", parent=base["Normal"], fontName=_FONT_NAME, fontSize=9)
    head_style = ParagraphStyle("h", parent=base["Heading2"], fontName=_FONT_NAME, fontSize=12)
    cell_style = ParagraphStyle("c", parent=base["Normal"], fontName=_FONT_NAME, fontSize=8, leading=10)

    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=landscape(A4),
        leftMargin=12 * mm, rightMargin=12 * mm, topMargin=12 * mm, bottomMargin=12 * mm,
        title=_REPORT_TITLES.get(report, report),
    )
    generated = datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
    story: List[Any] = [
        Paragraph(_REPORT_TITLES.get(report, report), title_style),
        Paragraph(_period_label(payload), meta_style),
        Paragraph(f"Данные на: {payload.get('updated_at') or '—'}", meta_style),
        Paragraph(f"Файл сформирован: {generated}", meta_style),
        Spacer(1, 6 * mm),
    ]

    for section in build_sections(report, payload):
        story.append(Paragraph(section.title, head_style))
        data = [[Paragraph(str(h), cell_style) for h in section.headers]]
        data += [
            [Paragraph("" if value is None else str(value), cell_style) for value in row]
            for row in section.rows
        ]
        table = Table(data, repeatRows=1, hAlign="LEFT")
        table.setStyle(
            TableStyle(
                [
                    ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#B0B0B0")),
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#EFEFEF")),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 4),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ]
            )
        )
        story += [table, Spacer(1, 5 * mm)]

    doc.build(story)
    return buffer.getvalue()
