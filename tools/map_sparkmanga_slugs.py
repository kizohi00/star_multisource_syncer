#!/usr/bin/env python3
"""Map Manga Star series IDs to their SparkManga slugs.

This is an explicit, resumable maintenance tool.  It reads the canonical
series table, searches SparkManga through its WordPress AJAX endpoint, and
stores only the mapping/audit data in ``ms_sparkmanga_series_slugs``.

The tool is deliberately locked to databases whose name ends with ``_copy``.
It never writes to ``series``, ``chapters``, or any application/API table.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from bs4 import BeautifulSoup


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

import pymysql
from pymysql.connections import Connection

from mangastar_multisource.adapters.madara import SparkMangaAdapter
from mangastar_multisource.adapters.base import clean_text, extract_labeled_values
from mangastar_multisource.config import Settings, load_local_env
from mangastar_multisource.matching.metadata import normalize_title


TABLE_NAME = "ms_sparkmanga_series_slugs"
DEFAULT_BATCH_SIZE = 10
DEFAULT_NOT_FOUND_FILE = PROJECT_ROOT / "sparkmanga_unmatched.txt"
MATCHED = "matched"
AMBIGUOUS = "ambiguous"
NOT_FOUND = "not_found"
ERROR = "error"


@dataclass(frozen=True)
class SeriesRow:
    series_id: int
    title: str
    summary: str | None
    author_name: str | None
    painter_name: str | None
    type_name: str | None
    publisher_name: str | None


@dataclass(frozen=True)
class MappingResult:
    series_id: int
    source_title: str
    status: str
    spark_slug: str | None = None
    spark_url: str | None = None
    matched_title: str | None = None
    score: float | None = None
    margin: float | None = None
    evidence: dict[str, Any] | None = None
    reason: str = ""


def _clean(value: object | None) -> str:
    """Convert text and remove HTML that some AJAX responses include."""

    text = str(value or "")
    if "<" in text and ">" in text:
        text = clean_text(BeautifulSoup(text, "html.parser"))
    return " ".join(text.split()).strip()


def _limited(value: str | None, length: int) -> str | None:
    if value is None:
        return None
    value = _clean(value)
    return value[:length] if value else None


def _text_similarity(left: str | None, right: str | None) -> float:
    left_normalized = normalize_title(left)
    right_normalized = normalize_title(right)
    if not left_normalized or not right_normalized:
        return 0.0
    if left_normalized == right_normalized:
        return 1.0
    left_tokens = set(left_normalized.split())
    right_tokens = set(right_normalized.split())
    token_score = len(left_tokens & right_tokens) / len(left_tokens | right_tokens)
    sequence_score = SequenceMatcher(None, left_normalized, right_normalized).ratio()
    if left_normalized in right_normalized or right_normalized in left_normalized:
        containment_score = min(len(left_normalized), len(right_normalized)) / max(
            len(left_normalized), len(right_normalized)
        )
        return max(sequence_score, 0.88 + 0.12 * containment_score)
    return max(sequence_score, 0.65 * sequence_score + 0.35 * token_score)


def _slug_from_url(url: str) -> str | None:
    parsed = urlparse(url)
    if (parsed.hostname or "").casefold() != "sparkmanga.net":
        return None
    parts = [unquote(part).strip() for part in parsed.path.split("/") if part.strip()]
    try:
        manga_index = next(index for index, part in enumerate(parts) if part.casefold() == "manga")
    except StopIteration:
        return None
    if manga_index + 1 >= len(parts):
        return None
    slug = parts[manga_index + 1]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", slug):
        return None
    return slug


def _candidate_score(source_title: str, candidate: dict[str, str]) -> tuple[float, dict[str, Any]]:
    candidate_title = _clean(candidate.get("title"))
    slug = _slug_from_url(candidate.get("url", "")) or ""
    title_score = _text_similarity(source_title, candidate_title)
    slug_score = _text_similarity(source_title, slug.replace("-", " ").replace("_", " "))
    score = title_score if candidate_title else slug_score
    if candidate_title and slug_score:
        score = max(score, 0.82 * title_score + 0.18 * slug_score)
    return score, {
        "title_score": round(title_score, 6),
        "slug_score": round(slug_score, 6),
        "title": candidate_title,
        "url": candidate.get("url", ""),
        "slug": slug,
    }


def _candidate_identity(adapter: SparkMangaAdapter, url: str) -> dict[str, Any]:
    """Fetch only lightweight identity metadata for borderline candidates."""

    _, html = adapter._get_html(url)  # noqa: SLF001 - adapter owns the request policy.
    soup = BeautifulSoup(html, "html.parser")
    title_node = soup.select_one("h1.entry-title, .post-title h1, h1")
    title = clean_text(title_node) or ""
    summary = clean_text(
        soup.select_one(
            ".description-summary .summary__content, .summary__content, "
            ".summary_content, .description-summary"
        )
    )
    if not summary:
        description = soup.select_one("meta[name='description'], meta[property='og:description']")
        summary = _clean(description.get("content")) if description else ""
    author = extract_labeled_values(soup, ("author", "writer", "المؤلف", "الكاتب"))
    return {
        "title": title,
        "summary": summary,
        "author": author[0] if author else "",
    }


def _rank_and_choose(
    row: SeriesRow,
    candidates: list[dict[str, str]],
    adapter: SparkMangaAdapter,
) -> MappingResult:
    ranked: list[dict[str, Any]] = []
    seen_slugs: set[str] = set()
    for candidate in candidates:
        slug = _slug_from_url(candidate.get("url", ""))
        if not slug or slug in seen_slugs:
            continue
        seen_slugs.add(slug)
        score, evidence = _candidate_score(row.title, candidate)
        ranked.append({"candidate": candidate, "score": score, "evidence": evidence})

    ranked.sort(key=lambda item: item["score"], reverse=True)
    if not ranked:
        return MappingResult(
            row.series_id,
            row.title,
            NOT_FOUND,
            evidence={"queries_returned_candidates": 0},
            reason="SparkManga search returned no usable /manga/ candidate.",
        )

    exact = [
        item
        for item in ranked
        if normalize_title(row.title) == normalize_title(item["evidence"].get("title"))
    ]
    # A unique normalized exact title is strong enough to avoid opening another
    # page. This is important when processing thousands of series.
    if len(exact) == 1:
        chosen = exact[0]
        return _matched_result(row, chosen, ranked, "unique_normalized_exact_title")

    # Only borderline cases get detail-page checks. This keeps the operation
    # at roughly one AJAX request per series in the normal case.
    top = ranked[0]
    second_score = ranked[1]["score"] if len(ranked) > 1 else 0.0
    needs_details = (
        len(exact) > 1
        or top["score"] < 0.96
        or top["score"] - second_score < 0.10
    )
    if needs_details:
        for item in ranked[:3]:
            try:
                identity = _candidate_identity(adapter, item["candidate"]["url"])
            except Exception as exc:  # details are supporting evidence only
                item["evidence"]["detail_error"] = type(exc).__name__
                continue
            item["evidence"]["detail_title"] = identity["title"]
            item["evidence"]["summary_score"] = round(
                _text_similarity(row.summary, identity["summary"]), 6
            )
            item["evidence"]["author_score"] = round(
                _text_similarity(row.author_name, identity["author"]), 6
            )
            # Title remains dominant; summary and author only resolve close
            # candidates and never override a clear title mismatch.
            detail_title_score = _text_similarity(row.title, identity["title"])
            item["score"] = max(
                item["score"],
                0.78 * item["score"]
                + 0.15 * item["evidence"]["summary_score"]
                + 0.07 * item["evidence"]["author_score"],
                0.90 * item["score"] + 0.10 * detail_title_score,
            )
        ranked.sort(key=lambda item: item["score"], reverse=True)
        top = ranked[0]
        second_score = ranked[1]["score"] if len(ranked) > 1 else 0.0

    margin = top["score"] - second_score
    evidence = {
        "candidates": [
            {
                "slug": item["evidence"].get("slug"),
                "title": item["evidence"].get("title"),
                "url": item["evidence"].get("url"),
                "score": round(item["score"], 6),
            }
            for item in ranked[:5]
        ],
        "selection_rule": "ranked_title_and_borderline_detail_metadata",
    }
    if top["score"] >= 0.90 and margin >= 0.08:
        return _matched_result(row, top, ranked, "high_score_and_margin", evidence, margin)
    if top["score"] < 0.72:
        return MappingResult(
            row.series_id,
            row.title,
            NOT_FOUND,
            score=top["score"],
            margin=margin,
            evidence=evidence,
            reason="Candidates existed, but none reached the safe match threshold.",
        )
    return MappingResult(
        row.series_id,
        row.title,
        AMBIGUOUS,
        score=top["score"],
        margin=margin,
        evidence=evidence,
        reason="Multiple or borderline candidates; no mapping was guessed.",
    )


def _matched_result(
    row: SeriesRow,
    chosen: dict[str, Any],
    ranked: list[dict[str, Any]],
    rule: str,
    evidence: dict[str, Any] | None = None,
    margin: float | None = None,
) -> MappingResult:
    candidate = chosen["candidate"]
    slug = chosen["evidence"].get("slug") or _slug_from_url(candidate.get("url", ""))
    if margin is None:
        second = next((item["score"] for item in ranked if item is not chosen), 0.0)
        margin = chosen["score"] - second
    evidence = evidence or {
        "selection_rule": rule,
        "candidates": [
            {
                "slug": item["evidence"].get("slug"),
                "title": item["evidence"].get("title"),
                "url": item["evidence"].get("url"),
                "score": round(item["score"], 6),
            }
            for item in ranked[:5]
        ],
    }
    return MappingResult(
        row.series_id,
        row.title,
        MATCHED,
        spark_slug=slug,
        spark_url=candidate.get("url"),
        matched_title=_clean(candidate.get("title")) or slug.replace("-", " "),
        score=chosen["score"],
        margin=margin,
        evidence=evidence,
        reason=rule,
    )


def _process_one(
    row: SeriesRow,
    adapter: SparkMangaAdapter,
) -> MappingResult:
    if not row.title.strip():
        return MappingResult(row.series_id, row.title, NOT_FOUND, reason="Series has no title.")
    queries = [row.title.strip()]
    normalized_query = normalize_title(row.title)
    if normalized_query and normalized_query.casefold() != row.title.strip().casefold():
        queries.append(normalized_query)
    candidates: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    try:
        for query in queries:
            for candidate in adapter.search_series_candidates(query):
                url = candidate.get("url", "")
                if url and url not in seen_urls:
                    seen_urls.add(url)
                    candidates.append(candidate)
            if candidates:
                break
        return _rank_and_choose(row, candidates, adapter)
    except Exception as exc:
        return MappingResult(
            row.series_id,
            row.title,
            ERROR,
            evidence={"exception": type(exc).__name__},
            reason=str(exc)[:500],
        )


def _connect(settings: Settings) -> Connection:
    return pymysql.connect(
        host=settings.db_host,
        port=settings.db_port,
        user=settings.db_user,
        password=settings.db_password,
        database=settings.db_name,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=20,
        read_timeout=180,
        write_timeout=180,
        autocommit=False,
    )


def _ensure_table(connection: Connection) -> None:
    connection.cursor().execute(
        f"""
        CREATE TABLE IF NOT EXISTS `{TABLE_NAME}` (
            `series_id` BIGINT NOT NULL,
            `spark_slug` VARCHAR(255) NULL,
            `spark_url` VARCHAR(1024) NULL,
            `source_title` VARCHAR(512) NOT NULL,
            `matched_title` VARCHAR(512) NULL,
            `match_status` VARCHAR(32) NOT NULL,
            `match_score` DECIMAL(8,6) NULL,
            `match_margin` DECIMAL(8,6) NULL,
            `evidence_json` LONGTEXT NULL,
            `reason` VARCHAR(512) NULL,
            `checked_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (`series_id`),
            UNIQUE KEY `uq_{TABLE_NAME}_slug` (`spark_slug`),
            CONSTRAINT `fk_{TABLE_NAME}_series`
                FOREIGN KEY (`series_id`) REFERENCES `series` (`id`)
                ON DELETE CASCADE ON UPDATE CASCADE
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """
    )


def _load_series(connection: Connection, limit: int | None) -> list[SeriesRow]:
    sql = """
        SELECT id, title, summary, author_name, painter_name, type_name, publisher_name
        FROM series
        WHERE deleted_at IS NULL
        ORDER BY id ASC
    """
    if limit is not None:
        sql += " LIMIT %s"
        params: tuple[Any, ...] = (limit,)
    else:
        params = ()
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        return [
            SeriesRow(
                series_id=int(row["id"]),
                title=_clean(row["title"]),
                summary=_clean(row.get("summary")) or None,
                author_name=_clean(row.get("author_name")) or None,
                painter_name=_clean(row.get("painter_name")) or None,
                type_name=_clean(row.get("type_name")) or None,
                publisher_name=_clean(row.get("publisher_name")) or None,
            )
            for row in cursor.fetchall()
        ]


def _load_existing(connection: Connection) -> dict[int, dict[str, Any]]:
    with connection.cursor() as cursor:
        cursor.execute(
            f"SELECT series_id, spark_slug, match_status FROM `{TABLE_NAME}`"
        )
        return {int(row["series_id"]): row for row in cursor.fetchall()}


def _persist_batch(
    connection: Connection,
    results: list[MappingResult],
    occupied_slugs: dict[str, int],
) -> list[MappingResult]:
    """Persist one batch and prevent two series from claiming one slug."""

    normalized: list[MappingResult] = []
    batch_slugs: dict[str, int] = {}
    for result in results:
        if result.status == MATCHED and result.spark_slug:
            owner = occupied_slugs.get(result.spark_slug) or batch_slugs.get(result.spark_slug)
            if owner is not None and owner != result.series_id:
                result = MappingResult(
                    result.series_id,
                    result.source_title,
                    AMBIGUOUS,
                    score=result.score,
                    margin=result.margin,
                    evidence=result.evidence,
                    reason=f"Spark slug is already mapped to series_id={owner}.",
                )
            else:
                batch_slugs[result.spark_slug] = result.series_id
        normalized.append(result)

    try:
        with connection.cursor() as cursor:
            for result in normalized:
                cursor.execute(
                f"""
                INSERT INTO `{TABLE_NAME}`
                    (series_id, spark_slug, spark_url, source_title, matched_title,
                     match_status, match_score, match_margin, evidence_json, reason, checked_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
                ON DUPLICATE KEY UPDATE
                    spark_slug=VALUES(spark_slug),
                    spark_url=VALUES(spark_url),
                    source_title=VALUES(source_title),
                    matched_title=VALUES(matched_title),
                    match_status=VALUES(match_status),
                    match_score=VALUES(match_score),
                    match_margin=VALUES(match_margin),
                    evidence_json=VALUES(evidence_json),
                    reason=VALUES(reason),
                    checked_at=CURRENT_TIMESTAMP
                    """,
                    (
                        result.series_id,
                        _limited(result.spark_slug, 255),
                        _limited(result.spark_url, 1024),
                        _limited(result.source_title, 512) or "",
                        _limited(result.matched_title, 512),
                        result.status,
                        result.score,
                        result.margin,
                        json.dumps(result.evidence or {}, ensure_ascii=False, separators=(",", ":")),
                        _limited(result.reason, 512),
                    ),
                )
        connection.commit()
    except Exception:
        try:
            connection.rollback()
        except Exception:
            pass
        raise
    for result in normalized:
        if result.status == MATCHED and result.spark_slug:
            occupied_slugs[result.spark_slug] = result.series_id
    return normalized


def _is_retryable_db_error(exc: Exception) -> bool:
    return isinstance(exc, pymysql.err.OperationalError) and exc.args and exc.args[0] in {
        2003, 2006, 2013, 2014, 2055
    }


def _connect_until_available(settings: Settings) -> Connection:
    """Keep the maintenance job alive while the remote DB is unreachable."""

    attempt = 1
    while True:
        try:
            return _connect(settings)
        except Exception as exc:
            if not _is_retryable_db_error(exc):
                raise
            delay = min(5 * attempt, 30)
            print(
                f"[spark-slug-map] MySQL is temporarily unavailable; "
                f"retrying initial connection in {delay}s",
                flush=True,
            )
            time.sleep(delay)
            attempt += 1


def _reconnect(settings: Settings, old_connection: Connection) -> Connection:
    try:
        old_connection.close()
    except Exception:
        pass
    attempt = 1
    while True:
        try:
            return _connect(settings)
        except Exception as exc:
            if not _is_retryable_db_error(exc):
                raise
            delay = min(5 * attempt, 30)
            print(
                f"[spark-slug-map] MySQL is temporarily unavailable; "
                f"retrying connection in {delay}s",
                flush=True,
            )
            time.sleep(delay)
            attempt += 1


def _persist_with_reconnect(
    settings: Settings,
    connection: Connection,
    results: list[MappingResult],
    occupied_slugs: dict[str, int],
) -> tuple[Connection, list[MappingResult]]:
    """Retry a batch after a transient MySQL disconnect.

    Each batch is idempotent (primary key is series_id), so retrying after an
    uncertain connection failure cannot create duplicate mapping rows.
    """

    for attempt in range(1, 4):
        try:
            return connection, _persist_batch(connection, results, occupied_slugs)
        except Exception as exc:
            if not _is_retryable_db_error(exc) or attempt == 3:
                raise
            print(
                f"[spark-slug-map] database connection lost while saving batch; "
                f"reconnecting (attempt {attempt + 1}/3)",
                flush=True,
            )
            connection = _reconnect(settings, connection)
    raise AssertionError("unreachable")


def _append_unmatched(path: Path, results: list[MappingResult]) -> None:
    unmatched = [result for result in results if result.status != MATCHED]
    if not unmatched:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for result in unmatched:
            title = result.source_title.replace("\t", " ").replace("\n", " ")
            reason = result.reason.replace("\t", " ").replace("\n", " ")
            handle.write(f"{result.series_id}\t{result.status}\t{title}\t{reason}\n")


def _rewrite_unmatched(connection: Connection, path: Path) -> int:
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            SELECT series_id, source_title, match_status, reason
            FROM `{TABLE_NAME}`
            WHERE match_status <> 'matched'
            ORDER BY series_id ASC
            """
        )
        rows = cursor.fetchall()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        handle.write("series_id\tstatus\ttitle\treason\n")
        for row in rows:
            title = _clean(row.get("source_title")).replace("\t", " ").replace("\n", " ")
            reason = _clean(row.get("reason")).replace("\t", " ").replace("\n", " ")
            handle.write(f"{row['series_id']}\t{row['match_status']}\t{title}\t{reason}\n")
    return len(rows)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--limit", type=int, default=None, help="Process only the first N active series.")
    parser.add_argument(
        "--retry-matched",
        action="store_true",
        help="Recheck rows already marked matched; normally they are preserved.",
    )
    parser.add_argument(
        "--not-found-file",
        type=Path,
        default=DEFAULT_NOT_FOUND_FILE,
        help="Local UTF-8 report for ambiguous, missing, and failed searches.",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if args.batch_size != DEFAULT_BATCH_SIZE:
        raise SystemExit("This operation is intentionally fixed to parallel batches of 10.")
    if args.limit is not None and args.limit < 1:
        raise SystemExit("--limit must be a positive integer.")

    load_local_env(PROJECT_ROOT / ".env")
    settings = Settings.from_env()
    settings.validate()
    if not settings.db_name.casefold().endswith("_copy"):
        raise SystemExit(f"Refusing database {settings.db_name!r}; this tool only permits *_copy databases.")

    connection = _connect_until_available(settings)
    try:
        _ensure_table(connection)
        connection.commit()
        series = _load_series(connection, args.limit)
        existing = _load_existing(connection)
        pending = [
            row
            for row in series
            if args.retry_matched or existing.get(row.series_id, {}).get("match_status") != MATCHED
        ]
        occupied = {
            str(row["spark_slug"]): int(series_id)
            for series_id, row in existing.items()
            if row.get("match_status") == MATCHED and row.get("spark_slug")
        }
        args.not_found_file.parent.mkdir(parents=True, exist_ok=True)
        args.not_found_file.write_text(
            "series_id\tstatus\ttitle\treason\n", encoding="utf-8"
        )
        print(
            f"[spark-slug-map] database={settings.db_name} active_series={len(series)} "
            f"pending={len(pending)} batch_size=10"
        )

        adapter = SparkMangaAdapter(
            timeout_seconds=settings.http_timeout_seconds,
            user_agent=settings.http_user_agent,
        )
        batches = (len(pending) + DEFAULT_BATCH_SIZE - 1) // DEFAULT_BATCH_SIZE
        matched_total = ambiguous_total = not_found_total = error_total = 0
        with ThreadPoolExecutor(max_workers=DEFAULT_BATCH_SIZE) as executor:
            for batch_number, start in enumerate(range(0, len(pending), DEFAULT_BATCH_SIZE), start=1):
                batch_rows = pending[start : start + DEFAULT_BATCH_SIZE]
                futures = {executor.submit(_process_one, row, adapter): row for row in batch_rows}
                results: list[MappingResult] = []
                for future in as_completed(futures):
                    row = futures[future]
                    try:
                        results.append(future.result())
                    except Exception as exc:
                        results.append(
                            MappingResult(
                                row.series_id,
                                row.title,
                                ERROR,
                                evidence={"exception": type(exc).__name__},
                                reason=str(exc)[:500],
                            )
                        )
                results.sort(key=lambda result: result.series_id)
                connection, results = _persist_with_reconnect(
                    settings, connection, results, occupied
                )
                _append_unmatched(args.not_found_file, results)
                matched_total += sum(result.status == MATCHED for result in results)
                ambiguous_total += sum(result.status == AMBIGUOUS for result in results)
                not_found_total += sum(result.status == NOT_FOUND for result in results)
                error_total += sum(result.status == ERROR for result in results)
                print(
                    f"[spark-slug-map] batch={batch_number}/{batches} "
                    f"processed={start + len(batch_rows)}/{len(pending)} "
                    f"matched={matched_total} ambiguous={ambiguous_total} "
                    f"not_found={not_found_total} errors={error_total}",
                    flush=True,
                )

        unresolved = _rewrite_unmatched(connection, args.not_found_file)
        print(
            f"[spark-slug-map] finished mapped={len(occupied)} unresolved={unresolved} "
            f"report={args.not_found_file}"
        )
        return 0
    finally:
        try:
            connection.close()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
