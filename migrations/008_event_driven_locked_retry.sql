-- Locked chapters are retried when a later chapter/fallback observation is
-- inserted, not on every polling cycle. This index keeps that event check
-- bounded on the populated source-chapter table.
ALTER TABLE ms_source_chapters
    ADD INDEX IF NOT EXISTS idx_ms_source_chapters_work_first_seen
        (source_work_id, first_seen_at, id);
