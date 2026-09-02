from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from collections.abc import Sequence
from dataclasses import dataclass

from ..domain.models import MatchResult
from ..domain.ports import SourceAdapter
from ..infrastructure.repositories import MySqlSourceRepository
from ..matching.metadata import MetadataMatcher
from .auto_chapters import AutoChapterPromotionService


def _log(message: str) -> None:
    print(f"[mangastar-syncer] {message}", flush=True)


@dataclass(frozen=True)
class EnrichmentResult:
    source_key: str
    source_work_id: int
    title: str
    chapters: int
    linked_chapters: int
    ambiguous_chapters: int
    status: str
    error: str | None = None
    auto_created_series_id: int | None = None
    auto_promoted_chapters: int = 0
    auto_failed_chapters: int = 0
    fallback_chapters: int = 0


class WorkEnrichmentService:
    def __init__(
        self,
        repository: MySqlSourceRepository,
        matcher: MetadataMatcher,
        *,
        auto_create_low_confidence: bool = True,
        new_series_score_threshold: float = 0.60,
        max_workers: int = 1,
        page_workers: int = 1,
    ) -> None:
        self.repository = repository
        self.matcher = matcher
        if not 0 < new_series_score_threshold <= 1:
            raise ValueError("new_series_score_threshold must be between 0 and 1")
        if max_workers < 1:
            raise ValueError("max_workers must be at least 1")
        if page_workers < 1:
            raise ValueError("page_workers must be at least 1")
        self.auto_create_low_confidence = auto_create_low_confidence
        self.new_series_score_threshold = new_series_score_threshold
        self.max_workers = max_workers
        self.auto_chapters = AutoChapterPromotionService(repository, max_workers=page_workers)

    def enrich(
        self,
        adapters: tuple[SourceAdapter, ...],
        *,
        limit: int,
        pending_only: bool = False,
        fallback_adapters: Sequence[SourceAdapter] | None = None,
    ) -> list[EnrichmentResult]:
        _log(f"enrichment: loading pending works; limit={limit}")
        canonical_series = self.repository.list_canonical_series(include_metadata=False)
        self.matcher.prepare(canonical_series)
        hydrate = getattr(self.repository, "hydrate_canonical_series_metadata", None)
        work_items: list[tuple[SourceAdapter, dict]] = []
        for adapter in adapters:
            work_items.extend(
                (adapter, row)
                for row in self.repository.list_source_works(
                    adapter.key,
                    limit,
                    pending_only=pending_only,
                )
            )

        if not work_items:
            _log("enrichment: no pending works")
            return []

        _log(f"enrichment: queued {len(work_items)} work(s)")

        # Each job owns its matcher instance. The canonical objects are
        # immutable snapshots, while metadata hydration and persistence use a
        # separate DB connection per operation. This keeps matching decisions
        # identical while allowing slow source detail pages to overlap.
        if self.max_workers == 1 or len(work_items) == 1:
            return [
                self._enrich_one(
                    adapter,
                    row,
                    canonical_series,
                    hydrate,
                    fallback_adapters,
                    adapters,
                )
                for adapter, row in work_items
            ]

        with ThreadPoolExecutor(
            max_workers=min(self.max_workers, len(work_items)),
            thread_name_prefix="source-enrich",
        ) as executor:
            futures = [
                executor.submit(
                    self._enrich_one,
                    adapter,
                    row,
                    canonical_series,
                    hydrate,
                    fallback_adapters,
                    adapters,
                )
                for adapter, row in work_items
            ]
            return [future.result() for future in futures]

    def _enrich_one(
        self,
        adapter: SourceAdapter,
        row: dict,
        canonical_series: list,
        hydrate,
        fallback_adapters: Sequence[SourceAdapter] | None,
        selected_adapters: tuple[SourceAdapter, ...],
    ) -> EnrichmentResult:
        try:
            _log(f"enrichment: fetching details for {adapter.key} work {row['id']}")
            matcher = MetadataMatcher(
                auto_threshold=self.matcher.auto_threshold,
                margin=self.matcher.margin,
            )
            matcher.prepare(canonical_series)
            snapshot = adapter.fetch_work_details(row["source_url"])
            _log(f"enrichment: details fetched for {adapter.key} work {row['id']}")
            matching_series = canonical_series
            if callable(hydrate):
                candidate_ids = matcher.candidate_ids_for_work(
                    snapshot,
                    canonical_series,
                    allow_full_scan=False,
                )
                matching_series = hydrate(canonical_series, candidate_ids)
                matcher.prepare(matching_series)
            match, ranked = matcher.decide(snapshot, matching_series)
            if row["canonical_series_id"] is not None:
                match = MatchResult(
                    "matched",
                    int(row["canonical_series_id"]),
                    1.0,
                    1.0,
                    {"reason": "existing_source_mapping"},
                )
            source_work_id, chapter_count = self.repository.persist_enriched_work(
                snapshot,
                display_name=adapter.display_name,
                base_url=adapter.base_url,
                match=match,
                candidates=ranked,
            )
            auto_created_series_id = None
            if (
                self.auto_create_low_confidence
                and match.status == "unmatched"
                and match.score < self.new_series_score_threshold
            ):
                creator = getattr(self.repository, "auto_create_low_confidence_series", None)
                if callable(creator):
                    creation = creator(
                        source_work_id,
                        score_threshold=self.new_series_score_threshold,
                    )
                    if creation.get("status") == "created":
                        auto_created_series_id = int(creation["canonical_series_id"])
            chapter_promotion = {
                "promoted": 0,
                "failed": 0,
                "fallbacks": 0,
            }
            if (
                auto_created_series_id is not None
                or row.get("match_status") == "auto_created"
            ):
                chapter_promotion = self.auto_chapters.promote_all(
                    source_work_id,
                    fallback_adapters or selected_adapters,
                )
            chapter_links = self.repository.link_exact_chapters(source_work_id)
            _log(
                f"enrichment: completed {adapter.key} work {row['id']}; "
                f"chapters={chapter_count}; linked={chapter_links['linked']}"
            )
            return EnrichmentResult(
                adapter.key,
                source_work_id,
                snapshot.title,
                chapter_count,
                chapter_links["linked"],
                chapter_links["ambiguous"],
                "success",
                auto_created_series_id=auto_created_series_id,
                auto_promoted_chapters=int(chapter_promotion["promoted"]),
                auto_failed_chapters=int(chapter_promotion["failed"]),
                fallback_chapters=int(chapter_promotion["fallbacks"]),
            )
        except Exception as exc:
            _log(f"enrichment: failed {adapter.key} work {row['id']}; {type(exc).__name__}: {exc}")
            return EnrichmentResult(
                adapter.key,
                int(row["id"]),
                row["title_raw"],
                0,
                0,
                0,
                "failed",
                f"{type(exc).__name__}: {exc}",
            )
