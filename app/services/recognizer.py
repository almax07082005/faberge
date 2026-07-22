"""Распознавание экспоната по фото (внешний сервис поиска YOLO+DINOv2 + стаб)."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import List, Mapping, Optional, Sequence, Tuple

import httpx

from ..config import settings
from . import UpstreamError


@dataclass
class RecognitionOutcome:
    recognized: bool
    label_slug: Optional[str]
    confidence: Optional[float]
    candidates: List[Tuple[str, float]] = field(default_factory=list)  # (label_slug, confidence)


async def recognize(
    image: bytes,
    known_slugs: Sequence[str],
    hall_id: Optional[int] = None,
    top_k: int = 3,
    name_to_slug: Optional[Mapping[str, str]] = None,
) -> RecognitionOutcome:
    """Вернуть label_slug для фото.

    `known_slugs` — классы из БД (для стаба).
    `name_to_slug` — карта имя→label_slug для сшивки с внешним ML-поиском (реал).
    """
    if settings.yolo_configured:
        return await _recognize_search(image, name_to_slug or {}, top_k)
    return _recognize_stub(image, known_slugs, top_k)


def _recognize_stub(image: bytes, known_slugs: Sequence[str], top_k: int) -> RecognitionOutcome:
    """Детерминированно «распознаёт» по хэшу картинки среди известных классов."""
    if not known_slugs:
        return RecognitionOutcome(False, None, 0.0, [])
    h = int(hashlib.sha256(image).hexdigest(), 16)
    confidence = round(0.45 + (h % 55) / 100.0, 2)  # 0.45..0.99
    idx = h % len(known_slugs)
    primary = known_slugs[idx]
    if confidence >= settings.recognition_confidence_threshold:
        return RecognitionOutcome(True, primary, confidence, [(primary, confidence)])
    candidates: List[Tuple[str, float]] = []
    for i in range(min(top_k, len(known_slugs))):
        candidates.append((known_slugs[(idx + i) % len(known_slugs)], round(max(0.05, confidence - i * 0.08), 2)))
    return RecognitionOutcome(False, None, confidence, candidates)


async def _recognize_search(image: bytes, name_to_slug: Mapping[str, str], top_k: int) -> RecognitionOutcome:
    """Реальный вызов развёрнутого сервиса поиска по фото (YOLO + DINOv2).

    Контракт ``POST {yolo_endpoint}`` (Faberge Search API ``/search``):
        multipart {file, limit} →
        {"predictions": [{"item_id", "title", "confidence"}, ...], "found": bool}

    Сервис ключует предметы по названию (``title``), а наш каталог — по
    ``label_slug``; сшиваем ``title → label_slug`` через `name_to_slug`. Предсказания
    с неизвестным нам названием отбрасываем. Дубли имён в каталоге («Портсигар» ×12)
    сворачиваются в один экспонат (см. crud.slug_by_name) — поэтому дедуп по slug.
    """
    try:
        async with httpx.AsyncClient(timeout=25.0) as client:
            resp = await client.post(
                settings.yolo_endpoint,  # type: ignore[arg-type]
                files={"file": ("photo.jpg", image, "application/octet-stream")},
                data={"limit": str(top_k)},
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:  # noqa: BLE001
        raise UpstreamError("Сервис распознавания временно недоступен.") from exc

    candidates: List[Tuple[str, float]] = []
    seen: set[str] = set()
    for pred in data.get("predictions") or []:
        title = (pred.get("title") or "").strip()
        slug = name_to_slug.get(title)
        conf = pred.get("confidence")
        if not slug or conf is None or slug in seen:
            continue
        seen.add(slug)
        candidates.append((slug, float(conf)))
        if len(candidates) >= top_k:
            break

    if not candidates:
        return RecognitionOutcome(False, None, None, [])
    top_slug, top_conf = candidates[0]
    recognized = top_conf >= settings.recognition_confidence_threshold
    return RecognitionOutcome(recognized, top_slug if recognized else None, top_conf, candidates)
