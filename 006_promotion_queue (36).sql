CREATE TABLE IF NOT EXISTS ms_promotion_queue (
    id bigint unsigned NOT NULL AUTO_INCREMENT,
    action enum('create_series','create_chapter') NOT NULL,
    target_key varchar(128) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    source_work_id bigint unsigned NOT NULL,
    source_chapter_id bigint unsigned NULL,
    canonical_series_id bigint NULL,
    proposed_title varchar(500) NOT NULL,
    proposed_chapter_number decimal(8,2) NULL,
    proposed_chapter_title varchar(500) NULL,
    evidence_json longtext NOT NULL CHECK (json_valid(evidence_json)),
    status enum('pending','approved','rejected','applied') NOT NULL DEFAULT 'pending',
    review_note varchar(500) NULL,
    applied_series_id bigint NULL,
    applied_chapter_id bigint NULL,
    created_at datetime NOT NULL DEFAULT current_timestamp(),
    updated_at datetime NOT NULL DEFAULT current_timestamp() ON UPDATE current_timestamp(),
    PRIMARY KEY (id),
    UNIQUE KEY uq_ms_promotion_target (target_key),
    KEY idx_ms_promotion_review (status, action, created_at),
    KEY idx_ms_promotion_source_work (source_work_id),
    KEY idx_ms_promotion_source_chapter (source_chapter_id),
    CONSTRAINT fk_ms_promotion_work
        FOREIGN KEY (source_work_id) REFERENCES ms_source_works (id)
        ON UPDATE CASCADE ON DELETE CASCADE,
    CONSTRAINT fk_ms_promotion_chapter
        FOREIGN KEY (source_chapter_id) REFERENCES ms_source_chapters (id)
        ON UPDATE CASCADE ON DELETE CASCADE,
    CONSTRAINT fk_ms_promotion_series
        FOREIGN KEY (canonical_series_id) REFERENCES series (id)
        ON UPDATE CASCADE ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
