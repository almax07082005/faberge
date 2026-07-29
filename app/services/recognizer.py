"""Распознавание экспоната по фото (внешний сервис поиска YOLO+DINOv2 + стаб)."""
from __future__ import annotations

import difflib
import hashlib
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

import httpx

from ..config import settings
from . import UpstreamError

logger = logging.getLogger(__name__)


@dataclass
class RecognitionOutcome:
    recognized: bool
    label_slug: Optional[str]
    confidence: Optional[float]
    candidates: List[Tuple[str, float]] = field(default_factory=list)  # (label_slug, confidence)
    # Предсказания ML-сервиса, которые не удалось сшить с каталогом по названию:
    # (title, confidence). Роутер добирает по ним кандидатов полнотекстовым
    # поиском, чтобы при неудаче фронт показал топ-3, а не глухую ошибку (E19).
    unmatched: List[Tuple[str, float]] = field(default_factory=list)


# ── Сшивка названий ML-индекса с каталогом ───────────────────────────────────
# Сервис поиска ключует предметы по названию (title), наш каталог — по label_slug.
# Названия приходят из другого источника, поэтому расходятся в мелочах: регистр,
# «ё/е», кавычки-ёлочки против прямых, двойные пробелы, хвостовая пунктуация. При
# точном сравнении такое предсказание молча выбрасывалось и распознавание «не
# работало» при полностью исправной модели (баг-репорт 28.07.2026, п.1).
_QUOTES = "«»„“”\"'`’‘"
_PUNCT_EDGE_RE = re.compile(r"^[\s\-–—.,;:!?]+|[\s\-–—.,;:!?]+$")
_SPACES_RE = re.compile(r"\s+")


def normalize_name(name: str) -> str:
    """Ключ для сопоставления названий: регистр, «ё», кавычки и пробелы не важны."""
    text = unicodedata.normalize("NFKC", name)
    text = "".join(" " if ch in _QUOTES else ch for ch in text)
    text = text.replace("ё", "е").replace("Ё", "Е")
    text = _SPACES_RE.sub(" ", text).strip().casefold()
    return _PUNCT_EDGE_RE.sub("", text)


def build_name_index(name_to_slug: Mapping[str, str]) -> Dict[str, str]:
    """Карта нормализованное имя → label_slug (обе стороны нормализуются одинаково)."""
    index: Dict[str, str] = {}
    for name, slug in name_to_slug.items():
        key = normalize_name(name or "")
        if key and key not in index:
            index[key] = slug
    return index


def match_title(
    title: str, index: Mapping[str, str], cutoff: Optional[float] = None
) -> Tuple[Optional[str], str]:
    """Найти label_slug по названию из ML-индекса.

    Возвращает ``(slug, способ)``: ``exact`` — совпали нормализованные ключи,
    ``fuzzy`` — сошлись с точностью не ниже ``cutoff``, ``miss`` — не нашли.
    Нечёткое сопоставление добирает расхождения, которые нормализация не лечит
    (сокращения, «яйцо Ландыши» vs «Яйцо „Ландыши“»).
    """
    key = normalize_name(title or "")
    if not key:
        return None, "miss"
    slug = index.get(key)
    if slug is not None:
        return slug, "exact"
    threshold = settings.recognition_name_match_cutoff if cutoff is None else cutoff
    if threshold <= 0:
        return None, "miss"
    close = difflib.get_close_matches(key, list(index.keys()), n=1, cutoff=threshold)
    if close:
        return index[close[0]], "fuzzy"
    return None, "miss"


def _slug_from_ids(pred: Mapping, known_slugs: Sequence[str]) -> Optional[str]:
    """Фолбэк на идентификаторы предсказания, когда название не сошлось.

    Берём ``label_slug``/``slug``/``item_id`` ТОЛЬКО если значение само является
    известным нам label_slug — то есть идентификатор доказуемо стабилен и общий
    с каталогом. Числовой ``item_id`` внутреннего индекса ML-сервиса не является
    нашим id, гадать по нему нельзя (увели бы посетителя на чужой экспонат).
    """
    known = set(known_slugs)
    for key in ("label_slug", "slug", "item_id"):
        value = pred.get(key)
        if value is not None and str(value) in known:
            return str(value)
    return None


