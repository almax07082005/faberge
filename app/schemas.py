"""Pydantic-схемы запросов/ответов (зеркало openapi.yaml)."""
from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Annotated, Any, Dict, List, Literal, Optional

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, computed_field, model_validator

from .config import settings
# Обрезка по границе предложения нужна прямо в схеме (превью описания зала).
# Цикла импортов нет: config тянет только stdlib и pydantic_settings, а
# text_normalize — только re/dataclasses/typing, ни БД, ни сети.
from .services.text_normalize import shorten_to_sentence


# Датировка экспоната — строка как в путеводителе («1899–1903», «конец XIX века»).
# До 17.08.2026 поле было числом; int принимаем и приводим к строке, чтобы старые
# клиенты (фронт до обновления типов, разовые скрипты) не ловили 422 на переходе.
# Никакой валидации «это должен быть год» нет сознательно — датировка бывает любой.
YearCreated = Annotated[Optional[str], BeforeValidator(lambda v: str(v) if isinstance(v, int) else v)]


# ── Система ──────────────────────────────────────────────────────────────────
class HealthStatus(BaseModel):
    status: str = "ok"
    version: str
    time: Optional[datetime] = None
    dependencies: Dict[str, str] = Field(default_factory=dict)


# ── Залы ─────────────────────────────────────────────────────────────────────
# Ближе трети лимита граница фразы уже не считается подходящей: иначе превью
# выродится в огрызок вроде «Зал открыт в 2013 году.» рядом с кнопкой
# «Подробнее». Порог именно здесь, а не в настройке: музею нужен один понятный
# рычаг «сколько текста видно», а качество разреза — наше внутреннее правило.
# В промпте гида порог другой (0.5, дефолт shorten_to_sentence) — там цена
# ошибки обратная, см. докстринг функции.
_HALL_PREVIEW_MIN_RATIO = 1 / 3


