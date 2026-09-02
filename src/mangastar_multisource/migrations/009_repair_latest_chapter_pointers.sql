-- Keep the pre-repair pointer table in the test database for audit/recovery.
CREATE TABLE IF NOT EXISTS ms_repair_backup_series_latest_chapters_20260902
    LIKE series_latest_chapters;

INSERT INTO ms_repair_backup_series_latest_chapters_20260902
    (series_id, latest_chapter_id, updated_at)
SELECT current_pointer.series_id,
       current_pointer.latest_chapter_id,
       current_pointer.updated_at
FROM series_latest_chapters AS current_pointer
WHERE NOT EXISTS (
    SELECT 1
    FROM ms_repair_backup_series_latest_chapters_20260902 AS saved_pointer
    WHERE saved_pointer.series_id=current_pointer.series_id
      AND saved_pointer.latest_chapter_id=current_pointer.latest_chapter_id
);

-- Remove missing, deleted, or cross-series pointers before rebuilding them.
DELETE current_pointer
FROM series_latest_chapters AS current_pointer
LEFT JOIN series AS s ON s.id=current_pointer.series_id
LEFT JOIN chapters AS c ON c.id=current_pointer.latest_chapter_id
WHERE s.id IS NULL
   OR s.deleted_at IS NOT NULL
   OR c.id IS NULL
   OR c.deleted_at IS NOT NULL
   OR c.series_id<>current_pointer.series_id;

-- Rebuild exactly one pointer per active series from a chapter belonging to
-- that same series. The chapter number is authoritative, and created_at and id
-- only break ties between duplicate chapter numbers.
INSERT INTO series_latest_chapters (series_id, latest_chapter_id)
SELECT ranked.series_id, ranked.id
FROM (
    SELECT c.series_id,
           c.id,
           ROW_NUMBER() OVER (
               PARTITION BY c.series_id
               ORDER BY c.chapter_number DESC,
                        COALESCE(c.created_at, '1000-01-01 00:00:00') DESC,
                        c.id DESC
           ) AS row_position
    FROM chapters AS c
    INNER JOIN series AS s ON s.id=c.series_id
    WHERE c.deleted_at IS NULL
      AND s.deleted_at IS NULL
) AS ranked
WHERE ranked.row_position=1
ON DUPLICATE KEY UPDATE
    latest_chapter_id=VALUES(latest_chapter_id),
    updated_at=CURRENT_TIMESTAMP;

-- Keep the denormalized series counter consistent with visible chapters.
UPDATE series AS s
LEFT JOIN (
    SELECT series_id, COUNT(*) AS chapter_count
    FROM chapters
    WHERE deleted_at IS NULL
    GROUP BY series_id
) AS counts ON counts.series_id=s.id
SET s.total_chapters=COALESCE(counts.chapter_count, 0)
WHERE s.total_chapters<>COALESCE(counts.chapter_count, 0)
   OR s.total_chapters IS NULL;
