from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    return default if value in (None, "") else int(value)


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    return default if value in (None, "") else float(value)


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value in (None, ""):
        return default
    normalized = value.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean value")


def _env_value(name: str, fallback_name: str | None, default: str) -> str:
    value = os.getenv(name)
    if value not in (None, ""):
        return value
    if fallback_name is not None:
        value = os.getenv(fallback_name)
        if value not in (None, ""):
            return value
    return default


def _env_int_from_names(names: tuple[str, ...], default: int) -> int:
    for name in names:
        value = os.getenv(name)
        if value not in (None, ""):
            return int(value)
    return default


def load_local_env(path: Path) -> None:
    """Load KEY=VALUE pairs without overriding already-set environment vars."""
    if not path.is_file():
        raise FileNotFoundError(f"environment file not found: {path}")
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


@dataclass(frozen=True)
class Settings:
    db_host: str
    db_port: int
    db_name: str
    db_user: str
    db_password: str
    http_timeout_seconds: int = 25
    http_user_agent: str = "MangaStar-MultiSource/0.1"
    mangatek_cookie: str = ""
    auto_match_threshold: float = 0.94
    match_margin: float = 0.08
    auto_create_new_series: bool = True
    new_series_score_threshold: float = 0.60
    max_latest_items: int = 120
    latest_known_streak: int = 5
    latest_max_pages_per_source: int = 100
    poll_interval_seconds: int = 300
    enrich_new_work_limit: int = 5
    enrichment_workers: int = 3
    page_workers: int = 3
    chapter_promotion_limit: int = 50
    backfill_enabled: bool = True
    backfill_interval_seconds: int = 86400

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            # Accept the reference worker's DB_* names for shared Railway
            # environment files, while keeping MangaStar-specific overrides
            # higher priority.
            db_host=_env_value("MANGA_DB_HOST", "DB_HOST", "45.86.220.2"),
            db_port=_env_int_from_names(("MANGA_DB_PORT", "DB_PORT"), 3306),
            db_name=os.getenv("MANGA_DB_NAME", "zhffrycs_star_copy"),
            db_user=os.getenv("MANGA_DB_USER", "zhffrycs_codex"),
            # Never inherit DB_PASSWORD from the reference project's file:
            # it belongs to that project's DB_USER.  The Codex credential must
            # be supplied explicitly through the MangaStar-specific variable.
            db_password=os.getenv("MANGA_DB_PASSWORD", ""),
            http_timeout_seconds=_env_int("MANGA_HTTP_TIMEOUT_SECONDS", 25),
            http_user_agent=os.getenv("MANGA_HTTP_USER_AGENT", "MangaStar-MultiSource/0.1"),
            mangatek_cookie=os.getenv("MANGA_MANGATEK_COOKIE", "").strip(),
            auto_match_threshold=_env_float("MANGA_AUTO_MATCH_THRESHOLD", 0.94),
            match_margin=_env_float("MANGA_MATCH_MARGIN", 0.08),
            auto_create_new_series=_env_bool("MANGA_AUTO_CREATE_NEW_SERIES", True),
            new_series_score_threshold=_env_float("MANGA_NEW_SERIES_SCORE_THRESHOLD", 0.60),
            max_latest_items=_env_int("MANGA_MAX_LATEST_ITEMS", 120),
            latest_known_streak=_env_int("MANGA_LATEST_KNOWN_STREAK", 5),
            latest_max_pages_per_source=_env_int("MANGA_LATEST_MAX_PAGES_PER_SOURCE", 100),
            poll_interval_seconds=_env_int("MANGA_POLL_INTERVAL_SECONDS", 300),
            enrich_new_work_limit=_env_int("MANGA_ENRICH_NEW_WORK_LIMIT", 5),
            enrichment_workers=_env_int("MANGA_ENRICHMENT_WORKERS", 3),
            page_workers=_env_int("MANGA_PAGE_WORKERS", 3),
            chapter_promotion_limit=_env_int("MANGA_CHAPTER_PROMOTION_LIMIT", 50),
            backfill_enabled=_env_bool("MANGA_BACKFILL_ENABLED", True),
            backfill_interval_seconds=_env_int("MANGA_BACKFILL_INTERVAL_SECONDS", 86400),
        )

    def validate(self, *, require_password: bool = True) -> None:
        if require_password and not self.db_password:
            raise ValueError("MANGA_DB_PASSWORD is required; keep credentials outside the repository.")
        if not self.db_name.endswith("_copy") and os.getenv("MANGA_ALLOW_NON_COPY_DB") != "1":
            raise ValueError(
                f"Refusing database {self.db_name!r}. This build is locked to *_copy databases; "
                "set MANGA_ALLOW_NON_COPY_DB=1 only for an explicitly approved deployment."
            )
        if not 0 < self.auto_match_threshold <= 1:
            raise ValueError("MANGA_AUTO_MATCH_THRESHOLD must be between 0 and 1.")
        if not 0 < self.new_series_score_threshold < self.auto_match_threshold:
            raise ValueError(
                "MANGA_NEW_SERIES_SCORE_THRESHOLD must be greater than 0 and below "
                "MANGA_AUTO_MATCH_THRESHOLD."
            )
        if self.match_margin < 0:
            raise ValueError("MANGA_MATCH_MARGIN cannot be negative.")
        if self.poll_interval_seconds < 1:
            raise ValueError("MANGA_POLL_INTERVAL_SECONDS must be at least 1.")
        if self.enrich_new_work_limit < 0:
            raise ValueError("MANGA_ENRICH_NEW_WORK_LIMIT cannot be negative.")
        if self.enrichment_workers < 1:
            raise ValueError("MANGA_ENRICHMENT_WORKERS must be at least 1.")
        if self.page_workers < 1:
            raise ValueError("MANGA_PAGE_WORKERS must be at least 1.")
        if self.chapter_promotion_limit < 0:
            raise ValueError("MANGA_CHAPTER_PROMOTION_LIMIT cannot be negative.")
        if self.latest_known_streak < 1:
            raise ValueError("MANGA_LATEST_KNOWN_STREAK must be at least 1.")
        if self.latest_max_pages_per_source < 0:
            raise ValueError("MANGA_LATEST_MAX_PAGES_PER_SOURCE cannot be negative.")
        if self.backfill_interval_seconds < 0:
            raise ValueError("MANGA_BACKFILL_INTERVAL_SECONDS cannot be negative.")
