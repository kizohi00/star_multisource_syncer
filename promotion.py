from __future__ import annotations

from collections.abc import Sequence

from ..domain.ports import SourceAdapter
from ..infrastructure.repositories import MySqlSourceRepository


class PromotionService:
    """Prepare canonical-content proposals without auto-creating uncertain data."""

    def __init__(self, repository: MySqlSourceRepository) -> None:
        self.repository = repository

    def preview(
        self,
        adapters: Sequence[SourceAdapter],
        *,
        limit: int,
        enqueue: bool = False,
    ) -> dict:
        source_keys = [adapter.key for adapter in adapters]
        candidates = self.repository.list_promotion_candidates(source_keys, limit=limit)
        queued = self.repository.enqueue_promotion_candidates(candidates) if enqueue else 0
        counts: dict[str, int] = {"create_series": 0, "create_chapter": 0}
        for candidate in candidates:
            action = str(candidate.get("action"))
            if action in counts:
                counts[action] += 1
        return {
            "status": "queued" if enqueue else "preview",
            "candidates": candidates,
            "counts": counts,
            "queued": queued,
        }

    def queue(
        self,
        adapters: Sequence[SourceAdapter],
        *,
        limit: int,
    ) -> dict:
        return self.preview(adapters, limit=limit, enqueue=True)

    def review(
        self,
        queue_ids: Sequence[int],
        *,
        status: str,
        note: str | None = None,
    ) -> dict:
        updated = self.repository.review_promotion_queue(queue_ids, status=status, note=note)
        return {
            "status": status,
            "requested": len(queue_ids),
            "updated": updated,
        }

    def apply_approved(
        self,
        *,
        source_keys: Sequence[str] | None = None,
        limit: int,
    ) -> dict:
        results = self.repository.apply_approved_promotions(source_keys=source_keys, limit=limit)
        counts: dict[str, int] = {}
        for result in results:
            status = str(result.get("status", "unknown"))
            counts[status] = counts.get(status, 0) + 1
        return {
            "status": "completed",
            "processed": len(results),
            "counts": counts,
            "results": results,
        }
