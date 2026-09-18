from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence

from bs4 import BeautifulSoup

from ..domain.models import (
    CanonicalSeries,
    LatestFeedSnapshot,
    MatchResult,
    SourceChapterFrontier,
    SourceChapterSnapshot,
    SourcePageSnapshot,
    SourceWorkFrontier,
    SourceWorkSnapshot,
)
from ..domain.status import story_status_from_payload
from ..matching.metadata import title_similarity
from .db import MySqlDatabase


def _hash_key(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _snapshot_payload(snapshot: SourceWorkSnapshot) -> dict:
    """Keep parsed metadata available for later review without schema churn."""
    payload = dict(snapshot.payload)
    metadata = dict(payload.get("_mangastar_metadata") or {})
    if snapshot.author_names:
        metadata["author_names"] = list(snapshot.author_names)
    if snapshot.painter_names:
        metadata["painter_names"] = list(snapshot.painter_names)
    if snapshot.publisher_name:
        metadata["publisher_name"] = snapshot.publisher_name
    if snapshot.type_name:
        metadata["type_name"] = snapshot.type_name
    if metadata:
        payload["_mangastar_metadata"] = metadata
    return payload


def _chapter_candidate_score(source: dict, candidate: dict) -> dict[str, float | None]:
    title_score = None
    if source.get("title_raw") and candidate.get("title"):
        title_score = title_similarity(str(source["title_raw"]), str(candidate["title"]))

    date_score = None
    source_date = source.get("published_at")
    candidate_date = candidate.get("created_at")
    if source_date and candidate_date:
        days = abs((source_date - candidate_date).total_seconds()) / 86400
        if days <= 3:
            date_score = 1.0
        elif days <= 14:
            date_score = 0.75
        elif days <= 60:
            date_score = 0.45
        else:
            date_score = 0.15

    available = [(0.85, title_score), (0.15, date_score)]
    weight = sum(weight for weight, value in available if value is not None)
    score = sum(weight_value * float(value) for weight_value, value in available if value is not None) / weight if weight else 0.0
    return {"title_score": title_score, "date_score": date_score, "score": round(score, 6)}


class MySqlSourceRepository:
    def __init__(self, database: MySqlDatabase) -> None:
        self.database = database

    @staticmethod
    def _retryable_source_chapter_condition(work_alias: str, chapter_alias: str) -> str:
        """Return the event-driven retry predicate for one source chapter.

        A locked chapter is eligible on its first attempt, or after a later
        chapter for the same source work was observed, or after a new verified
        fallback candidate for the same canonical work/chapter appeared.
        """
        return f"""
            (
                {chapter_alias}.access_status <> 'locked'
                OR {chapter_alias}.access_checked_at IS NULL
                OR EXISTS (
                    SELECT 1
                    FROM ms_source_chapters AS newer_chapter
                    WHERE newer_chapter.source_work_id={chapter_alias}.source_work_id
                      AND newer_chapter.id <> {chapter_alias}.id
                      AND newer_chapter.first_seen_at > {chapter_alias}.access_checked_at
                )
                OR EXISTS (
                    SELECT 1
                    FROM ms_source_chapters AS fallback_chapter
                    INNER JOIN ms_source_works AS fallback_work
                        ON fallback_work.id=fallback_chapter.source_work_id
                    INNER JOIN ms_sources AS fallback_source
                        ON fallback_source.source_key=fallback_work.source_key
                    WHERE fallback_chapter.id <> {chapter_alias}.id
                      AND fallback_work.source_key <> {work_alias}.source_key
                      AND fallback_source.enabled=1
                      AND fallback_chapter.access_status <> 'locked'
                      AND fallback_chapter.first_seen_at > {chapter_alias}.access_checked_at
                      AND {work_alias}.canonical_series_id IS NOT NULL
                      AND fallback_work.canonical_series_id={work_alias}.canonical_series_id
                      AND {chapter_alias}.chapter_number IS NOT NULL
                      AND fallback_chapter.chapter_number={chapter_alias}.chapter_number
                )
            )
        """

    def persist_feed(
        self,
        feed: LatestFeedSnapshot,
        *,
        display_name: str,
        base_url: str,
        decisions: dict[str, tuple[MatchResult, Sequence[tuple[CanonicalSeries, float, dict]]]],
        pages_scanned: int | None = None,
        known_streak: int = 0,
        stop_reason: str | None = None,
        record_poll: bool = True,
    ) -> tuple[int, int, int]:
        """Persist one feed and return (new works, observed chapters, new chapters).

        Historical backfill pages share the same upsert path but do not replace
        the normal latest-feed health counters or telemetry row.
        """
        new_works = 0
        chapter_count = 0
        new_chapter_count = 0
        with self.database.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO ms_sources (source_key, display_name, base_url)
                    VALUES (%s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                      display_name = VALUES(display_name),
                      base_url = VALUES(base_url),
                      updated_at = CURRENT_TIMESTAMP
                    """,
                    (feed.source_key, display_name, base_url),
                )
                for work in feed.works:
                    cursor.execute(
                        "SELECT id, canonical_series_id FROM ms_source_works WHERE source_key=%s AND source_work_hash=%s",
                        (work.source_key, _hash_key(work.source_work_key)),
                    )
                    existing = cursor.fetchone()
                    match, ranked = decisions[work.source_work_key]
                    source_work_id = self._upsert_work_cursor(cursor, work, match)
                    self._record_candidates_cursor(cursor, source_work_id, ranked)
                    observed, new_chapters = self._upsert_chapters_cursor(
                        cursor, source_work_id, work, track_new=True
                    )
                    chapter_count += observed
                    new_chapter_count += new_chapters
                    if existing is None:
                        new_works += 1
                if record_poll:
                    cursor.execute(
                        """
                        UPDATE ms_sources
                        SET last_success_at=CURRENT_TIMESTAMP, last_error_at=NULL, last_error=NULL,
                            last_feed_count=%s, last_new_count=%s, updated_at=CURRENT_TIMESTAMP
                        WHERE source_key=%s
                        """,
                        (len(feed.works), new_works, feed.source_key),
                    )
                    cursor.execute(
                        """
                        INSERT INTO ms_poll_runs
                          (source_key, status, fetched_count, new_count, pages_scanned,
                           known_streak, stop_reason, finished_at)
                        VALUES (%s, 'success', %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
                        """,
                        (
                            feed.source_key,
                            len(feed.works),
                            new_works,
                            feed.page if pages_scanned is None else pages_scanned,
                            known_streak,
                            stop_reason,
                        ),
                    )
        return new_works, chapter_count, new_chapter_count

    def _upsert_work_cursor(self, cursor, snapshot: SourceWorkSnapshot, match: MatchResult) -> int:
        source_hash = _hash_key(snapshot.source_work_key)
        effective_match = match
        if match.canonical_series_id is None:
            exact_title_series = self._find_exact_title_series_cursor(cursor, snapshot.title)
            if exact_title_series is not None:
                effective_match = MatchResult(
                    "matched",
                    int(exact_title_series["id"]),
                    1.0,
                    1.0,
                    {
                        "reason": "exact_title_guard",
                        "source_title": snapshot.title,
                        "canonical_title": exact_title_series["title"],
                    },
                )
        cursor.execute(
            """
            INSERT INTO ms_source_works
              (source_key, source_work_key, source_work_hash, source_url, title_raw,
               alt_titles_json, summary_raw, cover_url, tags_json, payload_json,
               match_status, match_score, canonical_series_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
              source_url = VALUES(source_url),
              title_raw = VALUES(title_raw),
              alt_titles_json = VALUES(alt_titles_json),
              summary_raw = VALUES(summary_raw),
              cover_url = VALUES(cover_url),
              tags_json = VALUES(tags_json),
              payload_json = VALUES(payload_json),
              match_status = IF(canonical_series_id IS NULL, VALUES(match_status), match_status),
              match_score = IF(canonical_series_id IS NULL, VALUES(match_score), match_score),
              last_seen_at = CURRENT_TIMESTAMP,
              updated_at = CURRENT_TIMESTAMP
            """,
            (
                snapshot.source_key,
                snapshot.source_work_key,
                source_hash,
                snapshot.source_url,
                snapshot.title,
                json.dumps(snapshot.alternative_titles, ensure_ascii=False),
                snapshot.summary,
                snapshot.cover_url,
                json.dumps(snapshot.tags, ensure_ascii=False),
                json.dumps(_snapshot_payload(snapshot), ensure_ascii=False),
                effective_match.status,
                effective_match.score,
                effective_match.canonical_series_id,
            ),
        )
        cursor.execute(
            "SELECT id, canonical_series_id FROM ms_source_works WHERE source_key=%s AND source_work_hash=%s",
            (snapshot.source_key, source_hash),
        )
        row = cursor.fetchone()
        if not row:
            raise RuntimeError("Source work upsert did not return an id.")
        source_work_id = int(row["id"])
        if (
            effective_match.status == "matched"
            and effective_match.canonical_series_id is not None
            and row["canonical_series_id"] is None
        ):
            cursor.execute(
                """
                UPDATE ms_source_works
                SET canonical_series_id=%s, match_status='matched', match_score=%s, updated_at=CURRENT_TIMESTAMP
                WHERE id=%s
                """,
                (effective_match.canonical_series_id, effective_match.score, source_work_id),
            )
        canonical_series_id = row["canonical_series_id"] or effective_match.canonical_series_id
        source_story_status = self._source_story_status(snapshot)
        self._update_series_story_status_cursor(cursor, canonical_series_id, source_story_status)
        return source_work_id

    @staticmethod
    def _source_story_status(snapshot: SourceWorkSnapshot) -> str | None:
        """Translate source status labels to Manga Star's three statuses.

        ``story_status`` is intentionally used for the source translation
        state in the current Manga Star schema.  ``translation_status`` is a
        legacy/unused column and must never be changed by the syncer.
        """
        return story_status_from_payload(snapshot.payload)

    @staticmethod
    def _update_series_story_status_cursor(
        cursor,
        canonical_series_id: object,
        story_status: str | None,
    ) -> None:
        """Update only the canonical story-status column when evidence exists."""
        if canonical_series_id is None or story_status is None:
            return
        cursor.execute(
            """
            UPDATE series
            SET story_status=%s
            WHERE id=%s
              AND (story_status IS NULL OR story_status<>%s)
            """,
            (story_status, int(canonical_series_id), story_status),
        )

    @staticmethod
    def _find_exact_title_series_cursor(cursor, title: object) -> dict | None:
        value = str(title or "").strip()
        if not value:
            return None
        cursor.execute(
            """
            SELECT id, title
            FROM series
            WHERE deleted_at IS NULL
              AND title IS NOT NULL
              AND LOWER(TRIM(title))=LOWER(TRIM(%s))
            ORDER BY id
            LIMIT 1
            FOR UPDATE
            """,
            (value,),
        )
        return cursor.fetchone()

    @staticmethod
    def _upsert_chapters_cursor(
        cursor,
        source_work_id: int,
        snapshot: SourceWorkSnapshot,
        *,
        track_new: bool,
    ) -> tuple[int, int]:
        count = 0
        new_count = 0
        for chapter in snapshot.chapters:
            chapter_hash = _hash_key(chapter.source_chapter_key)
            if track_new:
                cursor.execute(
                    "SELECT id FROM ms_source_chapters WHERE source_work_id=%s AND source_chapter_hash=%s",
                    (source_work_id, chapter_hash),
                )
                if cursor.fetchone() is None:
                    new_count += 1
            cursor.execute(
                """
                INSERT INTO ms_source_chapters
                  (source_work_id, source_chapter_key, source_chapter_hash, source_url,
                   label_raw, chapter_number, title_raw, published_at, access_status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                  source_url = VALUES(source_url),
                  label_raw = VALUES(label_raw),
                  chapter_number = VALUES(chapter_number),
                  title_raw = VALUES(title_raw),
                  published_at = COALESCE(VALUES(published_at), published_at),
                  access_status = CASE
                    WHEN VALUES(access_status)='locked' THEN 'locked'
                    ELSE access_status
                  END,
                  last_seen_at = CURRENT_TIMESTAMP,
                  updated_at = CURRENT_TIMESTAMP
                """,
                (
                    source_work_id,
                    chapter.source_chapter_key,
                    chapter_hash,
                    chapter.source_url,
                    chapter.label,
                    chapter.number,
                    chapter.title,
                    chapter.published_at.replace(tzinfo=None) if chapter.published_at else None,
                    chapter.access_status,
                ),
            )
            count += 1
        return count, new_count

    @staticmethod
    def _record_candidates_cursor(cursor, source_work_id: int, candidates: Sequence[tuple[CanonicalSeries, float, dict]]) -> None:
        for rank, (series, score, evidence) in enumerate(candidates[:5], start=1):
            cursor.execute(
                """
                INSERT INTO ms_match_candidates
                  (source_work_id, canonical_series_id, rank_position, score, evidence_json)
                VALUES (%s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                  rank_position = VALUES(rank_position),
                  score = VALUES(score),
                  evidence_json = VALUES(evidence_json),
                  updated_at = CURRENT_TIMESTAMP
                """,
                (source_work_id, series.id, rank, score, json.dumps(evidence, ensure_ascii=False)),
            )

    def ensure_source(self, source_key: str, display_name: str, base_url: str) -> None:
        with self.database.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO ms_sources (source_key, display_name, base_url)
                    VALUES (%s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                      display_name = VALUES(display_name),
                      base_url = VALUES(base_url),
                      updated_at = CURRENT_TIMESTAMP
                    """,
                    (source_key, display_name, base_url),
                )
                cursor.execute(
                    """
                    INSERT INTO ms_source_backfill_state (source_key, next_page)
                    VALUES (%s, 2)
                    ON DUPLICATE KEY UPDATE source_key=VALUES(source_key)
                    """,
                    (source_key,),
                )

    def claim_source_backfill_page(
        self,
        source_key: str,
        *,
        interval_seconds: int = 86400,
        lease_seconds: int = 1800,
    ) -> int | None:
        """Claim the next historical page, or return None when not due.

        The short lease prevents overlapping Railway invocations from moving
        the cursor past a page whose first attempt has not finished. A failed
        fetch releases the lease without advancing the cursor.
        """
        if interval_seconds < 0:
            raise ValueError("interval_seconds cannot be negative")
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be at least 1")
        with self.database.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO ms_source_backfill_state (source_key, next_page)
                    VALUES (%s, 2)
                    ON DUPLICATE KEY UPDATE source_key=VALUES(source_key)
                    """,
                    (source_key,),
                )
                cursor.execute(
                    """
                    SELECT next_page,
                           in_flight_page,
                           lease_until,
                           cycle_completed_at,
                           (
                               cycle_completed_at IS NULL
                               OR TIMESTAMPDIFF(SECOND, cycle_completed_at, CURRENT_TIMESTAMP) >= %s
                           ) AS is_due,
                           (
                               in_flight_page IS NULL
                               OR lease_until IS NULL
                               OR lease_until <= CURRENT_TIMESTAMP
                           ) AS lease_available
                    FROM ms_source_backfill_state
                    WHERE source_key=%s
                    FOR UPDATE
                    """,
                    (interval_seconds, source_key),
                )
                row = cursor.fetchone()
                if not row or not row["is_due"] or not row["lease_available"]:
                    return None
                page = max(2, int(row["next_page"]))
                cursor.execute(
                    """
                    UPDATE ms_source_backfill_state
                    SET in_flight_page=%s,
                        lease_until=DATE_ADD(CURRENT_TIMESTAMP, INTERVAL %s SECOND),
                        last_error=NULL,
                        updated_at=CURRENT_TIMESTAMP
                    WHERE source_key=%s
                    """,
                    (page, lease_seconds, source_key),
                )
                return page

    def complete_source_backfill_page(
        self,
        source_key: str,
        page: int,
        *,
        has_more: bool,
        stop_reason: str,
    ) -> int | None:
        """Advance the historical cursor after a successful page fetch."""
        if page < 2:
            raise ValueError("backfill page must be at least 2")
        with self.database.transaction() as connection:
            with connection.cursor() as cursor:
                if has_more:
                    cursor.execute(
                        """
                        UPDATE ms_source_backfill_state
                        SET next_page=GREATEST(next_page, %s),
                            in_flight_page=NULL,
                            lease_until=NULL,
                            last_scanned_page=%s,
                            last_scanned_at=CURRENT_TIMESTAMP,
                            last_error=NULL,
                            updated_at=CURRENT_TIMESTAMP
                        WHERE source_key=%s AND in_flight_page=%s
                        """,
                        (page + 1, page, source_key, page),
                    )
                    next_page = page + 1
                else:
                    cursor.execute(
                        """
                        UPDATE ms_source_backfill_state
                        SET next_page=2,
                            in_flight_page=NULL,
                            lease_until=NULL,
                            last_scanned_page=%s,
                            last_scanned_at=CURRENT_TIMESTAMP,
                            cycle_completed_at=CURRENT_TIMESTAMP,
                            last_error=NULL,
                            updated_at=CURRENT_TIMESTAMP
                        WHERE source_key=%s AND in_flight_page=%s
                        """,
                        (page, source_key, page),
                    )
                    next_page = 2
                if cursor.rowcount != 1:
                    raise RuntimeError(
                        f"backfill cursor claim changed before completing page {page} ({stop_reason})"
                    )
                return next_page

    def fail_source_backfill_page(self, source_key: str, page: int, error: str) -> None:
        """Release a failed page claim so the same page is retried later."""
        with self.database.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE ms_source_backfill_state
                    SET in_flight_page=NULL,
                        lease_until=NULL,
                        last_error=%s,
                        updated_at=CURRENT_TIMESTAMP
                    WHERE source_key=%s AND in_flight_page=%s
                    """,
                    (error[:2000], source_key, page),
                )

    def load_source_frontier(self, source_key: str) -> dict[str, SourceWorkFrontier]:
        """Load only keys/numbers/titles needed by the latest-feed stop rule."""
        with self.database.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT sw.source_work_key, sc.source_chapter_key,
                           sc.chapter_number, sc.title_raw
                    FROM ms_source_works AS sw
                    LEFT JOIN ms_source_chapters AS sc ON sc.source_work_id=sw.id
                    WHERE sw.source_key=%s
                    ORDER BY sw.id, sc.chapter_number DESC, sc.id DESC
                    """,
                    (source_key,),
                )
                frontier: dict[str, list[SourceChapterFrontier]] = {}
                for row in cursor.fetchall():
                    work_key = str(row["source_work_key"])
                    chapters = frontier.setdefault(work_key, [])
                    if row.get("source_chapter_key") is not None:
                        chapters.append(
                            SourceChapterFrontier(
                                source_chapter_key=str(row["source_chapter_key"]),
                                number=row.get("chapter_number"),
                                title=row.get("title_raw"),
                            )
                        )
                return {
                    work_key: SourceWorkFrontier(work_key, tuple(chapters))
                    for work_key, chapters in frontier.items()
                }

    def list_source_works(self, source_key: str, limit: int, *, pending_only: bool = False) -> list[dict]:
        with self.database.transaction() as connection:
            with connection.cursor() as cursor:
                sql = """
                    SELECT sw.id, sw.source_work_key, sw.source_url, sw.title_raw,
                           sw.canonical_series_id, sw.match_status, sw.detail_fetched_at
                    FROM ms_source_works AS sw
                    WHERE sw.source_key=%s
                """
                params: list = [source_key]
                if pending_only:
                    # A low-confidence work may be auto-created before every
                    # source chapter is available. Keep it eligible for a
                    # transient page failure, or when a later chapter/fallback
                    # observation makes a locked chapter worth retrying.
                    sql += f"""
                        AND (
                            sw.detail_fetched_at IS NULL
                            OR (
                                sw.match_status='auto_created'
                                AND EXISTS (
                                    SELECT 1
                                    FROM ms_source_chapters AS pending_chapter
                                    WHERE pending_chapter.source_work_id=sw.id
                                      AND pending_chapter.canonical_chapter_id IS NULL
                                      AND {self._retryable_source_chapter_condition("sw", "pending_chapter")}
                                )
                            )
                        )
                    """
                sql += " ORDER BY last_seen_at DESC, id DESC LIMIT %s"
                params.append(limit)
                cursor.execute(sql, params)
                return list(cursor.fetchall())

    def list_source_chapters(self, source_work_id: int, limit: int | None = None) -> list[dict]:
        with self.database.transaction() as connection:
            with connection.cursor() as cursor:
                sql = """
                    SELECT id, source_chapter_key, source_url, label_raw, chapter_number,
                           title_raw, canonical_chapter_id, match_status, access_status,
                           access_error, access_checked_at, pages_fetched_at
                    FROM ms_source_chapters
                    WHERE source_work_id=%s
                    ORDER BY chapter_number DESC, id DESC
                """
                params: list = [source_work_id]
                if limit is not None:
                    sql += " LIMIT %s"
                    params.append(limit)
                cursor.execute(sql, params)
                return list(cursor.fetchall())

    def list_unpromoted_source_chapters(self, source_work_id: int) -> list[dict]:
        """Return retryable source chapters not yet represented canonically.

        Locked rows are intentionally excluded after their first failed
        attempt until a later same-work chapter or fallback observation is
        recorded. This prevents a permanently paid chapter from consuming
        network time on every ten-minute cycle.
        """
        with self.database.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    SELECT sc.id, sc.source_chapter_key, sc.source_url, sc.label_raw,
                           sc.chapter_number, sc.title_raw, sc.canonical_chapter_id,
                           sc.match_status, sc.access_status, sc.pages_fetched_at
                    FROM ms_source_chapters AS sc
                    INNER JOIN ms_source_works AS sw ON sw.id=sc.source_work_id
                    WHERE sc.source_work_id=%s
                      AND sc.canonical_chapter_id IS NULL
                      AND {self._retryable_source_chapter_condition("sw", "sc")}
                    ORDER BY sc.chapter_number IS NULL, sc.chapter_number, sc.id
                    """,
                    (source_work_id,),
                )
                return list(cursor.fetchall())

    def link_pending_exact_chapters(
        self,
        source_keys: Sequence[str] | None = None,
        *,
        limit: int = 500,
    ) -> dict[str, int]:
        """Link pending source rows to the one canonical chapter with the same number.

        A source can report a chapter that is already present in Manga Star under
        another source. Those rows do not need another page download or a second
        canonical chapter; they only need their source alias linked.
        """
        if limit < 1:
            raise ValueError("limit must be at least 1")
        source_filter, source_params = self._source_filter(source_keys, "sw.source_key")
        linked = 0
        touched_series: set[int] = set()
        with self.database.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    SELECT sc.id AS source_chapter_id,
                           sw.canonical_series_id,
                           canonical.id AS canonical_chapter_id
                    FROM ms_source_chapters AS sc
                    INNER JOIN ms_source_works AS sw ON sw.id=sc.source_work_id
                    INNER JOIN chapters AS canonical
                      ON canonical.series_id=sw.canonical_series_id
                     AND canonical.chapter_number=sc.chapter_number
                     AND canonical.deleted_at IS NULL
                    WHERE sw.canonical_series_id IS NOT NULL
                      AND sc.canonical_chapter_id IS NULL
                      AND sc.chapter_number IS NOT NULL
                      AND NOT EXISTS (
                          SELECT 1
                          FROM chapters AS duplicate
                          WHERE duplicate.series_id=sw.canonical_series_id
                            AND duplicate.chapter_number=sc.chapter_number
                            AND duplicate.deleted_at IS NULL
                            AND duplicate.id <> canonical.id
                      )
                      {source_filter}
                    ORDER BY sc.last_seen_at DESC, sc.id DESC
                    LIMIT %s
                    FOR UPDATE
                    """,
                    [*source_params, limit],
                )
                rows = list(cursor.fetchall())
                for row in rows:
                    cursor.execute(
                        """
                        UPDATE ms_source_chapters
                        SET canonical_chapter_id=%s, match_status='matched',
                            match_score=1.000000, updated_at=CURRENT_TIMESTAMP
                        WHERE id=%s AND canonical_chapter_id IS NULL
                        """,
                        (row["canonical_chapter_id"], row["source_chapter_id"]),
                    )
                    if cursor.rowcount == 1:
                        linked += 1
                        touched_series.add(int(row["canonical_series_id"]))
                for series_id in touched_series:
                    self._refresh_series_latest_chapter_cursor(cursor, series_id)
        return {"linked": linked, "series_refreshed": len(touched_series)}

    def list_unpromoted_mapped_source_chapters(
        self,
        source_keys: Sequence[str] | None = None,
        *,
        limit: int = 50,
    ) -> list[dict]:
        """Return retryable source chapters that still need canonical pages.

        Rows whose chapter number already exists canonically are deliberately
        excluded. ``link_pending_exact_chapters`` handles those aliases first;
        this query is only for genuinely new canonical chapters.
        """
        if limit < 1:
            raise ValueError("limit must be at least 1")
        source_filter, source_params = self._source_filter(source_keys, "sw.source_key")
        with self.database.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    SELECT sc.id, sc.source_work_id, sw.source_key,
                           sw.canonical_series_id, sc.source_chapter_key,
                           sc.source_url, sc.label_raw, sc.chapter_number,
                           sc.title_raw, sc.canonical_chapter_id,
                           sc.match_status, sc.access_status,
                           sc.pages_fetched_at
                    FROM ms_source_chapters AS sc
                    INNER JOIN ms_source_works AS sw ON sw.id=sc.source_work_id
                    WHERE sw.canonical_series_id IS NOT NULL
                      AND sc.canonical_chapter_id IS NULL
                      AND sc.chapter_number IS NOT NULL
                      AND NOT EXISTS (
                          SELECT 1
                          FROM chapters AS canonical
                          WHERE canonical.series_id=sw.canonical_series_id
                            AND canonical.chapter_number=sc.chapter_number
                            AND canonical.deleted_at IS NULL
                      )
                      AND {self._retryable_source_chapter_condition("sw", "sc")}
                      {source_filter}
                    ORDER BY sc.last_seen_at DESC, sc.id DESC
                    LIMIT %s
                    """,
                    [*source_params, limit],
                )
                return list(cursor.fetchall())

    def link_source_chapter_to_canonical(
        self,
        source_chapter_id: int,
        canonical_chapter_id: int,
    ) -> dict:
        """Link a locked source alias to the canonical chapter served by fallback."""
        with self.database.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT sc.canonical_chapter_id, sw.canonical_series_id,
                           c.series_id
                    FROM ms_source_chapters AS sc
                    INNER JOIN ms_source_works AS sw ON sw.id=sc.source_work_id
                    INNER JOIN chapters AS c ON c.id=%s
                    WHERE sc.id=%s
                    FOR UPDATE
                    """,
                    (canonical_chapter_id, source_chapter_id),
                )
                row = cursor.fetchone()
                if not row:
                    raise ValueError("source or canonical chapter was not found")
                if row["canonical_series_id"] is None or int(row["canonical_series_id"]) != int(row["series_id"]):
                    raise ValueError("source work and canonical chapter belong to different series")
                if row["canonical_chapter_id"] is not None:
                    if int(row["canonical_chapter_id"]) != int(canonical_chapter_id):
                        raise ValueError("source chapter is already linked to another canonical chapter")
                    return {"status": "already_linked", "canonical_chapter_id": int(canonical_chapter_id)}
                cursor.execute(
                    """
                    UPDATE ms_source_chapters
                    SET canonical_chapter_id=%s, match_status='matched',
                        match_score=1.000000, updated_at=CURRENT_TIMESTAMP
                    WHERE id=%s AND canonical_chapter_id IS NULL
                    """,
                    (canonical_chapter_id, source_chapter_id),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("source chapter mapping changed while linking fallback")
                return {"status": "linked", "canonical_chapter_id": int(canonical_chapter_id)}

    def auto_promote_source_chapter(self, source_chapter_id: int) -> dict:
        """Promote a verified source chapter without a human review step."""
        with self.database.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT sw.id AS source_work_id, sw.source_key, sw.canonical_series_id,
                           sw.title_raw AS work_title,
                           sc.id, sc.source_chapter_hash, sc.source_url, sc.label_raw,
                           sc.chapter_number, sc.title_raw, sc.published_at,
                           sc.canonical_chapter_id, sc.access_status, sc.pages_fetched_at
                    FROM ms_source_chapters AS sc
                    INNER JOIN ms_source_works AS sw ON sw.id=sc.source_work_id
                    WHERE sc.id=%s
                    FOR UPDATE
                    """,
                    (source_chapter_id,),
                )
                chapter = cursor.fetchone()
                if not chapter:
                    return {
                        "status": "skipped",
                        "reason": "source_chapter_not_found",
                        "source_chapter_id": int(source_chapter_id),
                    }
                if chapter["canonical_chapter_id"] is not None:
                    return {
                        "status": "already_mapped",
                        "source_chapter_id": int(source_chapter_id),
                        "canonical_chapter_id": int(chapter["canonical_chapter_id"]),
                    }
                if chapter["canonical_series_id"] is None:
                    raise PromotionBlocked("source work is not mapped to a canonical series")

                # A source may report a chapter that already exists in the
                # canonical series under another source. Link that alias
                # instead of creating a duplicate canonical chapter.
                if chapter["chapter_number"] is not None:
                    cursor.execute(
                        """
                        SELECT id
                        FROM chapters
                        WHERE series_id=%s AND chapter_number=%s AND deleted_at IS NULL
                        ORDER BY created_at DESC, id DESC
                        LIMIT 2
                        FOR UPDATE
                        """,
                        (chapter["canonical_series_id"], chapter["chapter_number"]),
                    )
                    same_number = list(cursor.fetchall())
                    if len(same_number) > 1:
                        raise PromotionBlocked(
                            "multiple canonical chapters already use this chapter number"
                        )
                    if same_number:
                        canonical_chapter_id = int(same_number[0]["id"])
                        cursor.execute(
                            """
                            UPDATE ms_source_chapters
                            SET canonical_chapter_id=%s, match_status='matched',
                                match_score=1.000000, updated_at=CURRENT_TIMESTAMP
                            WHERE id=%s AND canonical_chapter_id IS NULL
                            """,
                            (canonical_chapter_id, source_chapter_id),
                        )
                        if cursor.rowcount != 1:
                            raise RuntimeError("source chapter mapping changed while linking exact chapter")
                        self._refresh_series_latest_chapter_cursor(
                            cursor,
                            int(chapter["canonical_series_id"]),
                        )
                        return {
                            "status": "already_mapped",
                            "source_chapter_id": int(source_chapter_id),
                            "canonical_chapter_id": canonical_chapter_id,
                        }
                if chapter["access_status"] != "available" or chapter["pages_fetched_at"] is None:
                    raise PromotionBlocked("source chapter has no verified available pages")

                target_key = f"auto:create_chapter:{int(source_chapter_id)}"
                cursor.execute(
                    """
                    SELECT id, action, target_key, source_work_id, source_chapter_id,
                           canonical_series_id, proposed_title, proposed_chapter_number,
                           proposed_chapter_title, evidence_json, status, applied_chapter_id
                    FROM ms_promotion_queue
                    WHERE target_key=%s
                    FOR UPDATE
                    """,
                    (target_key,),
                )
                queue = cursor.fetchone()
                if queue:
                    if queue["status"] == "applied":
                        return {
                            "status": "already_applied",
                            "source_chapter_id": int(source_chapter_id),
                            "canonical_chapter_id": queue.get("applied_chapter_id"),
                        }
                    if queue["status"] != "approved":
                        raise PromotionBlocked(
                            f"automatic chapter proposal is already {queue['status']}"
                        )
                else:
                    cursor.execute(
                        """
                        INSERT INTO ms_promotion_queue
                          (action, target_key, source_work_id, source_chapter_id,
                           canonical_series_id, proposed_title, proposed_chapter_number,
                           proposed_chapter_title, evidence_json, status)
                        VALUES ('create_chapter', %s, %s, %s, %s, %s, %s, %s, %s, 'approved')
                        """,
                        (
                            target_key,
                            chapter["source_work_id"],
                            source_chapter_id,
                            chapter["canonical_series_id"],
                            _bounded_text(chapter["work_title"], 500) or "source chapter",
                            chapter["chapter_number"],
                            _bounded_text(chapter["title_raw"] or chapter["label_raw"], 500),
                            json.dumps(
                                {
                                    "mode": "auto_new_series_chapter",
                                    "source_chapter_id": int(source_chapter_id),
                                },
                                ensure_ascii=False,
                            ),
                        ),
                    )
                    cursor.execute(
                        """
                        SELECT id, action, target_key, source_work_id, source_chapter_id,
                               canonical_series_id, proposed_title, proposed_chapter_number,
                               proposed_chapter_title, evidence_json, status, applied_chapter_id
                        FROM ms_promotion_queue
                        WHERE target_key=%s
                        FOR UPDATE
                        """,
                        (target_key,),
                    )
                    queue = cursor.fetchone()
                if not queue:
                    raise RuntimeError("automatic chapter proposal was not created")
                result = self._apply_chapter_promotion_cursor(cursor, queue)
                result["auto"] = True
                return result

    def get_source_chapter(self, source_chapter_id: int) -> dict | None:
        with self.database.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT sc.id, sc.source_chapter_key, sc.source_url, sc.label_raw,
                           sc.chapter_number, sc.title_raw, sw.source_key, sc.source_work_id,
                           sw.canonical_series_id, sc.canonical_chapter_id, sc.access_status,
                           sc.access_error, sc.access_checked_at
                    FROM ms_source_chapters sc
                    JOIN ms_source_works sw ON sw.id = sc.source_work_id
                    WHERE sc.id=%s
                    """,
                    (source_chapter_id,),
                )
                return cursor.fetchone()

    def mark_source_chapter_access(
        self,
        source_chapter_id: int,
        status: str,
        error: str | None = None,
    ) -> None:
        allowed = {"unknown", "available", "locked", "error"}
        if status not in allowed:
            raise ValueError(f"Unsupported source chapter access status: {status}")
        with self.database.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE ms_source_chapters
                    SET access_status=%s, access_error=%s, access_checked_at=CURRENT_TIMESTAMP,
                        updated_at=CURRENT_TIMESTAMP
                    WHERE id=%s
                    """,
                    (status, error[:500] if error else None, source_chapter_id),
                )

    def find_fallback_chapters(self, source_chapter_id: int) -> list[dict]:
        """Find active-source chapters matching canonical identity or mapped work/number."""
        with self.database.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT candidate.id, candidate.source_chapter_key, candidate.source_url, candidate.label_raw,
                           candidate.chapter_number, candidate.title_raw,
                           candidate.canonical_chapter_id, candidate.access_status,
                           candidate.source_work_id, candidate_work.source_key,
                           candidate_work.canonical_series_id
                    FROM ms_source_chapters AS requested
                    JOIN ms_source_works AS requested_work
                      ON requested_work.id=requested.source_work_id
                    JOIN ms_source_chapters AS candidate
                      ON candidate.id <> requested.id
                    JOIN ms_source_works AS candidate_work
                      ON candidate_work.id=candidate.source_work_id
                    JOIN ms_sources AS candidate_source
                      ON candidate_source.source_key=candidate_work.source_key
                    WHERE requested.id=%s
                      AND candidate_work.source_key <> requested_work.source_key
                      AND candidate_source.enabled=1
                      AND candidate.access_status <> 'locked'
                      AND (
                        (
                          requested.canonical_chapter_id IS NOT NULL
                          AND candidate.canonical_chapter_id=requested.canonical_chapter_id
                        )
                        OR (
                          requested.canonical_chapter_id IS NULL
                          AND requested_work.canonical_series_id IS NOT NULL
                          AND candidate_work.canonical_series_id=requested_work.canonical_series_id
                          AND requested.chapter_number IS NOT NULL
                          AND candidate.chapter_number=requested.chapter_number
                        )
                      )
                    ORDER BY
                      CASE WHEN candidate_work.source_key='sparkmanga' THEN 0 ELSE 1 END,
                      candidate_source.priority ASC,
                      candidate.id ASC
                    """,
                    (source_chapter_id,),
                )
                return list(cursor.fetchall())

    def persist_enriched_work(
        self,
        snapshot: SourceWorkSnapshot,
        *,
        display_name: str,
        base_url: str,
        match: MatchResult,
        candidates: Sequence[tuple[CanonicalSeries, float, dict]],
    ) -> tuple[int, int]:
        """Persist details and its full chapter list atomically."""
        with self.database.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO ms_sources (source_key, display_name, base_url)
                    VALUES (%s, %s, %s)
                    ON DUPLICATE KEY UPDATE display_name=VALUES(display_name), base_url=VALUES(base_url), updated_at=CURRENT_TIMESTAMP
                    """,
                    (snapshot.source_key, display_name, base_url),
                )
                source_work_id = self._upsert_work_cursor(cursor, snapshot, match)
                self._record_candidates_cursor(cursor, source_work_id, candidates)
                chapter_count, _ = self._upsert_chapters_cursor(
                    cursor, source_work_id, snapshot, track_new=False
                )
                if match.canonical_series_id is not None and snapshot.summary:
                    self._repair_canonical_summary_cursor(
                        cursor,
                        int(match.canonical_series_id),
                        snapshot.summary,
                    )
                cursor.execute(
                    "UPDATE ms_source_works SET detail_fetched_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP WHERE id=%s",
                    (source_work_id,),
                )
                return source_work_id, chapter_count

    @staticmethod
    def _repair_canonical_summary_cursor(
        cursor,
        series_id: int,
        summary: str,
    ) -> None:
        """Repair only clearly polluted/empty summaries from a fresh source snapshot."""
        cursor.execute(
            """
            UPDATE series
            SET summary=%s
            WHERE id=%s
              AND (
                summary IS NULL
                OR TRIM(summary)=''
                OR summary LIKE '%%<p>%%'
                OR summary LIKE '%%<blockquote>%%'
                OR summary LIKE '%%&lt;p&gt;%%'
                OR (
                    summary LIKE '%%التقييم%%'
                    AND summary LIKE '%%التصنيفات%%'
                    AND summary LIKE '%%الفصول%%'
                )
              )
            """,
            (summary, series_id),
        )

    def link_exact_chapters(self, source_work_id: int) -> dict[str, int]:
        """Link only a unique exact chapter number; leave duplicate releases for review."""
        linked = 0
        ambiguous = 0
        with self.database.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT canonical_series_id FROM ms_source_works WHERE id=%s", (source_work_id,))
                work = cursor.fetchone()
                if not work or work["canonical_series_id"] is None:
                    return {"linked": 0, "ambiguous": 0}
                cursor.execute(
                    """
                    SELECT id, chapter_number, title_raw, published_at
                    FROM ms_source_chapters
                    WHERE source_work_id=%s AND canonical_chapter_id IS NULL
                    """,
                    (source_work_id,),
                )
                source_chapters = cursor.fetchall()
                cursor.execute(
                    """
                    SELECT id, chapter_number, season, title, created_at
                    FROM chapters
                    WHERE series_id=%s AND deleted_at IS NULL
                    """,
                    (work["canonical_series_id"],),
                )
                canonical_chapters = cursor.fetchall()
                for source_chapter in source_chapters:
                    number = source_chapter["chapter_number"]
                    if number is None:
                        continue
                    matches = [row for row in canonical_chapters if row["chapter_number"] == number]
                    if len(matches) == 1:
                        cursor.execute(
                            """
                            UPDATE ms_source_chapters
                            SET canonical_chapter_id=%s, match_status='matched', match_score=1.000000,
                                updated_at=CURRENT_TIMESTAMP
                            WHERE id=%s
                            """,
                            (matches[0]["id"], source_chapter["id"]),
                        )
                        linked += 1
                    elif len(matches) > 1:
                        ambiguous += 1
                        ranked = sorted(
                            [
                                (
                                    candidate,
                                    _chapter_candidate_score(source_chapter, candidate),
                                )
                                for candidate in matches
                            ],
                            key=lambda item: (-item[1]["score"], item[0]["id"]),
                        )
                        top_score = ranked[0][1]["score"] if ranked else 0.0
                        second_score = ranked[1][1]["score"] if len(ranked) > 1 else 0.0
                        margin = top_score - second_score
                        if (
                            ranked
                            and ranked[0][1]["title_score"] is not None
                            and top_score >= 0.94
                            and margin >= 0.08
                        ):
                            candidate = ranked[0][0]
                            cursor.execute(
                                """
                                UPDATE ms_source_chapters
                                SET canonical_chapter_id=%s, match_status='matched', match_score=%s,
                                    updated_at=CURRENT_TIMESTAMP
                                WHERE id=%s
                                """,
                                (candidate["id"], top_score, source_chapter["id"]),
                            )
                            linked += 1
                            ambiguous -= 1
                            continue

                        for rank, (candidate, score_data) in enumerate(ranked[:10], start=1):
                            evidence = {
                                "reason": "duplicate_canonical_chapter_number",
                                "chapter_number": str(number),
                                "title_score": score_data["title_score"],
                                "date_score": score_data["date_score"],
                                "score": score_data["score"],
                                "margin": margin,
                            }
                            cursor.execute(
                                """
                                INSERT INTO ms_chapter_match_candidates
                                  (source_chapter_id, canonical_chapter_id, rank_position, score, evidence_json)
                                VALUES (%s, %s, %s, %s, %s)
                                ON DUPLICATE KEY UPDATE rank_position=VALUES(rank_position), score=VALUES(score),
                                  evidence_json=VALUES(evidence_json), updated_at=CURRENT_TIMESTAMP
                                """,
                                (source_chapter["id"], candidate["id"], rank, score_data["score"], json.dumps(evidence)),
                            )
                        cursor.execute(
                            """
                            UPDATE ms_source_chapters
                            SET match_status='candidate', match_score=%s, updated_at=CURRENT_TIMESTAMP
                            WHERE id=%s
                            """,
                            (top_score, source_chapter["id"]),
                        )
                if linked:
                    self._refresh_series_latest_chapter_cursor(
                        cursor,
                        int(work["canonical_series_id"]),
                    )
        return {"linked": linked, "ambiguous": ambiguous}

    def upsert_pages(self, source_chapter_id: int, pages: Sequence[SourcePageSnapshot]) -> int:
        with self.database.transaction() as connection:
            with connection.cursor() as cursor:
                for page in pages:
                    cursor.execute(
                        """
                        INSERT INTO ms_source_pages
                          (source_chapter_id, page_order, source_page_key, image_url, width, height, sampled_at)
                        VALUES (%s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
                        ON DUPLICATE KEY UPDATE source_page_key=VALUES(source_page_key), image_url=VALUES(image_url),
                          width=VALUES(width), height=VALUES(height), sampled_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP
                        """,
                        (source_chapter_id, page.page_order, page.source_page_key, page.image_url, page.width, page.height),
                    )
                cursor.execute(
                    """
                    UPDATE ms_source_chapters
                    SET pages_fetched_at=CURRENT_TIMESTAMP, access_status='available',
                        access_error=NULL, access_checked_at=CURRENT_TIMESTAMP,
                        updated_at=CURRENT_TIMESTAMP
                    WHERE id=%s
                    """,
                    (source_chapter_id,),
                )
                return len(pages)

    def find_work(self, source_key: str, source_work_key: str) -> dict | None:
        with self.database.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT id, canonical_series_id, match_status FROM ms_source_works WHERE source_key=%s AND source_work_hash=%s",
                    (source_key, _hash_key(source_work_key)),
                )
                return cursor.fetchone()

    def upsert_work(self, snapshot: SourceWorkSnapshot, match: MatchResult) -> int:
        source_hash = _hash_key(snapshot.source_work_key)
        with self.database.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO ms_source_works
                      (source_key, source_work_key, source_work_hash, source_url, title_raw,
                       alt_titles_json, summary_raw, cover_url, tags_json, payload_json,
                       match_status, match_score, canonical_series_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                      source_url = VALUES(source_url),
                      title_raw = VALUES(title_raw),
                      alt_titles_json = VALUES(alt_titles_json),
                      summary_raw = VALUES(summary_raw),
                      cover_url = VALUES(cover_url),
                      tags_json = VALUES(tags_json),
                      payload_json = VALUES(payload_json),
                      match_status = IF(canonical_series_id IS NULL, VALUES(match_status), match_status),
                      match_score = IF(canonical_series_id IS NULL, VALUES(match_score), match_score),
                      last_seen_at = CURRENT_TIMESTAMP,
                      updated_at = CURRENT_TIMESTAMP
                    """,
                    (
                        snapshot.source_key,
                        snapshot.source_work_key,
                        source_hash,
                        snapshot.source_url,
                        snapshot.title,
                        json.dumps(snapshot.alternative_titles, ensure_ascii=False),
                        snapshot.summary,
                        snapshot.cover_url,
                        json.dumps(snapshot.tags, ensure_ascii=False),
                        json.dumps(_snapshot_payload(snapshot), ensure_ascii=False),
                        match.status,
                        match.score,
                        match.canonical_series_id,
                    ),
                )
                cursor.execute(
                    "SELECT id, canonical_series_id FROM ms_source_works WHERE source_key=%s AND source_work_hash=%s",
                    (snapshot.source_key, source_hash),
                )
                row = cursor.fetchone()
                if not row:
                    raise RuntimeError("Source work upsert did not return an id.")
                source_work_id = int(row["id"])
                if match.status == "matched" and match.canonical_series_id is not None and row["canonical_series_id"] is None:
                    cursor.execute(
                        """
                        UPDATE ms_source_works
                        SET canonical_series_id=%s, match_status='matched', match_score=%s, updated_at=CURRENT_TIMESTAMP
                        WHERE id=%s
                        """,
                        (match.canonical_series_id, match.score, source_work_id),
                    )
                canonical_series_id = row["canonical_series_id"] or match.canonical_series_id
                self._update_series_story_status_cursor(
                    cursor,
                    canonical_series_id,
                    self._source_story_status(snapshot),
                )
                return source_work_id

    def upsert_chapters(self, source_work_id: int, snapshot: SourceWorkSnapshot) -> int:
        count = 0
        with self.database.transaction() as connection:
            with connection.cursor() as cursor:
                for chapter in snapshot.chapters:
                    cursor.execute(
                        """
                        INSERT INTO ms_source_chapters
                          (source_work_id, source_chapter_key, source_chapter_hash, source_url,
                           label_raw, chapter_number, title_raw, published_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        ON DUPLICATE KEY UPDATE
                          source_url = VALUES(source_url),
                          label_raw = VALUES(label_raw),
                          chapter_number = VALUES(chapter_number),
                          title_raw = VALUES(title_raw),
                          published_at = COALESCE(VALUES(published_at), published_at),
                          last_seen_at = CURRENT_TIMESTAMP,
                          updated_at = CURRENT_TIMESTAMP
                        """,
                        (
                            source_work_id,
                            chapter.source_chapter_key,
                            _hash_key(chapter.source_chapter_key),
                            chapter.source_url,
                            chapter.label,
                            chapter.number,
                            chapter.title,
                            chapter.published_at.replace(tzinfo=None) if chapter.published_at else None,
                        ),
                    )
                    count += 1
        return count

    def record_candidates(self, source_work_id: int, candidates: Sequence[tuple[CanonicalSeries, float, dict]]) -> None:
        if not candidates:
            return
        with self.database.transaction() as connection:
            with connection.cursor() as cursor:
                for rank, (series, score, evidence) in enumerate(candidates[:5], start=1):
                    cursor.execute(
                        """
                        INSERT INTO ms_match_candidates
                          (source_work_id, canonical_series_id, rank_position, score, evidence_json)
                        VALUES (%s, %s, %s, %s, %s)
                        ON DUPLICATE KEY UPDATE
                          rank_position = VALUES(rank_position),
                          score = VALUES(score),
                          evidence_json = VALUES(evidence_json),
                          updated_at = CURRENT_TIMESTAMP
                        """,
                        (source_work_id, series.id, rank, score, json.dumps(evidence, ensure_ascii=False)),
                    )

    def mark_poll_success(self, source_key: str, fetched_count: int, new_count: int) -> None:
        with self.database.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE ms_sources
                    SET last_success_at=CURRENT_TIMESTAMP, last_error_at=NULL, last_error=NULL,
                        last_feed_count=%s, last_new_count=%s, updated_at=CURRENT_TIMESTAMP
                    WHERE source_key=%s
                    """,
                    (fetched_count, new_count, source_key),
                )
                cursor.execute(
                    """
                    INSERT INTO ms_poll_runs (source_key, status, fetched_count, new_count, finished_at)
                    VALUES (%s, 'success', %s, %s, CURRENT_TIMESTAMP)
                    """,
                    (source_key, fetched_count, new_count),
                )

    def mark_poll_failure(self, source_key: str, message: str) -> None:
        with self.database.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE ms_sources
                    SET last_error_at=CURRENT_TIMESTAMP, last_error=%s, updated_at=CURRENT_TIMESTAMP
                    WHERE source_key=%s
                    """,
                    (message[:2000], source_key),
                )
                cursor.execute(
                    "INSERT INTO ms_poll_runs (source_key, status, error_message, finished_at) VALUES (%s, 'failed', %s, CURRENT_TIMESTAMP)",
                    (source_key, message[:2000]),
                )

    def source_health(self, source_keys: Sequence[str] | None = None) -> list[dict]:
        """Return source health and the most recent persisted polling telemetry."""
        with self.database.transaction() as connection:
            with connection.cursor() as cursor:
                sql = """
                    SELECT s.source_key, s.display_name, s.base_url, s.enabled, s.priority,
                           s.last_success_at, s.last_error_at, s.last_error,
                           s.last_feed_count, s.last_new_count,
                           r.id AS poll_run_id, r.status AS poll_status,
                           r.fetched_count AS poll_fetched_count,
                           r.new_count AS poll_new_count,
                           r.pages_scanned, r.known_streak, r.stop_reason,
                           r.error_message AS poll_error,
                           r.started_at AS poll_started_at, r.finished_at AS poll_finished_at
                    FROM ms_sources AS s
                    LEFT JOIN ms_poll_runs AS r
                      ON r.id = (
                          SELECT MAX(recent.id)
                          FROM ms_poll_runs AS recent
                          WHERE recent.source_key=s.source_key
                      )
                """
                params: list = []
                if source_keys:
                    placeholders = ", ".join("%s" for _ in source_keys)
                    sql += f" WHERE s.source_key IN ({placeholders})"
                    params.extend(source_keys)
                sql += " ORDER BY s.priority ASC, s.source_key ASC"
                cursor.execute(sql, params)
                return list(cursor.fetchall())

    def list_promotion_candidates(
        self,
        source_keys: Sequence[str] | None = None,
        *,
        limit: int = 100,
    ) -> list[dict]:
        """List safe-to-review proposals without mutating canonical tables."""
        if limit < 1:
            raise ValueError("limit must be at least 1")
        with self.database.transaction() as connection:
            with connection.cursor() as cursor:
                source_filter, source_params = self._source_filter(source_keys, "sw.source_key")
                missing_series_sql = f"""
                    SELECT 'create_series' AS action, sw.id AS source_work_id,
                           NULL AS source_chapter_id, NULL AS canonical_series_id,
                           sw.source_key, sw.title_raw AS proposed_title,
                           NULL AS proposed_chapter_number,
                           NULL AS proposed_chapter_title,
                           sw.match_status, sw.match_score, sw.summary_raw,
                           sw.cover_url, sw.payload_json
                    FROM ms_source_works AS sw
                    WHERE sw.canonical_series_id IS NULL
                      AND sw.match_status IN ('unmatched','candidate')
                      {source_filter}
                    ORDER BY sw.last_seen_at DESC, sw.id DESC
                    LIMIT %s
                """
                cursor.execute(missing_series_sql, [*source_params, limit])
                candidates = list(cursor.fetchall())

                missing_chapter_sql = f"""
                    SELECT 'create_chapter' AS action, sw.id AS source_work_id,
                           sc.id AS source_chapter_id, sw.canonical_series_id,
                           sw.source_key,
                           sw.title_raw AS proposed_title,
                           sc.chapter_number AS proposed_chapter_number,
                           COALESCE(sc.title_raw, sc.label_raw) AS proposed_chapter_title,
                           sw.match_status, sw.match_score, sc.source_url,
                           sc.access_status
                    FROM ms_source_works AS sw
                    INNER JOIN ms_source_chapters AS sc ON sc.source_work_id=sw.id
                    WHERE sw.canonical_series_id IS NOT NULL
                      AND sc.canonical_chapter_id IS NULL
                      AND sc.chapter_number IS NOT NULL
                      AND NOT EXISTS (
                          SELECT 1
                          FROM chapters AS c
                          WHERE c.series_id=sw.canonical_series_id
                            AND c.deleted_at IS NULL
                            AND c.chapter_number=sc.chapter_number
                      )
                      {source_filter}
                    ORDER BY sc.last_seen_at DESC, sc.id DESC
                    LIMIT %s
                """
                cursor.execute(missing_chapter_sql, [*source_params, limit])
                candidates.extend(cursor.fetchall())
                self._attach_candidate_evidence_cursor(cursor, candidates)
                candidates.sort(
                    key=lambda row: (
                        row.get("source_key", ""),
                        -(row.get("source_work_id") or 0),
                        -(row.get("source_chapter_id") or 0),
                    )
                )
                return candidates[:limit]

    @staticmethod
    def _attach_candidate_evidence_cursor(cursor, candidates: list[dict]) -> None:
        source_work_ids = sorted({int(row["source_work_id"]) for row in candidates})
        if not source_work_ids:
            return
        placeholders = ", ".join("%s" for _ in source_work_ids)
        cursor.execute(
            f"""
            SELECT mc.source_work_id, mc.canonical_series_id, mc.rank_position,
                   mc.score, mc.evidence_json, s.title AS canonical_title
            FROM ms_match_candidates AS mc
            INNER JOIN series AS s ON s.id=mc.canonical_series_id
            WHERE mc.source_work_id IN ({placeholders})
            ORDER BY mc.source_work_id, mc.rank_position
            """,
            source_work_ids,
        )
        by_work: dict[int, list[dict]] = {}
        for row in cursor.fetchall():
            by_work.setdefault(int(row["source_work_id"]), []).append(
                {
                    "canonical_series_id": int(row["canonical_series_id"]),
                    "canonical_title": row["canonical_title"],
                    "rank_position": int(row["rank_position"]),
                    "score": float(row["score"]),
                    "evidence": _json_or_value(row["evidence_json"]),
                }
            )
        for candidate in candidates:
            candidate["match_candidates"] = by_work.get(int(candidate["source_work_id"]), [])

    @staticmethod
    def _source_filter(source_keys: Sequence[str] | None, column: str) -> tuple[str, list[str]]:
        if not source_keys:
            return "", []
        values = sorted(set(source_keys))
        placeholders = ", ".join("%s" for _ in values)
        return f"AND {column} IN ({placeholders})", values

    def enqueue_promotion_candidates(self, candidates: Sequence[dict]) -> int:
        """Idempotently store review proposals in the promotion queue."""
        if not candidates:
            return 0
        with self.database.transaction() as connection:
            with connection.cursor() as cursor:
                queued = 0
                for candidate in candidates:
                    action = str(candidate["action"])
                    source_work_id = int(candidate["source_work_id"])
                    source_chapter_id = candidate.get("source_chapter_id")
                    target_key = f"{action}:{source_chapter_id or source_work_id}"
                    evidence = {
                        key: value
                        for key, value in candidate.items()
                        if key not in {"source_chapter_id", "proposed_title", "proposed_chapter_title"}
                    }
                    cursor.execute(
                        """
                        INSERT INTO ms_promotion_queue
                          (action, target_key, source_work_id, source_chapter_id,
                           canonical_series_id, proposed_title, proposed_chapter_number,
                           proposed_chapter_title, evidence_json)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON DUPLICATE KEY UPDATE
                          canonical_series_id=VALUES(canonical_series_id),
                          proposed_title=VALUES(proposed_title),
                          proposed_chapter_number=VALUES(proposed_chapter_number),
                          proposed_chapter_title=VALUES(proposed_chapter_title),
                          evidence_json=VALUES(evidence_json),
                          updated_at=CURRENT_TIMESTAMP
                        """,
                        (
                            action,
                            target_key,
                            source_work_id,
                            source_chapter_id,
                            candidate.get("canonical_series_id"),
                            str(candidate.get("proposed_title") or "")[:500],
                            candidate.get("proposed_chapter_number"),
                            str(candidate.get("proposed_chapter_title") or "")[:500] or None,
                            json.dumps(evidence, ensure_ascii=False, default=str),
                        ),
                    )
                    queued += 1
                return queued

    def list_promotion_queue(
        self,
        *,
        status: str = "pending",
        source_keys: Sequence[str] | None = None,
        limit: int = 100,
    ) -> list[dict]:
        allowed = {"pending", "approved", "rejected", "applied"}
        if status not in allowed:
            raise ValueError(f"Unsupported promotion status: {status}")
        if limit < 1:
            raise ValueError("limit must be at least 1")
        with self.database.transaction() as connection:
            with connection.cursor() as cursor:
                source_filter, source_params = self._source_filter(source_keys, "sw.source_key")
                cursor.execute(
                    f"""
                    SELECT q.id, q.action, q.target_key, q.source_work_id,
                           q.source_chapter_id, q.canonical_series_id,
                           q.proposed_title, q.proposed_chapter_number,
                           q.proposed_chapter_title, q.evidence_json, q.status,
                           q.review_note, q.applied_series_id, q.applied_chapter_id,
                           q.created_at, q.updated_at, sw.source_key
                    FROM ms_promotion_queue AS q
                    INNER JOIN ms_source_works AS sw ON sw.id=q.source_work_id
                    WHERE q.status=%s {source_filter}
                    ORDER BY q.created_at DESC, q.id DESC
                    LIMIT %s
                    """,
                    [status, *source_params, limit],
                )
                return list(cursor.fetchall())

    def review_promotion_queue(
        self,
        queue_ids: Sequence[int],
        *,
        status: str,
        note: str | None = None,
    ) -> int:
        """Approve or reject only pending proposals, never already-applied rows."""
        if status not in {"approved", "rejected"}:
            raise ValueError(f"Unsupported review status: {status}")
        ids = sorted({int(queue_id) for queue_id in queue_ids})
        if not ids:
            raise ValueError("At least one promotion queue id is required")
        placeholders = ", ".join("%s" for _ in ids)
        with self.database.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    UPDATE ms_promotion_queue
                    SET status=%s, review_note=%s, reviewed_at=CURRENT_TIMESTAMP,
                        updated_at=CURRENT_TIMESTAMP
                    WHERE status='pending' AND id IN ({placeholders})
                    """,
                    [status, (note or "")[:500] or None, *ids],
                )
                return cursor.rowcount

    def apply_approved_promotions(
        self,
        *,
        source_keys: Sequence[str] | None = None,
        limit: int = 20,
    ) -> list[dict]:
        """Apply approved proposals one transaction at a time.

        A blocked proposal stays approved so an operator can fix the missing
        evidence and retry it. Unexpected errors are returned per item after
        their transaction has been rolled back.
        """
        if limit < 1:
            raise ValueError("limit must be at least 1")
        source_filter, source_params = self._source_filter(source_keys, "sw.source_key")
        with self.database.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    SELECT q.id
                    FROM ms_promotion_queue AS q
                    INNER JOIN ms_source_works AS sw ON sw.id=q.source_work_id
                    WHERE q.status='approved' {source_filter}
                    ORDER BY q.created_at, q.id
                    LIMIT %s
                    """,
                    [*source_params, limit],
                )
                queue_ids = [int(row["id"]) for row in cursor.fetchall()]

        results: list[dict] = []
        for queue_id in queue_ids:
            try:
                results.append(self._apply_promotion(queue_id))
            except Exception as error:
                results.append(
                    {
                        "queue_id": queue_id,
                        "status": "error",
                        "error": f"{type(error).__name__}: {error}",
                    }
                )
        return results

    def _apply_promotion(self, queue_id: int) -> dict:
        with self.database.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id, action, source_work_id, source_chapter_id,
                           canonical_series_id, proposed_title, proposed_chapter_number,
                           proposed_chapter_title, status
                    FROM ms_promotion_queue
                    WHERE id=%s
                    FOR UPDATE
                    """,
                    (queue_id,),
                )
                queue = cursor.fetchone()
                if not queue:
                    return {"queue_id": queue_id, "status": "skipped", "reason": "not_found"}
                if queue["status"] != "approved":
                    return {
                        "queue_id": queue_id,
                        "status": "skipped",
                        "reason": f"status_{queue['status']}",
                    }

                if queue["action"] == "create_series":
                    return self._apply_series_promotion_cursor(cursor, queue)
                if queue["action"] == "create_chapter":
                    return self._apply_chapter_promotion_cursor(cursor, queue)
                raise ValueError(f"Unsupported promotion action: {queue['action']}")

    def auto_create_low_confidence_series(
        self,
        source_work_id: int,
        *,
        score_threshold: float = 0.60,
    ) -> dict:
        """Create a canonical series only when matching found no safe candidate.

        This is intentionally narrower than the review queue: only an
        enriched source work with ``match_status='unmatched'`` and a score
        below the configured threshold qualifies. Candidate/ambiguous works
        remain available for later evidence or human review.
        """
        if not 0 < score_threshold <= 1:
            raise ValueError("score_threshold must be between 0 and 1")
        with self.database.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id, source_key, source_work_key, source_url, title_raw,
                           alt_titles_json, summary_raw, cover_url, tags_json, payload_json,
                           canonical_series_id, match_status, match_score, detail_fetched_at
                    FROM ms_source_works
                    WHERE id=%s
                    FOR UPDATE
                    """,
                    (source_work_id,),
                )
                work = cursor.fetchone()
                if not work:
                    return {
                        "status": "skipped",
                        "reason": "source_work_not_found",
                        "source_work_id": int(source_work_id),
                    }
                if work["canonical_series_id"] is not None:
                    return {
                        "status": "already_mapped",
                        "source_work_id": int(source_work_id),
                        "canonical_series_id": int(work["canonical_series_id"]),
                    }
                if work["detail_fetched_at"] is None:
                    return {
                        "status": "skipped",
                        "reason": "details_not_enriched",
                        "source_work_id": int(source_work_id),
                    }
                # A temporary/incomplete candidate scan must not create a
                # second canonical series when the database already has the
                # exact same title. This also protects legacy copies whose
                # related tables contain references to unused series ids.
                exact_title_series = self._find_exact_title_series_cursor(cursor, work["title_raw"])
                if exact_title_series is not None:
                    exact_series_id = int(exact_title_series["id"])
                    cursor.execute(
                        """
                        UPDATE ms_source_works
                        SET canonical_series_id=%s, match_status='matched',
                            match_score=1.000000, updated_at=CURRENT_TIMESTAMP
                        WHERE id=%s AND canonical_series_id IS NULL
                        """,
                        (exact_series_id, source_work_id),
                    )
                    if cursor.rowcount != 1:
                        raise RuntimeError("source work mapping changed during exact-title guard")
                    self._update_series_story_status_cursor(
                        cursor,
                        exact_series_id,
                        story_status_from_payload(_json_or_value(work["payload_json"])),
                    )
                    return {
                        "status": "already_mapped",
                        "reason": "exact_title_guard",
                        "source_work_id": int(source_work_id),
                        "canonical_series_id": exact_series_id,
                    }
                score = float(work["match_score"] or 0.0)
                if work["match_status"] != "unmatched":
                    return {
                        "status": "skipped",
                        "reason": f"match_status_{work['match_status']}",
                        "source_work_id": int(source_work_id),
                        "score": score,
                    }
                if score >= score_threshold:
                    return {
                        "status": "skipped",
                        "reason": "score_above_threshold",
                        "source_work_id": int(source_work_id),
                        "score": score,
                        "threshold": score_threshold,
                    }
                series_id = self._create_canonical_series_cursor(
                    cursor,
                    work,
                    match_status="auto_created",
                    match_score=score,
                )
                return {
                    "status": "created",
                    "action": "create_series",
                    "source_work_id": int(source_work_id),
                    "canonical_series_id": series_id,
                    "score": score,
                    "threshold": score_threshold,
                }

    def _create_canonical_series_cursor(
        self,
        cursor,
        work: dict,
        *,
        match_status: str,
        match_score: float,
    ) -> int:
        series_id = self._allocate_series_id_cursor(cursor)
        story_status = story_status_from_payload(_json_or_value(work["payload_json"]))
        source_payload = json.dumps(
            {
                "managed_by": "mangastar_multisource",
                "creation_mode": "auto_new_series" if match_status == "auto_created" else "approved_promotion",
                "source_key": work["source_key"],
                "source_work_id": int(work["id"]),
                "source_work_key": work["source_work_key"],
                "source_url": work["source_url"],
                "cover_url": work["cover_url"],
                "alternative_titles": _json_or_value(work["alt_titles_json"]),
                "tags": _json_or_value(work["tags_json"]),
                "payload": _json_or_value(work["payload_json"]),
                "story_status": story_status,
            },
            ensure_ascii=False,
            default=str,
        )
        cursor.execute(
            """
            INSERT INTO series
              (id, title, summary, cover, banner, total_chapters, rating,
               rates_count, series_views, translation_status, story_status,
               is_oneshot, over17, created_at, source_payload, scraped_at)
            VALUES (%s, %s, %s, %s, NULL, 0, 0, 0, 0, NULL, %s,
                    0, 0, CURRENT_TIMESTAMP, %s, CURRENT_TIMESTAMP)
            """,
            (
                series_id,
                _bounded_text(work["title_raw"], 255),
                _clean_summary_for_storage(work["summary_raw"]),
                _bounded_text(work["cover_url"], 255),
                story_status,
                source_payload,
            ),
        )
        cursor.execute(
            """
            UPDATE ms_source_works
            SET canonical_series_id=%s, match_status=%s, match_score=%s,
                updated_at=CURRENT_TIMESTAMP
            WHERE id=%s AND canonical_series_id IS NULL
            """,
            (series_id, match_status, match_score, work["id"]),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("source work mapping changed while creating series")
        return series_id

    def _apply_series_promotion_cursor(self, cursor, queue: dict) -> dict:
        cursor.execute(
            """
            SELECT id, source_key, source_work_key, source_url, title_raw,
                   alt_titles_json, summary_raw, cover_url, tags_json, payload_json,
                   canonical_series_id, match_status
            FROM ms_source_works
            WHERE id=%s
            FOR UPDATE
            """,
            (queue["source_work_id"],),
        )
        work = cursor.fetchone()
        if not work:
            raise ValueError("source work no longer exists")
        if work["canonical_series_id"] is not None:
            self._mark_promotion_applied_cursor(
                cursor,
                queue["id"],
                series_id=int(work["canonical_series_id"]),
                chapter_id=None,
                note="Source work was already mapped before approval was applied.",
            )
            return {
                "queue_id": int(queue["id"]),
                "action": "create_series",
                "status": "already_applied",
                "canonical_series_id": int(work["canonical_series_id"]),
            }

        series_id = self._create_canonical_series_cursor(
            cursor,
            work,
            match_status="matched",
            match_score=1.0,
        )
        self._mark_promotion_applied_cursor(
            cursor,
            queue["id"],
            series_id=series_id,
            chapter_id=None,
            note="Canonical series created; chapters require separate approved promotions.",
        )
        return {
            "queue_id": int(queue["id"]),
            "action": "create_series",
            "status": "applied",
            "canonical_series_id": series_id,
        }

    def _apply_chapter_promotion_cursor(self, cursor, queue: dict) -> dict:
        source_chapter_id = queue.get("source_chapter_id")
        if source_chapter_id is None:
            raise ValueError("create_chapter proposal has no source chapter")
        cursor.execute(
            """
            SELECT sw.id AS source_work_id, sw.source_key, sw.canonical_series_id,
                   sc.id, sc.source_chapter_hash, sc.source_url, sc.label_raw,
                   sc.chapter_number, sc.title_raw, sc.published_at,
                   sc.canonical_chapter_id, sc.access_status, sc.pages_fetched_at
            FROM ms_source_chapters AS sc
            INNER JOIN ms_source_works AS sw ON sw.id=sc.source_work_id
            WHERE sc.id=%s
            FOR UPDATE
            """,
            (source_chapter_id,),
        )
        chapter = cursor.fetchone()
        if not chapter:
            raise ValueError("source chapter no longer exists")
        if int(chapter["source_work_id"]) != int(queue["source_work_id"]):
            raise ValueError("source chapter does not belong to queued source work")
        if chapter["canonical_series_id"] is None:
            raise ValueError("source work is no longer mapped to a canonical series")
        if queue["canonical_series_id"] is not None and int(chapter["canonical_series_id"]) != int(queue["canonical_series_id"]):
            raise ValueError("canonical series mapping changed after approval")
        if chapter["canonical_chapter_id"] is not None:
            self._mark_promotion_applied_cursor(
                cursor,
                queue["id"],
                series_id=int(chapter["canonical_series_id"]),
                chapter_id=int(chapter["canonical_chapter_id"]),
                note="Source chapter was already mapped before approval was applied.",
            )
            return {
                "queue_id": int(queue["id"]),
                "action": "create_chapter",
                "status": "already_applied",
                "canonical_series_id": int(chapter["canonical_series_id"]),
                "canonical_chapter_id": int(chapter["canonical_chapter_id"]),
            }
        if chapter["access_status"] != "available" or chapter["pages_fetched_at"] is None:
            raise PromotionBlocked(
                "chapter needs verified pages before it can be promoted "
                f"(access_status={chapter['access_status']}, pages_fetched_at={chapter['pages_fetched_at']})"
            )
        if chapter["chapter_number"] is None:
            raise PromotionBlocked("chapter has no normalized chapter number")

        cursor.execute(
            """
            SELECT id, page_order, image_url, width, height, canonical_page_id
            FROM ms_source_pages
            WHERE source_chapter_id=%s
            ORDER BY page_order, id
            FOR UPDATE
            """,
            (source_chapter_id,),
        )
        pages = list(cursor.fetchall())
        if not pages:
            raise PromotionBlocked("chapter has no stored source pages")
        for page in pages:
            image_url = str(page["image_url"] or "").strip()
            if not image_url:
                raise PromotionBlocked(f"source page {page['id']} has an empty image URL")
            if len(image_url) > 500:
                raise PromotionBlocked(
                    f"source page {page['id']} URL exceeds canonical pages.image_url limit"
                )

        series_id = int(chapter["canonical_series_id"])
        source_key = str(chapter["source_chapter_hash"])
        cursor.execute(
            """
            SELECT id, chapter_number, source_key
            FROM chapters
            WHERE series_id=%s AND (chapter_ref_id=%s OR source_key=%s)
            FOR UPDATE
            """,
            (series_id, source_chapter_id, source_key),
        )
        existing_chapters = list(cursor.fetchall())
        if len(existing_chapters) > 1:
            raise PromotionBlocked("multiple canonical chapters already claim this source identity")

        if existing_chapters:
            canonical_chapter_id = int(existing_chapters[0]["id"])
            if existing_chapters[0]["chapter_number"] != chapter["chapter_number"]:
                raise PromotionBlocked("existing canonical chapter number conflicts with source chapter")
        else:
            cursor.execute(
                """
                INSERT INTO chapters
                  (chapter_ref_id, series_id, team_id, views, chapter_number, season,
                   title, storage_key, source_url, source_key, source_release_date,
                   chapter_views)
                VALUES (%s, %s, NULL, 0, %s, NULL, %s, NULL, %s, %s, %s, 0)
                """,
                (
                    source_chapter_id,
                    series_id,
                    chapter["chapter_number"],
                    _bounded_text(chapter["title_raw"] or chapter["label_raw"], 255),
                    _bounded_text(chapter["source_url"], 500),
                    source_key,
                    _format_release_date(chapter["published_at"]),
                ),
            )
            canonical_chapter_id = int(cursor.lastrowid)

        cursor.execute(
            "SELECT id, page_order, image_url FROM pages WHERE chapter_id=%s ORDER BY page_order, id FOR UPDATE",
            (canonical_chapter_id,),
        )
        existing_pages = list(cursor.fetchall())
        if existing_pages and len(existing_pages) != len(pages):
            raise PromotionBlocked("canonical chapter already has a different page count")
        canonical_page_ids: list[int] = []
        if existing_pages:
            for source_page, canonical_page in zip(pages, existing_pages, strict=True):
                if int(source_page["page_order"]) != int(canonical_page["page_order"]):
                    raise PromotionBlocked("canonical page order conflicts with source pages")
                canonical_page_ids.append(int(canonical_page["id"]))
        else:
            for page in pages:
                if page["canonical_page_id"] is not None:
                    cursor.execute(
                        "SELECT chapter_id FROM pages WHERE id=%s FOR UPDATE",
                        (page["canonical_page_id"],),
                    )
                    claimed = cursor.fetchone()
                    if claimed and int(claimed["chapter_id"]) != canonical_chapter_id:
                        raise PromotionBlocked("source page is already mapped to another canonical chapter")
                cursor.execute(
                    """
                    INSERT INTO pages (chapter_id, page_order, image_url, width, height)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (
                        canonical_chapter_id,
                        page["page_order"],
                        str(page["image_url"]).strip(),
                        page["width"],
                        page["height"],
                    ),
                )
                canonical_page_ids.append(int(cursor.lastrowid))

        for page, canonical_page_id in zip(pages, canonical_page_ids, strict=True):
            cursor.execute(
                "UPDATE ms_source_pages SET canonical_page_id=%s, updated_at=CURRENT_TIMESTAMP WHERE id=%s",
                (canonical_page_id, page["id"]),
            )
        cursor.execute(
            """
            UPDATE ms_source_chapters
            SET canonical_chapter_id=%s, match_status='matched', match_score=1.000000,
                updated_at=CURRENT_TIMESTAMP
            WHERE id=%s AND canonical_chapter_id IS NULL
            """,
            (canonical_chapter_id, source_chapter_id),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("source chapter mapping changed while applying chapter")
        cursor.execute(
            """
            UPDATE series
            SET total_chapters=(
                SELECT COUNT(*) FROM chapters
                WHERE series_id=%s AND deleted_at IS NULL
            )
            WHERE id=%s
            """,
            (series_id, series_id),
        )
        self._refresh_series_latest_chapter_cursor(cursor, series_id)
        self._mark_promotion_applied_cursor(
            cursor,
            queue["id"],
            series_id=series_id,
            chapter_id=canonical_chapter_id,
            note="Canonical chapter and verified pages created.",
        )
        return {
            "queue_id": int(queue["id"]),
            "action": "create_chapter",
            "status": "applied",
            "canonical_series_id": series_id,
            "canonical_chapter_id": canonical_chapter_id,
            "pages": len(pages),
        }

    @staticmethod
    def _refresh_series_latest_chapter_cursor(cursor, series_id: int) -> None:
        """Keep the canonical latest-chapter pointer in sync with chapters."""
        cursor.execute(
            """
            SELECT id
            FROM chapters
            WHERE series_id=%s AND deleted_at IS NULL
            ORDER BY chapter_number DESC,
                     COALESCE(created_at, '1000-01-01 00:00:00') DESC,
                     id DESC
            LIMIT 1
            """,
            (series_id,),
        )
        latest = cursor.fetchone()
        if latest is None:
            cursor.execute(
                "DELETE FROM series_latest_chapters WHERE series_id=%s",
                (series_id,),
            )
            return
        # The legacy table can contain a stale pointer whose chapter now
        # belongs to another series (for example after a merge). Because
        # latest_chapter_id is globally unique, remove only that incorrect
        # pointer before writing the verified series/chapter pair.
        cursor.execute(
            """
            DELETE FROM series_latest_chapters
            WHERE latest_chapter_id=%s AND series_id<>%s
            """,
            (latest["id"], series_id),
        )
        cursor.execute(
            """
            INSERT INTO series_latest_chapters (series_id, latest_chapter_id)
            VALUES (%s, %s)
            ON DUPLICATE KEY UPDATE
                latest_chapter_id=VALUES(latest_chapter_id),
                updated_at=CURRENT_TIMESTAMP
            """,
            (series_id, latest["id"]),
        )

    def _allocate_series_id_cursor(self, cursor) -> int:
        # Some legacy copies were created without foreign keys and can contain
        # chapter/tag/latest rows that reference a series id before its series
        # row exists. Looking only at MAX(series.id) could therefore reuse one
        # of those ids and silently adopt unrelated legacy data.
        cursor.execute(
            """
            SELECT TABLE_NAME, COLUMN_NAME
            FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA=DATABASE()
              AND COLUMN_NAME IN ('series_id', 'canonical_series_id', 'applied_series_id')
            ORDER BY TABLE_NAME, COLUMN_NAME
            """
        )
        reference_columns = list(cursor.fetchall())
        minimum_next_id = 1
        for reference in reference_columns:
            table_name = str(reference["TABLE_NAME"]).replace("`", "``")
            column_name = str(reference["COLUMN_NAME"]).replace("`", "``")
            cursor.execute(
                f"SELECT COALESCE(MAX(`{column_name}`), 0) + 1 AS minimum_next_id "
                f"FROM `{table_name}`"
            )
            minimum_next_id = max(
                minimum_next_id,
                int(cursor.fetchone()["minimum_next_id"]),
            )
        cursor.execute(
            "SELECT next_id FROM ms_canonical_id_sequences WHERE entity='series' FOR UPDATE"
        )
        row = cursor.fetchone()
        if row is None:
            next_id = minimum_next_id
            cursor.execute(
                "INSERT INTO ms_canonical_id_sequences (entity, next_id) VALUES ('series', %s)",
                (next_id + 1,),
            )
        else:
            next_id = max(int(row["next_id"]), minimum_next_id)
            cursor.execute(
                "UPDATE ms_canonical_id_sequences SET next_id=%s, updated_at=CURRENT_TIMESTAMP WHERE entity='series'",
                (next_id + 1,),
            )
        return next_id

    @staticmethod
    def _mark_promotion_applied_cursor(
        cursor,
        queue_id: int,
        *,
        series_id: int,
        chapter_id: int | None,
        note: str,
    ) -> None:
        cursor.execute(
            """
            UPDATE ms_promotion_queue
            SET status='applied', applied_series_id=%s, applied_chapter_id=%s,
                applied_at=CURRENT_TIMESTAMP, review_note=%s, updated_at=CURRENT_TIMESTAMP
            WHERE id=%s AND status='approved'
            """,
            (series_id, chapter_id, note[:500], queue_id),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("promotion queue row changed while applying")

    def list_canonical_series(self, *, include_metadata: bool = True) -> list[CanonicalSeries]:
        with self.database.transaction() as connection:
            with connection.cursor() as cursor:
                # Summaries are LONGTEXT and the copy database contains more
                # than twenty thousand canonical rows. Fetching the whole
                # result in one remote query can exceed the hosting layer's
                # statement/socket window even though the row count is modest.
                # Keyset batches keep each result bounded and avoid OFFSET
                # scans while preserving deterministic ordering.
                rows: list[dict] = []
                last_series_id = 0
                while True:
                    columns = (
                        "id, title, summary, cover, author_name, painter_name, "
                        "publisher_name, type_name, story_status, translation_status, "
                        "is_oneshot, release_date"
                        if include_metadata
                        else "id, title"
                    )
                    cursor.execute(
                        f"""
                        SELECT {columns}
                        FROM series
                        WHERE deleted_at IS NULL AND id > %s
                        ORDER BY id
                        LIMIT 2000
                        """,
                        (last_series_id,),
                    )
                    batch = list(cursor.fetchall())
                    if not batch:
                        break
                    rows.extend(batch)
                    last_series_id = int(batch[-1]["id"])
                    if len(batch) < 2000:
                        break

                aliases_by_series: dict[int, list[str]] = {}
                if self._table_exists_cursor(cursor, "series_titles"):
                    cursor.execute(
                        """
                        SELECT st.series_id, st.title
                        FROM series_titles AS st
                        INNER JOIN series AS s ON s.id=st.series_id
                        WHERE s.deleted_at IS NULL
                        ORDER BY st.series_id, st.id
                        """
                    )
                    for alias in cursor.fetchall():
                        value = str(alias.get("title") or "").strip()
                        if value:
                            aliases_by_series.setdefault(int(alias["series_id"]), []).append(value)

                tags_by_series: dict[int, list[str]] = {}
                if include_metadata and self._table_exists_cursor(cursor, "series_tags") and self._table_exists_cursor(cursor, "tags"):
                    cursor.execute(
                        """
                        SELECT st.series_id, t.name
                        FROM series_tags AS st
                        INNER JOIN tags AS t ON t.id=st.tag_id
                        INNER JOIN series AS s ON s.id=st.series_id
                        WHERE s.deleted_at IS NULL
                        ORDER BY st.series_id, t.id
                        """
                    )
                    for tag in cursor.fetchall():
                        value = str(tag.get("name") or "").strip()
                        if value:
                            tags_by_series.setdefault(int(tag["series_id"]), []).append(value)

                chapter_stats: dict[int, dict] = {}
                if include_metadata:
                    cursor.execute(
                        """
                        SELECT c.series_id, COUNT(*) AS chapter_count,
                               MAX(c.chapter_number) AS latest_chapter_number,
                               MAX(c.created_at) AS latest_chapter_at
                        FROM chapters AS c
                        INNER JOIN series AS s ON s.id=c.series_id
                        WHERE c.deleted_at IS NULL AND s.deleted_at IS NULL
                        GROUP BY c.series_id
                        """
                    )
                    chapter_stats = {
                        int(stat["series_id"]): stat
                        for stat in cursor.fetchall()
                    }

                canonical: list[CanonicalSeries] = []
                for row in rows:
                    series_id = int(row["id"])
                    stats = chapter_stats.get(series_id, {})
                    canonical.append(
                        CanonicalSeries(
                            id=series_id,
                            title=row["title"],
                            summary=row.get("summary"),
                            cover=row.get("cover"),
                            author_name=row.get("author_name"),
                            type_name=row.get("type_name"),
                            alternative_titles=tuple(aliases_by_series.get(series_id, ())),
                            tags=tuple(tags_by_series.get(series_id, ())),
                            painter_name=row.get("painter_name"),
                            publisher_name=row.get("publisher_name"),
                            story_status=row.get("story_status"),
                            translation_status=row.get("translation_status"),
                            is_oneshot=bool(row.get("is_oneshot")),
                            release_date=row.get("release_date"),
                            chapter_count=int(stats.get("chapter_count") or 0),
                            latest_chapter_number=stats.get("latest_chapter_number"),
                            latest_chapter_at=stats.get("latest_chapter_at"),
                        )
                    )
                return canonical

    def hydrate_canonical_series_metadata(
        self,
        candidates: list[CanonicalSeries],
        series_ids: set[int],
    ) -> list[CanonicalSeries]:
        """Load expensive fields only for title-matched canonical candidates."""
        requested_ids = sorted({int(series_id) for series_id in series_ids})
        # Keep hydration bounded, but allow the normal multi-source batch
        # (usually a few thousand title-token candidates) to use metadata.
        if not requested_ids or len(requested_ids) > 10000:
            return candidates

        metadata_by_id: dict[int, dict] = {}
        tags_by_series: dict[int, list[str]] = {}
        chapter_stats: dict[int, dict] = {}
        with self.database.transaction() as connection:
            with connection.cursor() as cursor:
                for chunk_start in range(0, len(requested_ids), 500):
                    chunk = requested_ids[chunk_start:chunk_start + 500]
                    placeholders = ", ".join("%s" for _ in chunk)
                    cursor.execute(
                        f"""
                        SELECT id, summary, cover, author_name, painter_name,
                               publisher_name, type_name, story_status,
                               translation_status, is_oneshot, release_date
                        FROM series
                        WHERE deleted_at IS NULL AND id IN ({placeholders})
                        """,
                        chunk,
                    )
                    metadata_by_id.update({int(row["id"]): row for row in cursor.fetchall()})

                if self._table_exists_cursor(cursor, "series_tags") and self._table_exists_cursor(cursor, "tags"):
                    for chunk_start in range(0, len(requested_ids), 500):
                        chunk = requested_ids[chunk_start:chunk_start + 500]
                        placeholders = ", ".join("%s" for _ in chunk)
                        cursor.execute(
                            f"""
                            SELECT st.series_id, t.name
                            FROM series_tags AS st
                            INNER JOIN tags AS t ON t.id=st.tag_id
                            WHERE st.series_id IN ({placeholders})
                            ORDER BY st.series_id, t.id
                            """,
                            chunk,
                        )
                        for tag in cursor.fetchall():
                            value = str(tag.get("name") or "").strip()
                            if value:
                                tags_by_series.setdefault(int(tag["series_id"]), []).append(value)

                for chunk_start in range(0, len(requested_ids), 500):
                    chunk = requested_ids[chunk_start:chunk_start + 500]
                    placeholders = ", ".join("%s" for _ in chunk)
                    cursor.execute(
                        f"""
                        SELECT c.series_id, COUNT(*) AS chapter_count,
                               MAX(c.chapter_number) AS latest_chapter_number,
                               MAX(c.created_at) AS latest_chapter_at
                        FROM chapters AS c
                        WHERE c.deleted_at IS NULL AND c.series_id IN ({placeholders})
                        GROUP BY c.series_id
                        """,
                        chunk,
                    )
                    chapter_stats.update({int(row["series_id"]): row for row in cursor.fetchall()})

        hydrated: list[CanonicalSeries] = []
        for candidate in candidates:
            row = metadata_by_id.get(candidate.id)
            if row is None:
                hydrated.append(candidate)
                continue
            stats = chapter_stats.get(candidate.id, {})
            hydrated.append(
                CanonicalSeries(
                    id=candidate.id,
                    title=candidate.title,
                    summary=row.get("summary"),
                    cover=row.get("cover"),
                    author_name=row.get("author_name"),
                    type_name=row.get("type_name"),
                    alternative_titles=candidate.alternative_titles,
                    tags=tuple(tags_by_series.get(candidate.id, ())),
                    painter_name=row.get("painter_name"),
                    publisher_name=row.get("publisher_name"),
                    story_status=row.get("story_status"),
                    translation_status=row.get("translation_status"),
                    is_oneshot=bool(row.get("is_oneshot")),
                    release_date=row.get("release_date"),
                    chapter_count=int(stats.get("chapter_count") or 0),
                    latest_chapter_number=stats.get("latest_chapter_number"),
                    latest_chapter_at=stats.get("latest_chapter_at"),
                )
            )
        return hydrated

    @staticmethod
    def _table_exists_cursor(cursor, table_name: str) -> bool:
        cursor.execute("SHOW TABLES LIKE %s", (table_name,))
        return cursor.fetchone() is not None

    def counts(self) -> dict[str, int]:
        tables = ("ms_sources", "ms_source_works", "ms_source_chapters", "ms_source_pages", "ms_match_candidates", "ms_chapter_match_candidates", "ms_poll_runs")
        with self.database.transaction() as connection:
            with connection.cursor() as cursor:
                result = {}
                for table in tables:
                    cursor.execute(f"SELECT COUNT(*) AS count FROM {table}")
                    result[table] = int(cursor.fetchone()["count"])
                return result


class PromotionBlocked(RuntimeError):
    """Promotion is safe to retry after missing or conflicting evidence is fixed."""


def _bounded_text(value: object, limit: int) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text[:limit] if text else None


def _clean_summary_for_storage(value: object) -> str | None:
    """Prevent legacy/source HTML fragments from reaching the app database."""
    if value in (None, ""):
        return None
    parsed = BeautifulSoup(str(value), "html.parser")
    for line_break in parsed.find_all("br"):
        line_break.replace_with(" ")
    text = " ".join(parsed.get_text(" ", strip=True).split())
    return text or None


def _json_or_value(raw: object) -> object:
    if raw in (None, ""):
        return None
    try:
        return json.loads(str(raw))
    except (TypeError, ValueError):
        return str(raw)


def _format_release_date(value: object) -> str | None:
    if value is None:
        return None
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    return str(value)[:100]
