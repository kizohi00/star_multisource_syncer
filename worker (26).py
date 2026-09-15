from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Event

from ..domain.ports import SourceAdapter
from .enrichment import EnrichmentResult, WorkEnrichmentService
from .sync import LatestFeedService, PollResult


def _log(message: str) -> None:
    print(f"[mangastar-syncer] {message}", flush=True)


@dataclass(frozen=True)
class WorkerCycleResult:
    started_at: datetime
    finished_at: datetime
    poll_results: tuple[PollResult, ...] = ()
    enrichment_results: tuple[EnrichmentResult, ...] = ()
    chapter_promotion_results: tuple[dict, ...] = ()
    error: str | None = None

    def as_dict(self) -> dict:
        return {
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat(),
            "poll": [result.__dict__ for result in self.poll_results],
            "enrichment": [result.__dict__ for result in self.enrichment_results],
            "chapter_promotions": list(self.chapter_promotion_results),
            "error": self.error,
        }


class MultiSourceWorker:
    """Poll latest feeds and enrich new or event-retryable source works."""

    def __init__(
        self,
        poll_service: LatestFeedService,
        enrichment_service: WorkEnrichmentService,
        adapters: tuple[SourceAdapter, ...],
        *,
        poll_limit: int,
        enrich_limit: int,
        interval_seconds: int,
        fallback_adapters: tuple[SourceAdapter, ...] | None = None,
        chapter_promotion_limit: int = 50,
    ) -> None:
        if poll_limit < 1:
            raise ValueError("poll_limit must be at least 1")
        if enrich_limit < 0:
            raise ValueError("enrich_limit cannot be negative")
        if chapter_promotion_limit < 0:
            raise ValueError("chapter_promotion_limit cannot be negative")
        if interval_seconds < 1:
            raise ValueError("interval_seconds must be at least 1")
        self.poll_service = poll_service
        self.enrichment_service = enrichment_service
        self.adapters = adapters
        self.poll_limit = poll_limit
        self.enrich_limit = enrich_limit
        self.interval_seconds = interval_seconds
        self.fallback_adapters = fallback_adapters
        self.chapter_promotion_limit = chapter_promotion_limit

    def run_once(self) -> WorkerCycleResult:
        started = datetime.now(timezone.utc)
        _log("poll started")
        poll_results = tuple(self.poll_service.poll(self.adapters, limit=self.poll_limit))
        _log(f"poll finished; source_results={len(poll_results)}")
        enrichment_results: tuple[EnrichmentResult, ...] = ()
        chapter_promotion_results: tuple[dict, ...] = ()
        if self.enrich_limit:
            _log(f"enrichment started; limit={self.enrich_limit}")
            enrich_kwargs = {
                "limit": self.enrich_limit,
                "pending_only": True,
            }
            if self.fallback_adapters is not None:
                enrich_kwargs["fallback_adapters"] = self.fallback_adapters
            enrichment_results = tuple(
                self.enrichment_service.enrich(self.adapters, **enrich_kwargs)
            )
            _log(f"enrichment finished; results={len(enrichment_results)}")
        if self.chapter_promotion_limit:
            promotion_service = getattr(self.enrichment_service, "auto_chapters", None)
            promote_pending = getattr(promotion_service, "promote_pending", None)
            if callable(promote_pending):
                _log(f"chapter promotion started; limit={self.chapter_promotion_limit}")
                promotion_adapters = self.fallback_adapters or self.adapters
                chapter_promotion_results = (
                    promote_pending(
                        promotion_adapters,
                        source_keys=tuple(adapter.key for adapter in self.adapters),
                        limit=self.chapter_promotion_limit,
                    ),
                )
                _log("chapter promotion finished")
        _log("cycle finished")
        return WorkerCycleResult(
            started_at=started,
            finished_at=datetime.now(timezone.utc),
            poll_results=poll_results,
            enrichment_results=enrichment_results,
            chapter_promotion_results=chapter_promotion_results,
        )

    def run_forever(
        self,
        *,
        stop_event: Event | None = None,
        on_cycle: Callable[[WorkerCycleResult], None] | None = None,
    ) -> None:
        event = stop_event or Event()
        while not event.is_set():
            started = datetime.now(timezone.utc)
            try:
                result = self.run_once()
            except Exception as exc:  # keep the worker alive across transient DB/network outages
                result = WorkerCycleResult(
                    started_at=started,
                    finished_at=datetime.now(timezone.utc),
                    error=f"{type(exc).__name__}: {exc}",
                )
            if on_cycle:
                on_cycle(result)
            event.wait(self.interval_seconds)
