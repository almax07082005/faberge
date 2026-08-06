FROM python:3.12-slim

WORKDIR /app

# Шрифт с кириллицей для PDF-выгрузки аналитики (п. 3 баг-репорта 06.08.2026).
# Стандартные шрифты ReportLab кириллицы не содержат, поэтому без TTF в образе
# GET /admin/analytics/export?format=pdf отдаёт 503 — заказчик видел это как
# «отчёт в формате pdf не формируется». Пакет кладёт DejaVuSans.ttf в
# /usr/share/fonts/truetype/dejavu — этот путь уже есть в списке кандидатов
# app/services/analytics_export.py, дополнительная настройка не нужна.
# Отдельным слоем ДО копирования исходников: правка кода не должна тянуть за
# собой переустановку пакета, а после чистой пересборки шрифт есть гарантированно.
RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

# Зависимости
COPY app/requirements.txt /app/app/requirements.txt
RUN pip install --no-cache-dir -r /app/app/requirements.txt

# Исходники
COPY . /app

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