async def recognize(
    image: bytes,
    known_slugs: Sequence[str],
    hall_id: Optional[int] = None,
    top_k: int = 3,
    name_to_slug: Optional[Mapping[str, str]] = None,
) -> RecognitionOutcome:
    """Вернуть label_slug для фото.

    `known_slugs` — классы из БД (для стаба и для проверки идентификаторов).
    `name_to_slug` — карта имя→label_slug для сшивки с внешним ML-поиском (реал).
    """
    if settings.yolo_configured:
        return await _recognize_search(image, name_to_slug or {}, known_slugs, top_k)
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


async def _recognize_search(
    image: bytes, name_to_slug: Mapping[str, str], known_slugs: Sequence[str], top_k: int
) -> RecognitionOutcome:
    """Реальный вызов развёрнутого сервиса поиска по фото (YOLO + DINOv2).

    Контракт ``POST {yolo_endpoint}`` (Faberge Search API ``/search``):
        multipart {file, limit} →
        {"predictions": [{"item_id", "title", "confidence"}, ...], "found": bool}

    Сервис ключует предметы по названию (``title``), а наш каталог — по
    ``label_slug``; сшиваем ``title → label_slug`` через `name_to_slug`
    (нормализация + нечёткое сопоставление, см. `match_title`). Дубли имён в
    каталоге («Портсигар» ×12) сворачиваются в один экспонат (см.
    crud.slug_by_name) — поэтому дедуп по slug.
    """
    try:
        async with httpx.AsyncClient(timeout=settings.recognition_timeout_sec) as client:
            resp = await client.post(
                settings.yolo_endpoint,  # type: ignore[arg-type]
                files={"file": ("photo.jpg", image, "application/octet-stream")},
                data={"limit": str(top_k)},
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Сервис распознавания недоступен: %r", exc)
        raise UpstreamError("Сервис распознавания временно недоступен.") from exc

    predictions = data.get("predictions") or []
    # Диагностика прода: без сырого ответа чинить сшивку вслепую невозможно.
    logger.info(
        "recognition: получено предсказаний=%d, found=%s, каталог=%d названий, сырые=%s",
        len(predictions), data.get("found"), len(name_to_slug),
        [(p.get("item_id"), p.get("title"), p.get("confidence")) for p in predictions],
    )

    index = build_name_index(name_to_slug)
    candidates: List[Tuple[str, float]] = []
    unmatched: List[Tuple[str, float]] = []
    seen: set[str] = set()
    for pred in predictions:
        title = (pred.get("title") or "").strip()
        conf = pred.get("confidence")
        if conf is None:
            logger.warning("recognition: предсказание без confidence, отброшено: %r", pred)
            continue
        slug, how = match_title(title, index)
        if slug is None:
            slug = _slug_from_ids(pred, known_slugs)
            how = "id" if slug else "miss"
        if slug is None:
            # Раньше здесь был тихий `continue` — именно поэтому баг «распознавание
            # не работает» не было видно по логам. Теперь название видно целиком.
            logger.warning(
                "recognition: название не сшито с каталогом, предсказание отброшено: "
                "title=%r item_id=%r confidence=%r",
                title, pred.get("item_id"), conf,
            )
            unmatched.append((title, float(conf)))
            continue
        if slug in seen:
            continue
        if how != "exact":
            logger.info("recognition: название сшито способом %s: title=%r → %s", how, title, slug)
        seen.add(slug)
        candidates.append((slug, float(conf)))
        if len(candidates) >= top_k:
            break

    if not candidates:
        # Уверенность верхнего предсказания отдаём даже без сшивки — она объясняет
        # фронту и логам, что модель что-то нашла, а споткнулся каталог.
        top_conf = unmatched[0][1] if unmatched else None
        return RecognitionOutcome(False, None, top_conf, [], unmatched)
    top_slug, top_conf = candidates[0]
    recognized = top_conf >= settings.recognition_confidence_threshold
    return RecognitionOutcome(
        recognized, top_slug if recognized else None, top_conf, candidates, unmatched
    )