class Hall(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    # null — у зала нет номера («Вне постоянной экспозиции»): подпись строится
    # только из названия, без «зал № …».
    hall_number: Optional[int] = None
    name: Optional[str] = None
    description: Optional[str] = None
    level: Optional[int] = None
    cover_image_url: Optional[str] = None
    is_temporary: bool = False            # зал временной выставки (vs основная экспозиция)
    is_service: bool = False              # служебная запись: в публичной выдаче не появляется
    sort_order: int = 0                   # порядок вывода в каталоге/админке (drag-n-drop, C11)
    showcase_count: Optional[int] = None
    exhibit_count: Optional[int] = None

    # П. I-3 баг-репорта 31.08.2026: «сократить видимую часть текста о зале,
    # добавить опцию раскрытия полного текста». description остаётся ПОЛНЫМ и на
    # месте — превью только добавляется, старые клиенты правки не замечают.
    # Поля вычисляемые, а не обычные, потому что sch.Hall собирается в четырёх
    # местах (crud.to_hall, crud.get_map, crud.get_hall и через to_hall в
    # crud.search — три из них дублируют друг друга руками), и пятое, которое
    # кто-нибудь допишет завтра, молча отдало бы null рядом с полным описанием.
    # Колонки в БД тоже нет сознательно: превью — способ ПОКАЗАТЬ description, а
    # не второй текст, который админ ведёт руками и который разъедется с
    # описанием после первой же правки в админке.
    @computed_field  # type: ignore[prop-decorator]
    @property
    def description_preview(self) -> Optional[str]:
        """Видимая часть текста о зале: начало description по границе предложения."""
        if self.description is None:
            return None
        return shorten_to_sentence(
            self.description,
            settings.hall_description_preview_chars,
            min_ratio=_HALL_PREVIEW_MIN_RATIO,
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def description_has_more(self) -> bool:
        """Есть ли что раскрывать кнопкой «Подробнее о зале».

        Сравниваем с ``.strip()``, потому что обрезка сама стрипает вход: иначе
        описание с хвостовым пробелом вечно показывало бы кнопку в никуда.
        """
        return (self.description_preview or "") != (self.description or "").strip()


class HallBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    hall_number: Optional[int] = None
    name: Optional[str] = None


class Showcase(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    hall_id: int
    # null — группа «не в витринах» (в путеводителе — пустой квадрат): экспонаты
    # зала, стоящие вне витрин. В зале такая группа одна и выводится последней.
    showcase_number: Optional[int] = None
    name: Optional[str] = None
    exhibit_count: Optional[int] = None


class ShowcaseBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    showcase_number: Optional[int] = None
    # Название витрины. Раньше в карточку не доезжало вовсе: музей просил «номер
    # витрины», и краткая ссылка отдавала один номер. С 31.08.2026 расположение
    # показывается готовой строкой, и заполнять ExhibitLocation.showcase_name
    # без этого поля нечем.
    name: Optional[str] = None


class HallDetail(Hall):
    showcases: Optional[List[Showcase]] = None


class MapHall(Hall):
    showcases: List[Showcase] = Field(default_factory=list)


class MapResponse(BaseModel):
    halls: List[MapHall]


class HallListResponse(BaseModel):
    items: List[Hall]
    total: int
    limit: int
    offset: int


class ShowcaseDetail(Showcase):
    hall: Optional[HallBrief] = None
    exhibits: Optional[List["ExhibitSummary"]] = None


class ShowcaseListResponse(BaseModel):
    items: List[Showcase]
    total: int
    limit: int
    offset: int


# ── Экспонаты ────────────────────────────────────────────────────────────────
class Image(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int                              # идентификатор для DELETE /admin/exhibits/{id}/media/{image_id}
    url: str
    alt: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None
    is_primary: bool = False             # главная фотография экспоната (= exhibits.image_url)


class ExhibitLocation(BaseModel):
    """Готовое расположение предмета: и структурой, и строкой.

    Музей просил расположение ГОТОВОЙ строкой (баг-репорт 31.08.2026, п. I-2):
    сейчас фронт собирает «Зал 4 «Синяя гостиная», витрина 5» сам из трёх ручек, а
    ИИ-гид ту же фразу печатает по-своему — и две формулировки про одно место
    расходятся там, где это видит посетитель. Текст берётся из общего
    `app/services/location.py`, тем же кодом, что и ответ гида.

    Структура рядом со строкой нужна не для показа, а для ссылок: по `hall_id` и
    `showcase_id` фронт делает переход в зал и в витрину, не разбирая текст обратно.

    Объект ПРИСУТСТВУЕТ ВСЕГДА, даже когда предмет ни к чему не привязан: пустеет
    он внутрь (все поля null), а не исчезновением ключа. Иначе фронт пишет три
    уровня проверок — «ключа нет / null / пусто», — а музей просил прогнозируемости.
    """

    hall_id: Optional[int] = None
    hall_number: Optional[int] = None      # null — зал без номера («Вне постоянной экспозиции»)
    hall_name: Optional[str] = None
    showcase_id: Optional[int] = None
    showcase_number: Optional[int] = None  # null — группа «не в витринах»
    showcase_name: Optional[str] = None
    # «Зал 4 «Синяя гостиная», витрина 5» — строка для показа отдельной плашкой.
    text: Optional[str] = None
    # «в зале 4 «Синяя гостиная», витрина 5» — та же фраза для оборота
    # «Найти его можно …»; ровно её печатает ИИ-гид.
    text_in: Optional[str] = None


class ExhibitMaker(BaseModel):
    """«Фирма и мастер»: полная строка и её разобранные части.

    В базе фирма и мастер лежат в ОДНОМ поле `master_name` («Фирма К. Фаберже,
    мастер М. Перхин»), а музей просит показывать их раздельно. Колонок под них мы
    не заводим: форму записи фирм музей оставил за собой, и два поля на одну
    сущность расползаются при первой же правке в админке (тем же доводом убита
    колонка-дубль `dating`, см. docs/task-2026-08-17-year-created-string.md).

    Инвариант: `text` — это `master_name` дословно и ВСЕГДА авторитетен;
    `firm`/`master` — его подстроки, разбор эвристический
    (`catalog_line.split_maker`). Не разобралось — обе части null, а `text` на
    месте: фронту всегда есть что нарисовать, и ошибиться он не может. Обратной
    записи в БД у разбора нет.
    """

    text: Optional[str] = None    # = master_name дословно
    firm: Optional[str] = None    # «Фирма К. Фаберже» — вместе со словом-маркером
    master: Optional[str] = None  # «мастер М. Перхин»


class ExhibitSummary(BaseModel):
    """Краткое представление экспоната для списков, поиска и карусели.

    Порядок полей — тот же, что в `Exhibit` (см. его докстринг): плашка
    «зал/витрина» над названием рисуется одинаково и в каталоге, и в карточке.
    """

    model_config = ConfigDict(from_attributes=True)
    id: int
    exhibit_number: Optional[str] = None  # номер по путеводителю музея (B3): перед названием
    label_slug: Optional[str] = None
    name: str
    thumbnail_url: Optional[str] = None
    # Расположение готовой строкой — то же самое, что в карточке. Данных на плашку
    # «зал/витрина» в списке раньше не было вовсе: только hall_id, без названия зала.
    # Лишних запросов не появляется — витрина и зал уже загружены eager-ом
    # _EXHIBIT_SUMMARY (app/crud.py).
    location: ExhibitLocation = Field(default_factory=ExhibitLocation)
    # Ниже — то же самое плоскими полями, как отдавалось до 31.08.2026. Не удаляем
    # и не переименовываем: у задеплоенных клиентов на них завязана вёрстка.
    hall_id: Optional[int] = None
    showcase_id: Optional[int] = None
    # Номер витрины (null — экспонат вне витрин). Отдаём прямо в списке, чтобы
    # фронт собирал группировку «витрина → её экспонаты» из одного ответа
    # GET /halls/{id}/exhibits, без запроса на каждую витрину.
    showcase_number: Optional[int] = None
    # Датировка строкой как в путеводителе («1899–1903», «конец XIX века») — её и
    # показываем под названием. Раньше рядом жил дубль dating, а year_created был
    # числом-огрызком (нижняя граница); с 17.08.2026 поле датировки одно.
    year_created: YearCreated = None
    maker: ExhibitMaker = Field(default_factory=ExhibitMaker)
    master_name: Optional[str] = None    # legacy-дубль maker.text
    is_temporary: Optional[bool] = None  # унаследовано от зала: экспонат временной выставки


class Exhibit(BaseModel):
    """Полная карточка экспоната.

    Порядок полей задан музеем дословно (баг-репорт 31.08.2026, п. I-2):
    «название предмета, изображение, расположение (название и номер зала, номер
    витрины), год создания, фирма и мастер, материалы, техники, описание». Pydantic
    сохраняет порядок объявления и в JSON, и в openapi — значит порядок объявлений
    здесь и есть контракт, а не украшение; тест на порядок полей лежит в
    tests/test_exhibit_card.py, чтобы правка схемы не переставила его молча.

    Второе требование музея — ПРОГНОЗИРУЕМОСТЬ: набор ключей постоянен, пустое
    приходит как null, а не отсутствием ключа. Поэтому `location` и `maker` — НЕ
    Optional: это всегда объекты, которые пустеют внутрь.

    Поля `hall`, `showcase` и `master_name` дублируют содержимое `location`/`maker`
    и оставлены для уже задеплоенных клиентов — удалять или переименовывать их нельзя.
    """

    model_config = ConfigDict(from_attributes=True)
    id: int
    exhibit_number: Optional[str] = None  # номер по путеводителю музея (B3)
    label_slug: Optional[str] = None
    name: str
    image_url: Optional[str] = None
    images: List[Image] = Field(default_factory=list)
    location: ExhibitLocation = Field(default_factory=ExhibitLocation)
    hall: Optional[HallBrief] = None         # legacy-дубль location.hall_*
    showcase: Optional[ShowcaseBrief] = None  # legacy-дубль location.showcase_*
    year_created: YearCreated = None  # датировка строкой как в путеводителе («1899–1903»)
    # Место создания рядом с датой — «Дата создания И МЕСТО» со скриншота музея.
    # Заполняется бэкфиллом из каталожной строки; пусто — null, не пустая строка.
    origin_place: Optional[str] = None
    maker: ExhibitMaker = Field(default_factory=ExhibitMaker)
    master_name: Optional[str] = None  # legacy-дубль maker.text
    material: Optional[str] = None
    techniques: Optional[str] = None  # техники из хвоста каталожной строки (после «;»)
    short_description: Optional[str] = None
    video_url: Optional[str] = None       # видео экспоната (B4/C22)
    model_3d_url: Optional[str] = None
    model_3d_embed: Optional[str] = None
    audio_url: Optional[str] = None
    source_url: Optional[str] = None


class ExhibitAdmin(Exhibit):
    raw_history: Optional[str] = None
    # Версия short_description для озвучки: числа прописью в нужном падеже (E15).
    # Автогенерируется LLM при сохранении описания; админ может переопределить вручную.
    short_description_spoken: Optional[str] = None
    # Когда карточку последний раз меняли (триггер trg_exhibits_updated). Отдаём
    # только админке: без этого поля нельзя даже отсортировать каталог по времени
    # правки и понять, какие карточки испортили сегодня, а какие месяц назад.
    updated_at: Optional[datetime] = None


class ExhibitListResponse(BaseModel):
    items: List[ExhibitSummary]
    total: int
    limit: int
    offset: int


# ── Поиск ────────────────────────────────────────────────────────────────────
class SearchResponse(BaseModel):
    query: str
    halls: List[Hall]
    exhibits: List[ExhibitSummary]
    total: int


# ── Распознавание ────────────────────────────────────────────────────────────
class RecognitionCandidate(BaseModel):
    label_slug: str
    name: Optional[str] = None
    confidence: float
    exhibit_id: Optional[int] = None      # id карточки экспоната для перехода (B5/E19)
    thumbnail_url: Optional[str] = None   # миниатюра кандидата (= exhibits.image_url) (B5/E19)


class RecognitionResponse(BaseModel):
    recognized: bool
    label_slug: Optional[str] = None
    confidence: Optional[float] = None
    exhibit: Optional[Exhibit] = None
    candidates: List[RecognitionCandidate] = Field(default_factory=list)
    request_id: Optional[str] = None
    processing_ms: Optional[int] = None


# ── ИИ-гид ───────────────────────────────────────────────────────────────────
class GuideStyle(str, enum.Enum):
    engaging = "engaging"
    historical = "historical"
    short = "short"
    kids = "kids"
    expert = "expert"


class GuideContext(BaseModel):
    exhibit_id: Optional[int] = None
    label_slug: Optional[str] = None
    hall_id: Optional[int] = None


class StoryRequest(BaseModel):
    exhibit_id: Optional[int] = None
    label_slug: Optional[str] = None
    style: GuideStyle = GuideStyle.engaging
    language: str = "ru"
    include_audio: bool = False
    max_questions: int = Field(default=4, ge=0, le=6)
    # Необязательный идентификатор диалога (тот же, что в /guide/chat). Если
    # передан, подсказки под рассказом исключают вопросы, уже заданные в этой
    # сессии, и те, на которые гид отказался отвечать. Нужно из-за дословной
    # жалобы 31.08.2026, п. II-7: «надо возвращаться назад» — то есть на экран
    # рассказа, где иначе снова показывается только что заданный вопрос.
    # Старые клиенты поля не шлют, и для них ничего не меняется.
    session_id: Optional[uuid.UUID] = None


class StoryResponse(BaseModel):
    exhibit_id: Optional[int] = None
    label_slug: Optional[str] = None
    style: GuideStyle = GuideStyle.engaging
    text: str
    # Может прийти КОРОЧЕ, чем просили в `max_questions`, и это норма, а не сбой
    # бэкенда: с 31.08.2026 (пп. II-4/II-8) из пула снимаются формулировки,
    # которые музей просил не предлагать, и перефразировки одного и того же
    # вопроса (app/services/guide_style.py). Короче он бывал и раньше — модель
    # регулярно отдаёт меньше, чем просили.
    # ПУСТЫМ при `max_questions > 0` не приходит: если пул исчерпан исключениями,
    # бэкенд подставляет детерминированный запас (services/guide_questions).
    suggested_questions: List[str] = Field(default_factory=list)
    audio_url: Optional[str] = None
    model: Optional[str] = None
    generated_at: Optional[datetime] = None


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    session_id: Optional[uuid.UUID] = None
    # Контракт контекста (баг-репорт 28.07.2026, п.3):
    #   • поле не передано вовсе   — бэкенд подставит контекст, сохранённый в сессии;
    #   • `"context": {}` / `null` — явный СБРОС: контекст сессии очищается,
    #                                вопрос трактуется как общий;
    #   • заполненный объект       — заменяет контекст сессии целиком (старый
    #                                hall_id не «доклеивается»).
    context: Optional[GuideContext] = None
    message: str = Field(min_length=1)
    history: Optional[List[ChatMessage]] = None
    language: str = "ru"
    max_questions: int = Field(default=3, ge=0, le=6)
    # Явный сброс контекста без передачи самого поля `context` — для входа в общий
    # чат с главного экрана, когда клиент переиспользует прежний `session_id`.
    reset_context: bool = False


# Плашка экспоната в ответе гида (B6): фронт рисует компонент exhibit-plaque.
class ReferencedExhibit(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    exhibit_number: Optional[str] = None
    thumbnail_url: Optional[str] = None
    hall_number: Optional[int] = None
    showcase_number: Optional[int] = None
    # Откуда взялась плашка (31.08.2026, п. II-1). Строго аддитивно: старые
    # клиенты поля просто не видят.
    #   answer   — предмет назван в ответе гида («Упомянуто в ответе»);
    #   question — назван только в вопросе посетителя, ответ его не повторил
    #              («где найти яйцо „Ландыши“» → «в зале 4, витрина 5»), фронт
    #              может озаглавить блок «Вы спрашивали про»;
    #   context  — экспонат, у которого посетитель стоит; он в блоке всегда;
    #   null     — плашка построена не проверкой упоминания, а точной выборкой
    #              (детерминированные ветки «поиск по номеру» и «список залов»).
    mentioned_in: Optional[Literal["answer", "question", "context"]] = None


# Структурированный зал в ответе гида (B10): «какие залы есть».
class ReferencedHall(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    hall_number: Optional[int] = None
    name: Optional[str] = None


# Навигационный ответ «зал + витрина» (B7): «как найти экспонат».
class GuideLocation(BaseModel):
    """Навигационный ответ «зал + витрина» (B7).

    Порядок полей менять незачем — новые дописаны в конец. `text`/`text_in` берутся
    из того же `app/services/location.py`, что и `ExhibitLocation.text`: до
    31.08.2026 гид отдавал сюда одни числа, фронт склеивал фразу сам, и это было
    третье место, где формулировка расположения могла разойтись с остальными.
    """

    hall_number: Optional[int] = None
    hall_name: Optional[str] = None
    showcase_number: Optional[int] = None
    hall_id: Optional[int] = None
    showcase_id: Optional[int] = None
    text: Optional[str] = None     # «Зал 4 «Синяя гостиная», витрина 5»
    text_in: Optional[str] = None  # «в зале 4 «Синяя гостиная», витрина 5»


class ChatResponse(BaseModel):
    session_id: uuid.UUID
    answer: str
    # Как и в StoryResponse, может быть короче `max_questions`: запрещённые музеем
    # формулировки и перефразировки снимаются на выдаче (services/guide_style.py).
    # Дополнительно с 31.08.2026 (п. II-2) сюда не попадают вопросы, уже заданные
    # в этой сессии, и (п. II-3) те, на которые гид по этому экспонату уже
    # отказался отвечать. При `max_questions > 0` блок НЕ пустеет ни в одной
    # ветке диалога: поиск по номеру, список залов, контекст зала и общий чат
    # получают детерминированные наборы (см. services/guide_questions).
    # Исключение — уточняющий диалог по неуникальному номеру (B9): там в этом
    # поле лежат не вопросы, а варианты «В зале 4 «Синяя гостиная», витрина 5».
    suggested_questions: List[str] = Field(default_factory=list)
    context: Optional[GuideContext] = None
    # Экспонаты, на которые ссылается ответ (B6): плашки-ссылки на карточки.
    # С 31.08.2026 (п. II-1) сюда попадают только те, чьё название или номер
    # реально встречаются в реплике, плюс контекстный экспонат — он в блоке
    # всегда. ПУСТОЙ СПИСОК — законное состояние, а не ошибка: значит, ответ ни
    # на один предмет каталога не сослался, и блок «Упомянуто в ответе» рисовать
    # не надо. Это всегда `[]`, никогда `null`.
    referenced_exhibits: List[ReferencedExhibit] = Field(default_factory=list)
    # Залы, если вопрос про список/навигацию по залам (B10).
    referenced_halls: List[ReferencedHall] = Field(default_factory=list)
    # Где искать экспонат, если вопрос навигационный (B7).
    location: Optional[GuideLocation] = None


# ── Озвучивание ──────────────────────────────────────────────────────────────
class SpeechVoice(str, enum.Enum):
    alena = "alena"
    filipp = "filipp"
    jane = "jane"
    omazh = "omazh"
    zahar = "zahar"
    ermil = "ermil"


class AudioFormat(str, enum.Enum):
    mp3 = "mp3"
    oggopus = "oggopus"
    wav = "wav"


class SpeechRequest(BaseModel):
    text: Optional[str] = None
    exhibit_id: Optional[int] = None
    voice: SpeechVoice = SpeechVoice.alena
    format: AudioFormat = AudioFormat.mp3
    speed: float = Field(default=1.0, ge=0.1, le=3.0)
    emotion: str = "neutral"


class SpeechResponse(BaseModel):
    audio_url: str
    format: AudioFormat
    voice: SpeechVoice
    duration_ms: Optional[int] = None
    characters: Optional[int] = None
    cached: Optional[bool] = None


# ── Администрирование ────────────────────────────────────────────────────────
class HallCreate(BaseModel):
    # null — зал без номера («Вне постоянной экспозиции»).
    hall_number: Optional[int] = None
    name: Optional[str] = None
    description: Optional[str] = None
    level: Optional[int] = None
    is_temporary: bool = False
    is_service: bool = False              # служебная запись: скрыта из публичной выдачи
    # Если не указан — бэкенд ставит hall_number, а залу без номера даёт конец списка.
    sort_order: Optional[int] = None


class HallPatch(BaseModel):
    # hall_number допускает явный null — так зал становится «без номера»
    # (заказчик: «ЗАЛ № 99 — Вне постоянной экспозиции» показывать без номера).
    hall_number: Optional[int] = None
    name: Optional[str] = None
    description: Optional[str] = None
    level: Optional[int] = None
    cover_image_url: Optional[str] = None
    is_temporary: Optional[bool] = None
    is_service: Optional[bool] = None
    sort_order: Optional[int] = None

    @model_validator(mode="after")
    def _reject_null_required(self) -> "HallPatch":
        # Не-nullable в БД поля нельзя обнулять явным null (иначе IntegrityError → 409).
        for field in ("is_temporary", "is_service", "sort_order"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"Поле '{field}' не может быть null.")
        return self


class HallReorderRequest(BaseModel):
    # Список id залов в желаемом порядке (drag-n-drop). Залы переставляются в
    # рамках своих текущих позиций (slot-preserving): бэкенд берёт их нынешние
    # sort_order, сортирует и раздаёт в новом порядке. Поэтому можно переставлять
    # как весь список сразу, так и подсписок одной группы (основная/временная) —
    # позиции залов вне запроса не затрагиваются.
    hall_ids: List[int] = Field(min_length=1)


class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class ShowcaseCreate(BaseModel):
    hall_id: int
    # null — группа «не в витринах» (в путеводителе — пустой квадрат). В зале
    # допустима одна такая группа: повтор → 409.
    showcase_number: Optional[int] = None
    name: Optional[str] = None


class ShowcasePatch(BaseModel):
    # Частичное обновление витрины (B2/E14). Фронт (updateShowcase в lib/api/admin.ts)
    # шлёт любой поднабор из {hall_id, showcase_number, name}.
    hall_id: Optional[int] = None
    showcase_number: Optional[int] = None
    name: Optional[str] = None

    @model_validator(mode="after")
    def _reject_null_required(self) -> "ShowcasePatch":
        # hall_id — NOT NULL в БД: явный null это ошибка ввода (422), а не «поле не
        # передано». Иначе IntegrityError маппился бы в неверный 409.
        # name и showcase_number допускают null (в БД nullable).
        if "hall_id" in self.model_fields_set and self.hall_id is None:
            raise ValueError("Поле 'hall_id' не может быть null.")
        return self


class ExhibitCreate(BaseModel):
    showcase_id: int
    exhibit_number: Optional[str] = None  # номер по путеводителю музея (B3)
    label_slug: Optional[str] = None
    name: str
    # Датировка как в путеводителе, без валидации формата: «1899–1903» и
    # «конец XIX века» сохраняются как есть (таска 17.08.2026).
    year_created: YearCreated = None
    origin_place: Optional[str] = None  # место создания («Санкт-Петербург»), п. I-2 от 31.08.2026
    master_name: Optional[str] = None
    material: Optional[str] = None
    techniques: Optional[str] = None
    short_description: Optional[str] = None
    # Ручное переопределение озвучки (числа прописью). Если не передан —
    # бэкенд сгенерирует его из short_description через LLM (E15).
    short_description_spoken: Optional[str] = None
    raw_history: Optional[str] = None
    image_url: Optional[str] = None
    video_url: Optional[str] = None       # видео экспоната (B4/C22)
    model_3d_url: Optional[str] = None


class ExhibitUpdate(ExhibitCreate):
    """Тело PUT /admin/exhibits/{id} — тот же набор полей, что у create.

    Набор полей общий, а семантика записи — слитная (см. `crud.replace_exhibit`):
    поле, отсутствующее в теле, НЕ изменяется, а явный `null` его стирает.
    Исторически было наоборот: все необязательные поля наследуют от
    `ExhibitCreate` дефолт `None`, `model_dump()` отдавал их скопом, и неполное
    тело от админки обнуляло `image_url`, `short_description`, `material`,
    `raw_history` — 31.08.2026 музей потерял так содержимое карточки, правя у неё
    техники. Не читайте «Update = Create» как разрешение снова взять
    `model_dump()` без `exclude_unset`.

    Числа полей здесь намеренно нет: состав схемы меняется от релиза к релизу
    (`origin_place` добавлен 31.08.2026), а устаревшая цифра в докстринге хуже её
    отсутствия. Актуальный состав — `ExhibitUpdate.model_fields`.
    """


class ExhibitPatch(BaseModel):
    showcase_id: Optional[int] = None
    exhibit_number: Optional[str] = None
    label_slug: Optional[str] = None
    name: Optional[str] = None
    year_created: YearCreated = None
    origin_place: Optional[str] = None
    master_name: Optional[str] = None
    material: Optional[str] = None
    techniques: Optional[str] = None
    short_description: Optional[str] = None
    short_description_spoken: Optional[str] = None
    raw_history: Optional[str] = None
    image_url: Optional[str] = None
    video_url: Optional[str] = None
    model_3d_url: Optional[str] = None


class MediaUploadResponse(BaseModel):
    image_id: int                        # id созданной записи галереи (для DELETE .../media/{image_id})
    image_url: str
    thumbnail_url: Optional[str] = None  # сейчас совпадает с image_url — отдельная миниатюра не генерируется
    object_key: str


class AnalyticsTopItem(BaseModel):
    # id = null у элементов, у которых нет сущности в каталоге — например, у
    # «экранов выхода» (там значимо только имя типа события).
    id: Optional[int] = None
    name: Optional[str] = None
    count: int


class AnalyticsOverview(BaseModel):
    from_: Optional[str] = Field(default=None, alias="from")
    to: Optional[str] = None
    # Время последнего пересчёта агрегатов (§12). Фронт показывает «данные на …»,
    # чтобы отсутствие сегодняшних цифр не выглядело поломкой дашборда.
    updated_at: Optional[datetime] = None
    total_sessions: int = 0
    total_app_opens: int = 0
    total_recognitions: int = 0
    recognition_success_rate: float = 0.0
    total_chat_messages: int = 0
    total_audio_plays: int = 0
    top_exhibits: List[AnalyticsTopItem] = Field(default_factory=list)
    top_halls: List[AnalyticsTopItem] = Field(default_factory=list)

    model_config = ConfigDict(populate_by_name=True)


# ── Аналитика: частые/редкие вопросы (C16 + §3 ТЗ 03.08.2026) ────────────────
class AnalyticsQuestionItem(BaseModel):
    question: str                     # представитель кластера — самая частая формулировка
    count: int                        # суммарно по кластеру
    # Другие формулировки того же смысла (до 5). Музею важно видеть, КАК спрашивают.
    variants: List[str] = Field(default_factory=list)


class AnalyticsQuestions(BaseModel):
    from_: Optional[str] = Field(default=None, alias="from")
    to: Optional[str] = None
    updated_at: Optional[datetime] = None
    total_questions: int = 0          # всего реплик пользователя (role='user')
    unique_questions: int = 0         # различных формулировок (после нормализации)
    total_clusters: int = 0           # смысловых групп вопросов
    frequent: List[AnalyticsQuestionItem] = Field(default_factory=list)  # топ по убыванию
    # Редкие — кластеры, встретившиеся не чаще RARE_MAX_COUNT раз; с `frequent` не пересекаются.
    rare: List[AnalyticsQuestionItem] = Field(default_factory=list)

    model_config = ConfigDict(populate_by_name=True)


# ── Аналитика: вопросы без ответа гида (§4) ──────────────────────────────────
class AnalyticsUnansweredItem(BaseModel):
    question: str                     # представитель кластера
    count: int
    variants: List[str] = Field(default_factory=list)
    # Сколько раз каждая причина:
    # no_context | llm_refusal | llm_hedge | not_found | error.
    # `llm_hedge` (31.08.2026) — ответ по существу с оговоркой «этого точно не
    # знаю»; в отличие от `llm_refusal` он НЕ прячет вопрос из подсказок гида.
    fail_reasons: Dict[str, int] = Field(default_factory=dict)
    # Экспонаты, у карточек которых задавали эти вопросы — там и не хватает описания.
    exhibits: List[AnalyticsTopItem] = Field(default_factory=list)


class AnalyticsUnanswered(BaseModel):
    from_: Optional[str] = Field(default=None, alias="from")
    to: Optional[str] = None
    updated_at: Optional[datetime] = None
    total_unanswered: int = 0
    total_answered: int = 0
    # Доля неотвеченных среди вопросов с проставленным признаком (answered IS NOT NULL).
    unanswered_rate: float = 0.0
    # Вопросы без признака (накоплены до миграции 2026-08-03) — в расчёт доли не входят.
    unclassified: int = 0
    fail_reasons: Dict[str, int] = Field(default_factory=dict)
    items: List[AnalyticsUnansweredItem] = Field(default_factory=list)

    model_config = ConfigDict(populate_by_name=True)


# ── Аналитика: вовлечённость / длительность сессии (C17) ─────────────────────
class AnalyticsDurationBucket(BaseModel):
    label: str                        # напр. "0–1 мин", "1–5 мин"
    count: int


class AnalyticsEngagement(BaseModel):
    from_: Optional[str] = Field(default=None, alias="from")
    to: Optional[str] = None
    updated_at: Optional[datetime] = None
    # Число ВИЗИТОВ: поток событий сессии режется по неактивности дольше
    # SESSION_TIMEOUT_MINUTES (§5), поэтому визитов может быть больше, чем сессий.
    total_sessions: int = 0
    total_visits: int = 0
    avg_duration_sec: float = 0.0     # среднее от первого события визита до последнего
    median_duration_sec: float = 0.0
    max_duration_sec: float = 0.0
    avg_events_per_session: float = 0.0
    # §6 — что именно посетитель успел сделать за визит.
    avg_exhibits_per_session: float = 0.0   # уникальных exhibit_view
    avg_questions_per_session: float = 0.0  # chat_message
    sessions_with_chat: int = 0             # визитов хотя бы с одним chat_open — ВСЕГО за период
    sessions_with_questions: int = 0        # визитов хотя бы с одним chat_message — ВСЕГО за период
    sessions_with_app_open: int = 0         # визитов с app_open (фронт шлёт с 04.08.2026)
    # ── База конверсий (п.7 баг-репорта 06.08.2026) ──────────────────────────
    # Знаменатель — визиты с `app_open`; если за период он не приходил ни разу
    # (данные до 04.08.2026), базой становятся все визиты. Раньше знаменатель уже
    # переключался на app_open, а числители оставались «по всем визитам», из-за
    # чего доля могла превысить 100%. Теперь числитель считается ВНУТРИ базы,
    # а сама база отдаётся явно — иначе цифру нельзя ни проверить, ни сравнить.
    #
    # ВНИМАНИЕ при сравнении периодов: доли, посчитанные от разных баз (до и
    # после 04.08.2026), несопоставимы — сравнивать можно только при одинаковом
    # `conversion_basis`.
    conversion_basis: Literal["app_open", "all_visits"] = "all_visits"
    conversion_denominator: int = 0         # ровно то число, на которое делили
    # Числители, согласованные с базой: подмножество sessions_with_chat /
    # sessions_with_questions, попавшее в знаменатель. Отдаются, чтобы дашборд
    # не пересчитывал долю сам от «всего» и не получал те же >100%.
    chat_conversion_numerator: int = 0
    question_conversion_numerator: int = 0
    chat_conversion_rate: float = 0.0       # chat_conversion_numerator / conversion_denominator
    question_conversion_rate: float = 0.0   # question_conversion_numerator / conversion_denominator
    buckets: List[AnalyticsDurationBucket] = Field(default_factory=list)

    model_config = ConfigDict(populate_by_name=True)

    @model_validator(mode="before")
    @classmethod
    def _restore_legacy_basis(cls, data: Any) -> Any:
        """Достроить базу конверсий для отчётов, посчитанных до 06.08.2026.

        Отчёты лежат в кэше агрегатов до ANALYTICS_CACHE_TTL_MINUTES (сутки),
        поэтому сразу после выката ручка ещё отдаёт payload старого формата, где
        полей базы нет, — а знаменатель 0 рядом с готовой долей выглядел бы
        поломкой дашборда.

        Восстанавливаем НЕ прежнюю формулу, а самосогласованную картину. В старых
        записях числители посчитаны по ВСЕМ визитам — значит, и база у них все
        визиты. Прежнее правило делило их на визиты с `app_open`, отчего доля
        могла перевалить за 100% (ровно тот вопрос заказчика про знаменатель);
        поэтому доля здесь пересчитывается от `total_visits`, а сохранённое в
        кэше значение игнорируется — оно и было неверным. Отдавать сутки после
        выката «конверсию 120%» нельзя: заказчик увидит её раньше, чем ночной
        джоб перетрёт кэш. Точные цифры вернёт первый пересчёт —
        `POST /admin/analytics/rebuild` либо ночной джоб.
        """
        if isinstance(data, dict) and "conversion_denominator" not in data:
            visits = data.get("total_visits") or 0
            chat = data.get("sessions_with_chat") or 0
            questions = data.get("sessions_with_questions") or 0
            data = {
                **data,
                "conversion_basis": "all_visits",
                "conversion_denominator": visits,
                "chat_conversion_numerator": chat,
                "question_conversion_numerator": questions,
                "chat_conversion_rate": round(chat / visits, 4) if visits else 0.0,
                "question_conversion_rate": round(questions / visits, 4) if visits else 0.0,
            }
        return data


# ── Аналитика: маршрут пользователя по залам (C18) ───────────────────────────
class AnalyticsRouteHall(BaseModel):
    id: int
    name: Optional[str] = None
    count: int


class AnalyticsRouteTransition(BaseModel):
    from_hall_id: int
    from_hall_name: Optional[str] = None
    to_hall_id: int
    to_hall_name: Optional[str] = None
    count: int


class AnalyticsRoutePath(BaseModel):
    halls: List[AnalyticsRouteHall] = Field(default_factory=list)  # последовательность залов
    count: int


class AnalyticsSessionsPerDeviceBucket(BaseModel):
    label: str                        # «1 визит», «2 визита», «3+ визита»
    devices: int


class AnalyticsRoutes(BaseModel):
    from_: Optional[str] = Field(default=None, alias="from")
    to: Optional[str] = None
    updated_at: Optional[datetime] = None
    total_sessions_with_route: int = 0
    avg_halls_per_session: float = 0.0
    top_hall_visits: List[AnalyticsRouteHall] = Field(default_factory=list)
    top_entry_halls: List[AnalyticsRouteHall] = Field(default_factory=list)     # с какого зала начинают
    top_transitions: List[AnalyticsRouteTransition] = Field(default_factory=list)  # переходы A→B
    top_paths: List[AnalyticsRoutePath] = Field(default_factory=list)           # частые полные маршруты
    # §7 — где отваливаются.
    top_exit_halls: List[AnalyticsRouteHall] = Field(default_factory=list)      # последний hall_view визита
    top_exit_screens: List[AnalyticsTopItem] = Field(default_factory=list)      # тип последнего события визита
    # §7 — повторные визиты по анонимному device_id. Сессии без device_id
    # (старые данные, приватный режим) считаются одиночными устройствами.
    total_devices: int = 0
    returning_devices: int = 0                # устройств с ≥2 сессиями
    avg_sessions_per_device: float = 0.0
    sessions_per_device_hist: List[AnalyticsSessionsPerDeviceBucket] = Field(default_factory=list)

    model_config = ConfigDict(populate_by_name=True)


# ── Аналитика: статистика по экспонатам (§8) ─────────────────────────────────
class AnalyticsExhibitRow(BaseModel):
    id: int
    name: Optional[str] = None
    hall_number: Optional[int] = None
    views: int = 0
    questions: int = 0                # chat_message с этим exhibit_id
    tts_plays: int = 0
    recognitions: int = 0


class AnalyticsExhibits(BaseModel):
    from_: Optional[str] = Field(default=None, alias="from")
    to: Optional[str] = None
    updated_at: Optional[datetime] = None
    order: str = "views"              # views | questions | asc
    total_exhibits: int = 0           # всего экспонатов в каталоге
    never_viewed: int = 0             # ни одного exhibit_view за период
    items: List[AnalyticsExhibitRow] = Field(default_factory=list)

    model_config = ConfigDict(populate_by_name=True)


# ── Аналитика: качество распознавания (§9) ───────────────────────────────────
class AnalyticsRecognition(BaseModel):
    from_: Optional[str] = Field(default=None, alias="from")
    to: Optional[str] = None
    updated_at: Optional[datetime] = None
    total: int = 0
    success: int = 0
    success_rate: float = 0.0
    fallback_shown: int = 0           # props.fallback == true (показан топ-3)
    fallback_rate: float = 0.0
    fallback_converted: int = 0       # после фолбэка открыли карточку из кандидатов
    fallback_conversion_rate: float = 0.0
    failed: int = 0
    abandoned_after_fail: int = 0     # после неуспеха в визите не было содержательных событий
    abandonment_rate: float = 0.0
    retry_after_fail: int = 0         # повторная съёмка после неуспеха
    avg_confidence: float = 0.0       # среднее props.confidence по событиям, где оно есть

    model_config = ConfigDict(populate_by_name=True)


# ── Аналитика: суточный срез (§12) ───────────────────────────────────────────
class AnalyticsDailyPoint(BaseModel):
    date: str                         # ISO-дата
    metric: str
    dimension_key: str = ""
    dimension_id: Optional[int] = None
    value: float = 0.0


class AnalyticsDailySeries(BaseModel):
    from_: Optional[str] = Field(default=None, alias="from")
    to: Optional[str] = None
    updated_at: Optional[datetime] = None
    points: List[AnalyticsDailyPoint] = Field(default_factory=list)

    model_config = ConfigDict(populate_by_name=True)


class AnalyticsRebuildResult(BaseModel):
    rebuilt_reports: List[str] = Field(default_factory=list)
    daily_days: int = 0               # за сколько суток пересчитан плоский срез
    daily_rows: int = 0
    updated_at: datetime


# ── Кэш вопросов-подсказок (26.08.2026) ──────────────────────────────────────
class GuideQuestionsStatus(BaseModel):
    """Покрытие каталога кэшем вопросов: сколько карточек прогрето."""
    language: str = "ru"
    exhibits: int = 0
    cached: int = 0
    missing: int = 0                  # карточки, у которых записи кэша ещё нет


class GuideQuestionsWarmResult(BaseModel):
    """Итог порции прогрева. `cached` — карточки, где запись была свежей и LLM не звали."""
    language: str = "ru"
    processed: int = 0
    generated: int = 0
    cached: int = 0
    failed: int = 0
    status: GuideQuestionsStatus


# ── Телеметрия ───────────────────────────────────────────────────────────────
class EventType(str, enum.Enum):
    """Словарь типов событий — единственный источник правды контракта телеметрии.

    До 03.08.2026 ``type`` был свободной строкой: опечатка на фронте
    (``exhibitView``, ``hall-view``) молча писалась в БД и навсегда портила
    агрегаты — события по требованию заказчика не удаляются.
    """

    app_open = "app_open"
    hall_view = "hall_view"
    showcase_view = "showcase_view"
    exhibit_view = "exhibit_view"
    recognition = "recognition"
    chat_open = "chat_open"
    chat_message = "chat_message"
    tts_play = "tts_play"
    search_query = "search_query"
    session_end = "session_end"


# Совместимость: фронт исторически шлёт `audio_play` — это то же самое, что
# `tts_play`. Канонический тип — `tts_play`, псевдоним нормализуется на входе,
# чтобы уже накопленные данные не потерялись и не раздвоились в отчётах.
EVENT_TYPE_ALIASES: Dict[str, EventType] = {"audio_play": EventType.tts_play}

# Белый список ключей `props` по типам события (§10). Всё, чего нет в контракте,
# отбрасывается на приёме: props — свободный JSONB, и без фильтра фронт (или кто
# угодно, эндпоинт без авторизации) может положить туда user_agent, url с
# параметрами, координаты — то есть персональные данные, которых мы не храним.
EVENT_PROPS_ALLOWED: Dict[EventType, frozenset] = {
    EventType.app_open: frozenset({"entry", "qr_id"}),
    EventType.hall_view: frozenset(),
    EventType.showcase_view: frozenset(),
    EventType.exhibit_view: frozenset({"source"}),
    # `retry` — повторная съёмка после неудачной попытки; фронт знает это точно,
    # бэкенд иначе восстанавливает признак эвристикой по порядку событий.
    EventType.recognition: frozenset({"recognized", "confidence", "fallback", "candidates_count", "retry"}),
    EventType.chat_open: frozenset(),
    EventType.chat_message: frozenset({"text"}),
    EventType.tts_play: frozenset(),
    EventType.search_query: frozenset({"text", "results_count"}),
    EventType.session_end: frozenset({"reason", "last_screen"}),
}

# Ключи, которые нельзя принимать ни при каких условиях. Формально избыточно
# (работает белый список), но перечислены явно — это то, что показывают
# заказчику в docs/analytics-privacy.md.
EVENT_PROPS_DENIED = frozenset({"user_agent", "ip", "email", "phone", "url", "referrer", "geo"})

MAX_EVENTS_PER_BATCH = 50   # эндпоинт без авторизации: ограничиваем размер записи
MAX_PROPS_TEXT_LEN = 500    # текст вопроса/запроса длиннее — обрезается, не отклоняется


class Event(BaseModel):
    """Одно событие телеметрии.

    ``type`` объявлен строкой, а не ``EventType``, СОЗНАТЕЛЬНО: фронт шлёт события
    пачкой, и одна опечатка не должна ронять валидацией весь батч (потерять
    девять корректных событий из-за десятого). Допустимые значения проверяет
    ``normalize_event`` — неизвестные попадают в ``rejected`` ответа
    ``POST /telemetry/events``, остальные записываются.
    """

    type: str = Field(
        description=(
            "Тип события. Допустимо: " + ", ".join(t.value for t in EventType)
            + " (плюс устаревший псевдоним audio_play → tts_play). "
            "Событие с неизвестным типом отбрасывается, батч не отклоняется."
        ),
        examples=["exhibit_view"],
    )
    exhibit_id: Optional[int] = None
    hall_id: Optional[int] = None
    showcase_id: Optional[int] = None
    label_slug: Optional[str] = None
    # Анонимный ID устройства; если не задан на событии — берётся из батча.
    device_id: Optional[uuid.UUID] = None
    # Момент ДЕЙСТВИЯ (проставляет фронт), а не момент отправки батча.
    ts: Optional[datetime] = None
    props: Optional[Dict[str, Any]] = None


class EventBatch(BaseModel):
    session_id: Optional[uuid.UUID] = None
    device_id: Optional[uuid.UUID] = None   # значение по умолчанию для событий батча
    events: List[Event] = Field(min_length=1, max_length=MAX_EVENTS_PER_BATCH)


class EventIngestResult(BaseModel):
    accepted: int = 0
    rejected: int = 0                       # события с неизвестным `type`


def clean_event_props(event_type: EventType, props: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Оставить только ключи из контракта события и подрезать длинные тексты (§1/§10)."""
    if not props:
        return None
    allowed = EVENT_PROPS_ALLOWED.get(event_type, frozenset())
    clean: Dict[str, Any] = {}
    for key, value in props.items():
        if key not in allowed:
            continue  # в т.ч. всё из EVENT_PROPS_DENIED — его нет ни в одном белом списке
        if key == "text" and isinstance(value, str):
            value = value[:MAX_PROPS_TEXT_LEN]
        clean[key] = value
    return clean or None


def normalize_event(ev: Event) -> Optional[Event]:
    """Привести событие к контракту телеметрии.

    Возвращает копию с каноническим ``type`` и отфильтрованным ``props`` либо
    ``None``, если тип неизвестен — тогда событие отбрасывается, а остальные из
    того же батча записываются.
    """
    raw = (ev.type or "").strip()
    event_type = EVENT_TYPE_ALIASES.get(raw)
    if event_type is None:
        try:
            event_type = EventType(raw)
        except ValueError:
            return None
    return ev.model_copy(update={"type": event_type.value, "props": clean_event_props(event_type, ev.props)})


# ── Ошибки ───────────────────────────────────────────────────────────────────
class Error(BaseModel):
    detail: str


# Разрешаем отложенные ссылки (ShowcaseDetail -> ExhibitSummary).
ShowcaseDetail.model_rebuild()
