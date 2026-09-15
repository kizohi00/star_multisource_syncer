from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True)
class SourceChapterSnapshot:
    source_chapter_key: str
    source_url: str
    label: str
    number: Decimal | None = None
    title: str | None = None
    published_at: datetime | None = None
    access_status: str = "unknown"


@dataclass(frozen=True)
class SourcePageSnapshot:
    source_page_key: str
    page_order: int
    image_url: str
    width: int | None = None
    height: int | None = None


@dataclass(frozen=True)
class SourceWorkSnapshot:
    source_key: str
    source_work_key: str
    source_url: str
    title: str
    alternative_titles: tuple[str, ...] = ()
    summary: str | None = None
    cover_url: str | None = None
    tags: tuple[str, ...] = ()
    author_names: tuple[str, ...] = ()
    painter_names: tuple[str, ...] = ()
    publisher_name: str | None = None
    type_name: str | None = None
    chapters: tuple[SourceChapterSnapshot, ...] = ()
    payload: dict = field(default_factory=dict)


@dataclass(frozen=True)
class LatestFeedSnapshot:
    source_key: str
    fetched_at: datetime
    works: tuple[SourceWorkSnapshot, ...]
    next_cursor: str | None = None
    page: int = 1
    has_more: bool | None = None


@dataclass(frozen=True)
class SourceChapterFrontier:
    """Small read-only representation used to stop latest-feed discovery."""

    source_chapter_key: str
    number: Decimal | None = None
    title: str | None = None


@dataclass(frozen=True)
class SourceWorkFrontier:
    source_work_key: str
    chapters: tuple[SourceChapterFrontier, ...] = ()


@dataclass(frozen=True)
class CanonicalSeries:
    id: int
    title: str
    summary: str | None = None
    cover: str | None = None
    author_name: str | None = None
    type_name: str | None = None
    alternative_titles: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    painter_name: str | None = None
    publisher_name: str | None = None
    story_status: str | None = None
    translation_status: str | None = None
    is_oneshot: bool = False
    release_date: object | None = None
    chapter_count: int = 0
    latest_chapter_number: Decimal | None = None
    latest_chapter_at: datetime | None = None


@dataclass(frozen=True)
class MatchResult:
    status: str
    canonical_series_id: int | None
    score: float
    margin: float
    evidence: dict
