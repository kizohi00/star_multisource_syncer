-- Recover exact-title source works that were left as candidates by a
-- temporary/incomplete matcher scan. Only unmapped source works are changed;
-- existing mappings are never rewritten by this backfill.
UPDATE ms_source_works AS sw
INNER JOIN (
    SELECT sw2.id AS source_work_id, MIN(s2.id) AS canonical_series_id
    FROM ms_source_works AS sw2
    INNER JOIN series AS s2
        ON s2.deleted_at IS NULL
       AND s2.title IS NOT NULL
       AND LOWER(TRIM(s2.title))=LOWER(TRIM(sw2.title_raw))
    WHERE sw2.canonical_series_id IS NULL
    GROUP BY sw2.id
) AS exact_match ON exact_match.source_work_id=sw.id
SET sw.canonical_series_id=exact_match.canonical_series_id,
    sw.match_status='matched',
    sw.match_score=1.000000,
    sw.updated_at=CURRENT_TIMESTAMP
WHERE sw.canonical_series_id IS NULL;
