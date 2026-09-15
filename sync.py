from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from ..domain.models import LatestFeedSnapshot, SourceChapterSnapshot, SourceWorkFrontier
from ..domain.ports import SourceAdapter
from ..infrastructure.repositories import MySqlSourceRepository
from ..matching.metadata import MetadataMatcher, normalize_title


@dataclass(frozen=True)
class PollResult:
    source_key: str
    status: str
    works: int = 0
    chapters: int = 0
    new_works: int = 0
    new_chapters: int = 0
    error: str | None = None
    pages_scanned: int = 0
    stop_reason: str | None = None


@dataclass(frozen=True)
class FeedDiscovery:
    feed: LatestFeedSnapshot
    pages_scanned: int
    known_streak: int
    stop_reason: str


class LatestFeedService:
    """Fetch feeds in parallel, then persist each source atomically."""

    def __init__(
        self,
        repository: MySqlSourceRepository,
        matcher: MetadataMatcher,
        *,
        known_work_streak: int = 5,
        max_pages_per_source: int = 100,
    ) -> None:
        if known_work_streak < 1:
            raise ValueError("known_work_streak must be at least 1")
        if max_pages_per_source < 0:
            raise ValueError("max_pages_per_source cannot be negative")
        self.repository = repository
        self.matcher = matcher
        self.known_work_streak = known_work_streak
        self.max_pages_per_source = max_pages_per_source

    def poll(self, adapters: tuple[SourceAdapter, ...], *, limit: int) -> list[PollResult]:
        # Load only titles/aliases before network discovery. Expensive fields
        # such as summaries and chapter statistics are hydrated after we know
        # which canonical IDs are plausible candidates for this feed batch.
        canonical_series = self.repository.list_canonical_series(include_metadata=False)
        self.matcher.prepare(canonical_series)
        # Register every selected source before network work so a failed fetch
        # can always be recorded without violating the source FK.
        for adapter in adapters:
            self.repository.ensure_source(adapter.key, adapter.display_name, adapter.base_url)
        frontiers = {
            adapter.key: self._load_frontier(adapter.key)
            for adapter in adapters
        }
        results: list[PollResult] = []
        discoveries: list[tuple[SourceAdapter, FeedDiscovery]] = []
        with ThreadPoolExecutor(max_workers=max(1, len(adapters)), thread_name_prefix="source-poll") as executor:
            jobs = {
                executor.submit(
                    self._discover,
                    adapter,
                    limit=limit,
                    frontier=frontiers.get(adapter.key, {}),
                ): adapter
                for adapter in adapters
            }
            for future in as_completed(jobs):
                adapter = jobs[future]
                try:
                    discovery = future.result()
                    discoveries.append((adapter, discovery))
                except Exception as exc:  # one broken source must not stop the other sources
                    message = f"{type(exc).__name__}: {exc}"
                    self.repository.mark_poll_failure(adapter.key, message)
                    results.append(PollResult(adapter.key, "failed", error=message))
            hydrate = getattr(self.repository, "hydrate_canonical_series_metadata", None)
            if callable(hydrate):
                candidate_ids: set[int] = set()
                for _, discovery in discoveries:
                    for work in discovery.feed.works:
                        candidate_ids.update(
                            self.matcher.candidate_ids_for_work(
                                work,
                                canonical_series,
                                allow_full_scan=False,
                            )
                        )
                canonical_series = hydrate(canonical_series, candidate_ids)
                self.matcher.prepare(canonical_series)
            for adapter, discovery in discoveries:
                try:
                    results.append(self._persist(adapter, discovery, canonical_series))
                except Exception as exc:  # one broken source must not stop the other sources
                    message = f"{type(exc).__name__}: {exc}"
                    self.repository.mark_poll_failure(adapter.key, message)
                    results.append(PollResult(adapter.key, "failed", error=message))
        return sorted(results, key=lambda result: result.source_key)

    def _load_frontier(self, source_key: str) -> dict[str, SourceWorkFrontier]:
        loader = getattr(self.repository, "load_source_frontier", None)
        if loader is None:
            # Keeps lightweight integrations that predate the frontier rule
            # usable; the real MySQL repository always provides this method.
            return {}
        return loader(source_key)

    def _discover(
        self,
        adapter: SourceAdapter,
        *,
        limit: int,
        frontier: dict[str, SourceWorkFrontier],
    ) -> FeedDiscovery:
        if limit < 1:
            raise ValueError("limit must be at least 1")

        works = []
        seen_work_keys: set[str] = set()
        known_streak = 0
        pages_scanned = 0
        stop_reason = "source_end"
        page = 1

        while True:
            if self.max_pages_per_source and page > self.max_pages_per_source:
                stop_reason = "max_pages"
                break

            feed, supports_pagination = self._fetch_page(adapter, page, limit=limit)
            pages_scanned = page
            page_works = [work for work in feed.works if work.source_work_key not in seen_work_keys]
            if not page_works:
                if supports_pagination and feed.has_more is not False:
                    page += 1
                    continue
                stop_reason = "empty_page"
                break

            for work in page_works:
                seen_work_keys.add(work.source_work_key)
                works.append(work)
                if self._latest_work_is_known(work, frontier.get(work.source_work_key)):
                    known_streak += 1
                    if known_streak >= self.known_work_streak:
                        stop_reason = "known_frontier"
                        return FeedDiscovery(
                            self._aggregate_feed(adapter.key, works, pages_scanned),
                            pages_scanned,
                            known_streak,
                            stop_reason,
                        )
                else:
                    known_streak = 0

                if len(works) >= limit:
                    stop_reason = "limit"
                    return FeedDiscovery(
                        self._aggregate_feed(adapter.key, works, pages_scanned),
                        pages_scanned,
                        known_streak,
                        stop_reason,
                    )

            if feed.has_more is False or not supports_pagination:
                stop_reason = "source_end"
                break
            page += 1

        return FeedDiscovery(
            self._aggregate_feed(adapter.key, works, pages_scanned),
            pages_scanned,
            known_streak,
            stop_reason,
        )

    @staticmethod
    def _fetch_page(adapter: SourceAdapter, page: int, *, limit: int) -> tuple[LatestFeedSnapshot, bool]:
        fetch_page = getattr(adapter, "fetch_latest_page", None)
        if callable(fetch_page):
            return fetch_page(page, limit=limit), True
        return adapter.fetch_latest(limit=limit), False

    @staticmethod
    def _aggregate_feed(source_key: str, works: list, pages_scanned: int) -> LatestFeedSnapshot:
        return LatestFeedSnapshot(
            source_key,
            datetime.now(timezone.utc),
            tuple(works),
            page=pages_scanned,
            has_more=False,
        )

    @staticmethod
    def _latest_work_is_known(
        work,
        frontier: SourceWorkFrontier | None,
    ) -> bool:
        if frontier is None or not work.chapters:
            return False
        latest = max(work.chapters, key=LatestFeedService._chapter_sort_key)
        known_keys = {chapter.source_chapter_key for chapter in frontier.chapters}
        if latest.source_chapter_key in known_keys:
            return True
        if latest.number is None:
            return False

        latest_title = normalize_title(latest.title)
        for known in frontier.chapters:
            if known.number != latest.number:
                continue
            known_title = normalize_title(known.title)
            if latest_title and known_title:
                if latest_title == known_title:
                    return True
            elif not latest_title and not known_title:
                return True
        return False

    @staticmethod
    def _chapter_sort_key(chapter: SourceChapterSnapshot) -> tuple[bool, Decimal, float]:
        number = chapter.number if chapter.number is not None else Decimal("-Infinity")
        published = chapter.published_at.timestamp() if chapter.published_at else float("-inf")
        return chapter.number is not None, number, published

    def _persist(self, adapter: SourceAdapter, discovery: FeedDiscovery, canonical_series: list) -> PollResult:
        feed = discovery.feed
        decisions = {}
        for work in feed.works:
            decisions[work.source_work_key] = self.matcher.decide(work, canonical_series)
        new_works, chapter_count, new_chapter_count = self.repository.persist_feed(
            feed,
            display_name=adapter.display_name,
            base_url=adapter.base_url,
            decisions=decisions,
            pages_scanned=discovery.pages_scanned,
            known_streak=discovery.known_streak,
            stop_reason=discovery.stop_reason,
        )
        return PollResult(
            adapter.key,
            "success",
            len(feed.works),
            chapter_count,
            new_works,
            new_chapter_count,
            None,
            discovery.pages_scanned,
            discovery.stop_reason,
        )
