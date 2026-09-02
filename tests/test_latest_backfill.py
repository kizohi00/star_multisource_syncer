from datetime import datetime, timezone
from decimal import Decimal

from mangastar_multisource.application.sync import LatestFeedService
from mangastar_multisource.domain.models import (
    LatestFeedSnapshot,
    SourceChapterFrontier,
    SourceChapterSnapshot,
    SourceWorkFrontier,
    SourceWorkSnapshot,
)
from mangastar_multisource.matching.metadata import MetadataMatcher


def _work(key: str, title: str, chapter_number: str) -> SourceWorkSnapshot:
    chapter = SourceChapterSnapshot(
        source_chapter_key=f"/{key}/{chapter_number}",
        source_url=f"https://example.test/{key}/{chapter_number}",
        label=f"Chapter {chapter_number}",
        number=Decimal(chapter_number),
    )
    return SourceWorkSnapshot(
        source_key="sparkmanga",
        source_work_key=f"/{key}",
        source_url=f"https://example.test/{key}",
        title=title,
        chapters=(chapter,),
    )


class FakeAdapter:
    key = "sparkmanga"
    display_name = "SparkManga"
    base_url = "https://example.test"

    def __init__(self) -> None:
        self.pages: list[int] = []

    def fetch_latest_page(self, page: int, *, limit: int) -> LatestFeedSnapshot:
        assert limit == 1
        self.pages.append(page)
        if page == 1:
            works = (_work("known", "Known Work", "10"),)
        else:
            works = (_work("historical", "Historical Work", "27"),)
        return LatestFeedSnapshot(
            self.key,
            datetime.now(timezone.utc),
            works,
            page=page,
            has_more=True,
        )


class FakeRepository:
    def __init__(self) -> None:
        self.persisted: list[tuple[int, bool]] = []
        self.completed: list[tuple[str, int, bool]] = []

    def list_canonical_series(self, *, include_metadata: bool) -> list:
        assert include_metadata is False
        return []

    def ensure_source(self, source_key: str, display_name: str, base_url: str) -> None:
        assert source_key == "sparkmanga"

    def load_source_frontier(self, source_key: str) -> dict[str, SourceWorkFrontier]:
        return {
            "/known": SourceWorkFrontier(
                "/known",
                (SourceChapterFrontier("/known/10", Decimal("10"), "Chapter 10"),),
            )
        }

    def hydrate_canonical_series_metadata(self, canonical_series: list, candidate_ids: set[int]) -> list:
        return canonical_series

    def persist_feed(self, feed, *, record_poll: bool, **kwargs):
        self.persisted.append((feed.page, record_poll))
        return (1 if not record_poll else 0, len(feed.works), len(feed.works))

    def mark_poll_failure(self, source_key: str, message: str) -> None:
        raise AssertionError(f"unexpected normal poll failure: {source_key}: {message}")

    def claim_source_backfill_page(self, source_key: str, *, interval_seconds: int) -> int:
        assert source_key == "sparkmanga"
        assert interval_seconds == 0
        return 2

    def complete_source_backfill_page(
        self,
        source_key: str,
        page: int,
        *,
        has_more: bool,
        stop_reason: str,
    ) -> int:
        self.completed.append((source_key, page, has_more))
        assert stop_reason == "backfill_page"
        return page + 1

    def fail_source_backfill_page(self, source_key: str, page: int, error: str) -> None:
        raise AssertionError(f"unexpected backfill failure: {source_key}: {page}: {error}")


def test_backfill_reaches_next_page_after_latest_frontier() -> None:
    repository = FakeRepository()
    adapter = FakeAdapter()
    service = LatestFeedService(
        repository,
        MetadataMatcher(),
        known_work_streak=1,
        max_pages_per_source=10,
        backfill_interval_seconds=0,
    )

    results = service.poll((adapter,), limit=1)

    assert adapter.pages == [1, 2]
    assert repository.persisted == [(1, True), (2, False)]
    assert repository.completed == [("sparkmanga", 2, True)]
    assert results[0].stop_reason == "known_frontier"
    assert results[0].backfill_page == 2
    assert results[0].backfill_status == "success"
    assert results[0].backfill_new_works == 1
    assert results[0].backfill_next_page == 3
