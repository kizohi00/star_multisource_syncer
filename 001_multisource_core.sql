CREATE TABLE IF NOT EXISTS ms_sources (
    source_key varchar(32) NOT NULL,
    display_name varchar(120) NOT NULL,
    base_url varchar(255) NOT NULL,
    enabled tinyint(1) NOT NULL DEFAULT 1,
    priority int NOT NULL DEFAULT 100,
    last_success_at datetime NULL,
    last_error_at datetime NULL,
    last_error varchar(2000) NULL,
    last_feed_count int NOT NULL DEFAULT 0,
    last_new_count int NOT NULL DEFAULT 0,
    created_at datetime NOT NULL DEFAULT current_timestamp(),
    updated_at datetime NOT NULL DEFAULT current_timestamp() ON UPDATE current_timestamp(),
    PRIMARY KEY (source_key),
    KEY idx_ms_sources_enabled_priority (enabled, priority)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS ms_source_works (
    id bigint unsigned NOT NULL AUTO_INCREMENT,
    source_key varchar(32) NOT NULL,
    source_work_key varchar(512) NOT NULL,
    source_work_hash char(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    source_url varchar(1024) NOT NULL,
    title_raw varchar(500) NOT NULL,
    alt_titles_json longtext NULL CHECK (alt_titles_json IS NULL OR json_valid(alt_titles_json)),
    summary_raw longtext NULL,
    cover_url varchar(1024) NULL,
    tags_json longtext NULL CHECK (tags_json IS NULL OR json_valid(tags_json)),
    payload_json longtext NULL CHECK (payload_json IS NULL OR json_valid(payload_json)),
    match_status varchar(20) NOT NULL DEFAULT 'unmatched',
    match_score decimal(7,6) NULL,
    canonical_series_id bigint NULL,
    first_seen_at datetime NOT NULL DEFAULT current_timestamp(),
    last_seen_at datetime NOT NULL DEFAULT current_timestamp(),
    updated_at datetime NOT NULL DEFAULT current_timestamp() ON UPDATE current_timestamp(),
    PRIMARY KEY (id),
    UNIQUE KEY uq_ms_source_work_identity (source_key, source_work_hash),
    KEY idx_ms_source_works_mapping (canonical_series_id),
    KEY idx_ms_source_works_status (match_status, last_seen_at),
    CONSTRAINT fk_ms_source_works_source
        FOREIGN KEY (source_key) REFERENCES ms_sources (source_key)
        ON UPDATE CASCADE ON DELETE RESTRICT,
    CONSTRAINT fk_ms_source_works_series
        FOREIGN KEY (canonical_series_id) REFERENCES series (id)
        ON UPDATE CASCADE ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS ms_source_chapters (
    id bigint unsigned NOT NULL AUTO_INCREMENT,
    source_work_id bigint unsigned NOT NULL,
    source_chapter_key varchar(512) NOT NULL,
    source_chapter_hash char(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    source_url varchar(1024) NOT NULL,
    label_raw varchar(255) NOT NULL,
    chapter_number decimal(8,2) NULL,
    title_raw varchar(500) NULL,
    published_at datetime NULL,
    canonical_chapter_id bigint NULL,
    match_status varchar(20) NOT NULL DEFAULT 'unmatched',
    first_seen_at datetime NOT NULL DEFAULT current_timestamp(),
    last_seen_at datetime NOT NULL DEFAULT current_timestamp(),
    updated_at datetime NOT NULL DEFAULT current_timestamp() ON UPDATE current_timestamp(),
    PRIMARY KEY (id),
    UNIQUE KEY uq_ms_source_chapter_identity (source_work_id, source_chapter_hash),
    KEY idx_ms_source_chapters_number (source_work_id, chapter_number),
    KEY idx_ms_source_chapters_mapping (canonical_chapter_id),
    CONSTRAINT fk_ms_source_chapters_work
        FOREIGN KEY (source_work_id) REFERENCES ms_source_works (id)
        ON UPDATE CASCADE ON DELETE RESTRICT,
    CONSTRAINT fk_ms_source_chapters_chapter
        FOREIGN KEY (canonical_chapter_id) REFERENCES chapters (id)
        ON UPDATE CASCADE ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS ms_source_pages (
    id bigint unsigned NOT NULL AUTO_INCREMENT,
    source_chapter_id bigint unsigned NOT NULL,
    page_order int NOT NULL,
    source_page_key varchar(1024) NOT NULL,
    image_url varchar(2048) NOT NULL,
    width int NULL,
    height int NULL,
    perceptual_hash char(16) CHARACTER SET ascii COLLATE ascii_bin NULL,
    sampled_at datetime NULL,
    canonical_page_id bigint NULL,
    created_at datetime NOT NULL DEFAULT current_timestamp(),
    updated_at datetime NOT NULL DEFAULT current_timestamp() ON UPDATE current_timestamp(),
    PRIMARY KEY (id),
    UNIQUE KEY uq_ms_source_page_order (source_chapter_id, page_order),
    KEY idx_ms_source_pages_hash (perceptual_hash),
    KEY idx_ms_source_pages_canonical (canonical_page_id),
    CONSTRAINT fk_ms_source_pages_chapter
        FOREIGN KEY (source_chapter_id) REFERENCES ms_source_chapters (id)
        ON UPDATE CASCADE ON DELETE RESTRICT,
    CONSTRAINT fk_ms_source_pages_page
        FOREIGN KEY (canonical_page_id) REFERENCES pages (id)
        ON UPDATE CASCADE ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS ms_match_candidates (
    id bigint unsigned NOT NULL AUTO_INCREMENT,
    source_work_id bigint unsigned NOT NULL,
    canonical_series_id bigint NOT NULL,
    rank_position smallint unsigned NOT NULL,
    score decimal(7,6) NOT NULL,
    evidence_json longtext NOT NULL CHECK (json_valid(evidence_json)),
    decision_status varchar(20) NOT NULL DEFAULT 'pending',
    created_at datetime NOT NULL DEFAULT current_timestamp(),
    updated_at datetime NOT NULL DEFAULT current_timestamp() ON UPDATE current_timestamp(),
    PRIMARY KEY (id),
    UNIQUE KEY uq_ms_match_candidate (source_work_id, canonical_series_id),
    KEY idx_ms_match_candidates_review (decision_status, score),
    CONSTRAINT fk_ms_match_candidates_work
        FOREIGN KEY (source_work_id) REFERENCES ms_source_works (id)
        ON UPDATE CASCADE ON DELETE CASCADE,
    CONSTRAINT fk_ms_match_candidates_series
        FOREIGN KEY (canonical_series_id) REFERENCES series (id)
        ON UPDATE CASCADE ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS ms_poll_runs (
    id bigint unsigned NOT NULL AUTO_INCREMENT,
    source_key varchar(32) NOT NULL,
    status varchar(20) NOT NULL,
    fetched_count int NOT NULL DEFAULT 0,
    new_count int NOT NULL DEFAULT 0,
    error_message varchar(2000) NULL,
    started_at datetime NOT NULL DEFAULT current_timestamp(),
    finished_at datetime NULL,
    PRIMARY KEY (id),
    KEY idx_ms_poll_runs_source_time (source_key, started_at),
    CONSTRAINT fk_ms_poll_runs_source
        FOREIGN KEY (source_key) REFERENCES ms_sources (source_key)
        ON UPDATE CASCADE ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
