-- Keep historical Starz/Lek rows for traceability, but stop treating their
-- alternate domains as active polling sources. SparkManga is the stable
-- operational domain for the shared catalog.
UPDATE ms_sources
SET enabled = 0,
    updated_at = CURRENT_TIMESTAMP
WHERE source_key IN ('starz', 'lekmanga')
  AND enabled <> 0;
