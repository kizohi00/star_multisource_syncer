from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .adapters.registry import build_adapters, select_adapters
from .application.enrichment import WorkEnrichmentService
from .application.pages import fetch_and_store_pages_with_fallback
from .application.promotion import PromotionService
from .application.sync import LatestFeedService
from .application.worker import MultiSourceWorker
from .config import Settings, load_local_env
from .infrastructure.db import MySqlDatabase
from .infrastructure.repositories import MySqlSourceRepository
from .matching.metadata import MetadataMatcher


MIGRATIONS = Path(__file__).resolve().parent / "migrations"


def configure_utf8_stdio() -> None:
    """Keep JSON output usable on Windows terminals with a legacy code page."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="strict")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ms-multisource", description="Manga Star multi-source ingestion pipeline")
    parser.add_argument(
        "--env-file",
        type=Path,
        help="Load DB_* or MANGA_* variables from an external environment file",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("migrate", help="Apply idempotent source-aware schema migrations")
    subparsers.add_parser("inspect", help="Show connection and pipeline table counts")
    monitor = subparsers.add_parser("monitor", help="Show source health and the last polling telemetry")
    monitor.add_argument("--source", action="append", dest="sources", help="Source key; repeat to select multiple")
    promote = subparsers.add_parser("promote", help="Preview or queue safe canonical-content promotion proposals")
    promote.add_argument("--source", action="append", dest="sources", help="Source key; repeat to select multiple")
    promote.add_argument("--limit", type=int, default=100, help="Maximum promotion proposals")
    promote.add_argument("--enqueue", action="store_true", help="Persist proposals in ms_promotion_queue")
    queue = subparsers.add_parser("promotion-queue", help="Show promotion proposals awaiting review")
    queue.add_argument("--source", action="append", dest="sources", help="Source key; repeat to select multiple")
    queue.add_argument("--status", choices=("pending", "approved", "rejected", "applied"), default="pending")
    queue.add_argument("--limit", type=int, default=100, help="Maximum queue rows")
    review = subparsers.add_parser("promotion-review", help="Approve or reject promotion proposals by queue id")
    review.add_argument("--id", action="append", dest="queue_ids", type=int, required=True)
    review.add_argument("--status", choices=("approved", "rejected"), required=True)
    review.add_argument("--note", default="", help="Optional review note")
    apply_approved = subparsers.add_parser(
        "apply-approved",
        help="Apply approved proposals after current source/page validation",
    )
    apply_approved.add_argument("--source", action="append", dest="sources", help="Source key; repeat to select multiple")
    apply_approved.add_argument("--limit", type=int, default=20, help="Maximum approved proposals to apply")
    poll = subparsers.add_parser("poll", help="Poll latest feeds in parallel and persist observations")
    poll.add_argument("--source", action="append", dest="sources", help="Source key; repeat to select multiple")
    poll.add_argument("--limit", type=int, default=None, help="Maximum works per source")
    poll.add_argument("--known-streak", type=int, default=None, help="Known consecutive works required to stop paging")
    poll.add_argument("--max-pages", type=int, default=None, help="Safety cap per source; 0 means unlimited")
    worker = subparsers.add_parser("worker", help="Run repeated polling and enrich only pending new works")
    worker.add_argument("--source", action="append", dest="sources", help="Source key; repeat to select multiple")
    worker.add_argument("--limit", type=int, default=None, help="Maximum latest works per source")
    worker.add_argument("--enrich-limit", type=int, default=None, help="Pending works to enrich per source per cycle")
    worker.add_argument(
        "--promote-limit",
        type=int,
        default=None,
        help="Maximum pending source chapters to publish per cycle",
    )
    worker.add_argument("--interval", type=int, default=None, help="Seconds between cycles")
    worker.add_argument("--once", action="store_true", help="Run one cycle and exit")
    worker.add_argument("--known-streak", type=int, default=None, help="Known consecutive works required to stop paging")
    worker.add_argument("--max-pages", type=int, default=None, help="Safety cap per source; 0 means unlimited")
    worker.add_argument(
        "--allow-source-failures",
        action="store_true",
        help="Treat an individual source failure as degraded while keeping the cycle successful",
    )
    enrich = subparsers.add_parser("enrich", help="Fetch details and complete chapter lists for observed works")
    enrich.add_argument("--source", action="append", dest="sources", required=True, help="Source key; repeat to select multiple")
    enrich.add_argument("--limit", type=int, default=3, help="Maximum observed works per source")
    pages = subparsers.add_parser("fetch-pages", help="Fetch page metadata for one source chapter on demand")
    pages.add_argument("--source", required=True, help="Source key")
    pages.add_argument("--chapter-id", required=True, type=int, help="ms_source_chapters.id")
    return parser


def main(argv: list[str] | None = None) -> None:
    configure_utf8_stdio()
    args = build_parser().parse_args(argv)
    if args.env_file is not None:
        load_local_env(args.env_file)
    settings = Settings.from_env()
    settings.validate()
    database = MySqlDatabase(settings)
    repository = MySqlSourceRepository(database)

    if args.command == "migrate":
        applied = database.apply_migrations(MIGRATIONS)
        print(json.dumps({"database": settings.db_name, "applied": applied}, ensure_ascii=False))
        return

    if args.command == "inspect":
        with database.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT DATABASE() AS database_name, VERSION() AS version")
                connection_info = cursor.fetchone()
        print(json.dumps({"connection": connection_info, "pipeline": repository.counts()}, ensure_ascii=False, default=str))
        return

    if args.command == "monitor":
        print(
            json.dumps(
                {
                    "database": settings.db_name,
                    "sources": repository.source_health(set(args.sources or [])),
                },
                ensure_ascii=False,
                default=str,
            )
        )
        return

    if args.command in {"promote", "promotion-queue"}:
        adapters = select_adapters(build_adapters(settings), set(args.sources or []))
        if args.command == "promote":
            result = PromotionService(repository).preview(
                adapters,
                limit=args.limit,
                enqueue=args.enqueue,
            )
        else:
            result = {
                "database": settings.db_name,
                "status": args.status,
                "proposals": repository.list_promotion_queue(
                    status=args.status,
                    source_keys=[adapter.key for adapter in adapters],
                    limit=args.limit,
                ),
            }
        print(json.dumps(result, ensure_ascii=False, default=str))
        return

    if args.command == "promotion-review":
        result = PromotionService(repository).review(
            args.queue_ids,
            status=args.status,
            note=args.note,
        )
        print(json.dumps(result, ensure_ascii=False, default=str))
        return

    if args.command == "apply-approved":
        result = PromotionService(repository).apply_approved(
            source_keys=args.sources,
            limit=args.limit,
        )
        print(json.dumps(result, ensure_ascii=False, default=str))
        if result["counts"].get("error"):
            raise SystemExit(2)
        return

    if args.command == "fetch-pages":
        adapters = build_adapters(settings)
        select_adapters(adapters, {args.source})
        print(
            json.dumps(
                fetch_and_store_pages_with_fallback(repository, adapters, args.chapter_id),
                ensure_ascii=False,
            )
        )
        return

    all_adapters = build_adapters(settings)
    adapters = select_adapters(all_adapters, set(args.sources or []))
    if args.command == "worker":
        poll_service = LatestFeedService(
            repository,
            MetadataMatcher(auto_threshold=settings.auto_match_threshold, margin=settings.match_margin),
            known_work_streak=(
                settings.latest_known_streak if args.known_streak is None else args.known_streak
            ),
            max_pages_per_source=(
                settings.latest_max_pages_per_source if args.max_pages is None else args.max_pages
            ),
            backfill_enabled=settings.backfill_enabled,
            backfill_interval_seconds=settings.backfill_interval_seconds,
        )
        enrichment_service = WorkEnrichmentService(
            repository,
            MetadataMatcher(auto_threshold=settings.auto_match_threshold, margin=settings.match_margin),
            auto_create_low_confidence=settings.auto_create_new_series,
            new_series_score_threshold=settings.new_series_score_threshold,
            max_workers=settings.enrichment_workers,
            page_workers=settings.page_workers,
        )
        worker = MultiSourceWorker(
            poll_service,
            enrichment_service,
            adapters,
            poll_limit=args.limit or settings.max_latest_items,
            enrich_limit=(
                settings.enrich_new_work_limit
                if args.enrich_limit is None
                else args.enrich_limit
            ),
            interval_seconds=args.interval or settings.poll_interval_seconds,
            fallback_adapters=all_adapters,
            chapter_promotion_limit=(
                settings.chapter_promotion_limit
                if args.promote_limit is None
                else args.promote_limit
            ),
        )
        if args.once:
            result = worker.run_once()
            print(json.dumps(result.as_dict(), ensure_ascii=False))
            if result.error or (
                not args.allow_source_failures
                and any(item.status == "failed" for item in result.poll_results)
            ):
                raise SystemExit(2)
            return
        print(
            json.dumps(
                {
                    "status": "started",
                    "sources": [adapter.key for adapter in adapters],
                    "interval_seconds": worker.interval_seconds,
                    "poll_limit": worker.poll_limit,
                    "enrich_limit": worker.enrich_limit,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        worker.run_forever(
            on_cycle=lambda result: print(json.dumps(result.as_dict(), ensure_ascii=False), flush=True)
        )
        return
    if args.command == "enrich":
        service = WorkEnrichmentService(
            repository,
            MetadataMatcher(auto_threshold=settings.auto_match_threshold, margin=settings.match_margin),
            auto_create_low_confidence=settings.auto_create_new_series,
            new_series_score_threshold=settings.new_series_score_threshold,
            max_workers=settings.enrichment_workers,
            page_workers=settings.page_workers,
        )
        print(
            json.dumps(
                [
                    result.__dict__
                    for result in service.enrich(
                        adapters,
                        limit=args.limit,
                        fallback_adapters=all_adapters,
                    )
                ],
                ensure_ascii=False,
            )
        )
        return
    service = LatestFeedService(
        repository,
        MetadataMatcher(auto_threshold=settings.auto_match_threshold, margin=settings.match_margin),
        known_work_streak=(
            settings.latest_known_streak if args.known_streak is None else args.known_streak
        ),
        max_pages_per_source=(
            settings.latest_max_pages_per_source if args.max_pages is None else args.max_pages
        ),
        backfill_enabled=settings.backfill_enabled,
        backfill_interval_seconds=settings.backfill_interval_seconds,
    )
    results = service.poll(adapters, limit=args.limit or settings.max_latest_items)
    print(json.dumps([result.__dict__ for result in results], ensure_ascii=False))
    if any(result.status == "failed" for result in results):
        raise SystemExit(2)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
