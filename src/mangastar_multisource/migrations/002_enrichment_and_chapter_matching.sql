ALTER TABLE ms_source_works
    ADD COLUMN IF NOT EXISTS detail_fetched_at datetime NULL;

ALTER TABLE ms_source_chapters
    ADD COLUMN IF NOT EXISTS match_score decimal(7,6) NULL,
    ADD COLUMN IF NOT EXISTS pages_fetched_at datetime NULL;

CREATE TABLE IF NOT EXISTS ms_chapter_match_candidates (
    id bigint unsigned NOT NULL AUTO_INCREMENT,
    source_chapter_id bigint unsigned NOT NULL,
    canonical_chapter_id bigint NOT NULL,
    rank_position smallint unsigned NOT NULL,
    score decimal(7,6) NOT NULL,
    evidence_json longtext NOT NULL CHECK (json_valid(evidence_json)),
    decision_status varchar(20) NOT NULL DEFAULT 'pending',
    created_at datetime NOT NULL DEFAULT current_timestamp(),
    updated_at datetime NOT NULL DEFAULT current_timestamp() ON UPDATE current_timestamp(),
    PRIMARY KEY (id),
    UNIQUE KEY uq_ms_chapter_match_candidate (source_chapter_id, canonical_chapter_id),
    KEY idx_ms_chapter_candidates_review (decision_status, score),
    CONSTRAINT fk_ms_chapter_candidates_source
        FOREIGN KEY (source_chapter_id) REFERENCES ms_source_chapters (id)
        ON UPDATE CASCADE ON DELETE CASCADE,
    CONSTRAINT fk_ms_chapter_candidates_canonical
        FOREIGN KEY (canonical_chapter_id) REFERENCES chapters (id)
        ON UPDATE CASCADE ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
