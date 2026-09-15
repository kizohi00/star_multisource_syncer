from __future__ import annotations

from collections.abc import Iterable

from ..domain.errors import SourceChapterLocked
from ..domain.models import SourceChapterSnapshot
from ..domain.ports import SourceAdapter
from ..infrastructure.repositories import MySqlSourceRepository


def fetch_and_store_pages(repository: MySqlSourceRepository, adapter: SourceAdapter, source_chapter_id: int) -> dict:
    row = repository.get_source_chapter(source_chapter_id)
    if not row:
        raise ValueError(f"Source chapter {source_chapter_id} was not found.")
    if row["source_key"] != adapter.key:
        raise ValueError(f"Chapter {source_chapter_id} belongs to {row['source_key']}, not {adapter.key}.")
    chapter = SourceChapterSnapshot(
        source_chapter_key=row["source_chapter_key"],
        source_url=row["source_url"],
        label=row["label_raw"],
        number=row["chapter_number"],
        title=row["title_raw"],
    )
    pages = adapter.fetch_pages(chapter)
    stored = repository.upsert_pages(source_chapter_id, pages)
    return {"source_key": adapter.key, "source_chapter_id": source_chapter_id, "pages": stored}


def fetch_and_store_pages_with_fallback(
    repository: MySqlSourceRepository,
    adapters: Iterable[SourceAdapter],
    source_chapter_id: int,
) -> dict:
    """Fetch a chapter, falling back to an active source when it is locked."""
    row = repository.get_source_chapter(source_chapter_id)
    if not row:
        raise ValueError(f"Source chapter {source_chapter_id} was not found.")

    adapter_by_key = {adapter.key: adapter for adapter in adapters}
    requested_adapter = adapter_by_key.get(row["source_key"])
    if requested_adapter is None:
        raise ValueError(f"No adapter is registered for source {row['source_key']}.")

    requested_chapter = _chapter_from_row(row)
    locked_error: SourceChapterLocked | None = None
    if row.get("access_status") == "locked":
        locked_error = SourceChapterLocked(
            f"Source chapter {source_chapter_id} was previously marked locked."
        )
    else:
        try:
            pages = requested_adapter.fetch_pages(requested_chapter)
        except SourceChapterLocked as error:
            locked_error = error
        else:
            stored = repository.upsert_pages(source_chapter_id, pages)
            return {
                "requested_source_chapter_id": source_chapter_id,
                "source_chapter_id": source_chapter_id,
                "source_key": requested_adapter.key,
                "pages": stored,
                "fallback_used": False,
                "status": "direct",
            }

    if locked_error is not None:
        # Record every failed locked attempt, including a chapter that was
        # already marked locked by the latest-feed parser. The timestamp is
        # the event-driven retry checkpoint: without refreshing it, a chapter
        # retried after one real event would remain eligible on every cycle.
        repository.mark_source_chapter_access(
            source_chapter_id,
            "locked",
            str(locked_error),
        )
        for candidate in repository.find_fallback_chapters(source_chapter_id):
            candidate_adapter = adapter_by_key.get(candidate["source_key"])
            if candidate_adapter is None:
                continue
            try:
                candidate_pages = candidate_adapter.fetch_pages(_chapter_from_row(candidate))
            except SourceChapterLocked as candidate_locked:
                repository.mark_source_chapter_access(
                    int(candidate["id"]),
                    "locked",
                    str(candidate_locked),
                )
                continue
            except Exception as candidate_error:
                repository.mark_source_chapter_access(
                    int(candidate["id"]),
                    "error",
                    f"{type(candidate_error).__name__}: {candidate_error}",
                )
                continue
            stored = repository.upsert_pages(int(candidate["id"]), candidate_pages)
            return {
                "requested_source_chapter_id": source_chapter_id,
                "source_chapter_id": int(candidate["id"]),
                "source_key": candidate["source_key"],
                "pages": stored,
                "fallback_used": True,
                "status": "fallback",
            }
        raise SourceChapterLocked(
            f"Source chapter {source_chapter_id} is locked and no active source "
            "has a verified matching chapter."
        ) from locked_error

    raise RuntimeError("Page fetch ended without a direct or fallback result.")


def _chapter_from_row(row: dict) -> SourceChapterSnapshot:
    return SourceChapterSnapshot(
        source_chapter_key=row["source_chapter_key"],
        source_url=row["source_url"],
        label=row["label_raw"],
        number=row["chapter_number"],
        title=row.get("title_raw"),
        access_status=row.get("access_status", "unknown"),
    )
