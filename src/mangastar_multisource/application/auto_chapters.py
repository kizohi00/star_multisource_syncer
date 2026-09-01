from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from collections.abc import Sequence

from ..domain.errors import SourceChapterLocked
from ..domain.ports import SourceAdapter
from ..infrastructure.repositories import MySqlSourceRepository
from .pages import fetch_and_store_pages_with_fallback


class AutoChapterPromotionService:
    """Fill a newly-created canonical series from every source chapter."""

    def __init__(self, repository: MySqlSourceRepository, *, max_workers: int = 1) -> None:
        if max_workers < 1:
            raise ValueError("max_workers must be at least 1")
        self.repository = repository
        self.max_workers = max_workers

    def promote_all(
        self,
        source_work_id: int,
        adapters: Sequence[SourceAdapter],
    ) -> dict:
        attempted = 0
        promoted = 0
        fallback_count = 0
        already_applied = 0
        failures: list[dict] = []
        rows = self.repository.list_unpromoted_source_chapters(source_work_id)
        if self.max_workers == 1 or len(rows) <= 1:
            outcomes = [self._promote_one(row, adapters) for row in rows]
        else:
            with ThreadPoolExecutor(
                max_workers=min(self.max_workers, len(rows)),
                thread_name_prefix="chapter-promote",
            ) as executor:
                outcomes = list(executor.map(
                    lambda row: self._promote_one(row, adapters),
                    rows,
                ))

        for outcome in outcomes:
            attempted += 1
            requested_id = int(outcome["requested_source_chapter_id"])
            if outcome["status"] == "failure":
                failures.append(outcome["failure"])
                continue
            fallback_count += int(bool(outcome.get("fallback_used")))
            result_status = str(outcome["chapter_status"])
            canonical_chapter_id = outcome.get("canonical_chapter_id")
            if result_status in {"created", "applied"}:
                promoted += 1
            elif result_status in {"already_applied", "already_mapped"}:
                already_applied += 1
        return {
            "source_work_id": int(source_work_id),
            "attempted": attempted,
            "promoted": promoted,
            "already_applied": already_applied,
            "fallbacks": fallback_count,
            "failed": len(failures),
            "failures": failures,
        }

    def _promote_one(
        self,
        row: dict,
        adapters: Sequence[SourceAdapter],
    ) -> dict:
        requested_id = int(row["id"])
        actual_id = requested_id
        try:
            fallback_used = False
            if row.get("pages_fetched_at") is None:
                page_result = fetch_and_store_pages_with_fallback(
                    self.repository,
                    adapters,
                    requested_id,
                )
                actual_id = int(page_result["source_chapter_id"])
                fallback_used = bool(page_result.get("fallback_used"))
            chapter_result = self.repository.auto_promote_source_chapter(actual_id)
            result_status = str(chapter_result.get("status"))
            if result_status not in {"created", "applied", "already_applied", "already_mapped"}:
                raise RuntimeError(
                    f"unexpected chapter promotion status: {result_status}"
                )
            canonical_chapter_id = chapter_result.get("canonical_chapter_id")
            if actual_id != requested_id and canonical_chapter_id is not None:
                self.repository.link_source_chapter_to_canonical(
                    requested_id,
                    int(canonical_chapter_id),
                )
            return {
                "status": "success",
                "requested_source_chapter_id": requested_id,
                "source_chapter_id": actual_id,
                "chapter_status": result_status,
                "canonical_chapter_id": canonical_chapter_id,
                "fallback_used": fallback_used,
            }
        except SourceChapterLocked as error:
            return {
                "status": "failure",
                "requested_source_chapter_id": requested_id,
                "failure": {
                    "source_chapter_id": requested_id,
                    "status": "locked",
                    "error": str(error),
                },
            }
        except Exception as error:
            return {
                "status": "failure",
                "requested_source_chapter_id": requested_id,
                "failure": {
                    "source_chapter_id": requested_id,
                    "status": "failed",
                    "error": f"{type(error).__name__}: {error}",
                },
            }
