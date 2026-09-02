CREATE TABLE IF NOT EXISTS ms_canonical_id_sequences (
    entity varchar(32) NOT NULL,
    next_id bigint NOT NULL,
    updated_at datetime NOT NULL DEFAULT current_timestamp() ON UPDATE current_timestamp(),
    PRIMARY KEY (entity)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

INSERT INTO ms_canonical_id_sequences (entity, next_id)
SELECT 'series', COALESCE(MAX(id), 0) + 1
FROM series
ON DUPLICATE KEY UPDATE
    next_id = GREATEST(next_id, VALUES(next_id)),
    updated_at = CURRENT_TIMESTAMP;

ALTER TABLE ms_promotion_queue
    ADD COLUMN IF NOT EXISTS reviewed_at datetime NULL AFTER review_note,
    ADD COLUMN IF NOT EXISTS applied_at datetime NULL AFTER applied_chapter_id;
