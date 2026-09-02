from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Protocol

from .models import (
    CanonicalSeries,
    LatestFeedSnapshot,
    MatchResult,
    SourceChapterSnapshot,
    SourcePageSnapshot,
    SourceWorkFrontier,
    SourceWorkSnapshot,
)


class SourceAdapter(Protocol):
    key: str
    display_name: str
    base_url: str

    def fetch_latest(self, *, limit: int) -> LatestFeedSnapshot:
        """Fetch only the source's latest/update feed."""

    def fetch_latest_page(self, page: int, *, limit: int) -> LatestFeedSnapshot:
        """Fetch one page/batch of the source's latest/update feed."""

    def fetch_work_details(self, source_url: str) -> SourceWorkSnapshot: ...

    def fetch_pages(self, chapter: SourceChapterSnapshot) -> Sequence[SourcePageSnapshot]: ...


class SourceRepository(Protocol):
    def ensure_source(self, source_key: str, display_name: str, base_url: str) -> None: ...

    def upsert_work(self, snapshot: SourceWorkSnapshot, match: MatchResult) -> int: ...

    def upsert_chapters(self, source_work_id: int, snapshot: SourceWorkSnapshot) -> int: ...

    def record_candidates(self, source_work_id: int, candidates: Sequence[tuple[CanonicalSeries, float, dict]]) -> None: ...

    def mark_poll_success(self, source_key: str, fetched_count: int, new_count: int) -> None: ...

    def mark_poll_failure(self, source_key: str, message: str) -> None: ...

    def load_source_frontier(self, source_key: str) -> dict[str, SourceWorkFrontier]: ...

    def source_health(self, source_keys: Sequence[str] | None = None) -> list[dict]: ...

    def list_promotion_candidates(self, source_keys: Sequence[str] | None = None, *, limit: int = 100) -> list[dict]: ...

    def enqueue_promotion_candidates(self, candidates: Sequence[dict]) -> int: ...

    def list_promotion_queue(
        self,
        *,
        status: str = "pending",
        source_keys: Sequence[str] | None = None,
        limit: int = 100,
    ) -> list[dict]: ...

    def review_promotion_queue(
        self,
        queue_ids: Sequence[int],
        *,
        status: str,
        note: str | None = None,
    ) -> int: ...

    def apply_approved_promotions(
        self,
        *,
        source_keys: Sequence[str] | None = None,
        limit: int = 20,
    ) -> list[dict]: ...

    def auto_create_low_confidence_series(
        self,
        source_work_id: int,
        *,
        score_threshold: float = 0.60,
    ) -> dict: ...

    def list_unpromoted_source_chapters(self, source_work_id: int) -> list[dict]: ...

    def auto_promote_source_chapter(self, source_chapter_id: int) -> dict: ...

    def link_source_chapter_to_canonical(
        self,
        source_chapter_id: int,
        canonical_chapter_id: int,
    ) -> dict: ...


class CanonicalSeriesRepository(Protocol):
    def list_match_candidates(self) -> Iterable[CanonicalSeries]: ...
