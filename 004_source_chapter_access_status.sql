ALTER TABLE ms_source_chapters
    ADD COLUMN IF NOT EXISTS access_status varchar(20) NOT NULL DEFAULT 'unknown' AFTER match_status,
    ADD COLUMN IF NOT EXISTS access_error varchar(500) NULL AFTER access_status,
    ADD COLUMN IF NOT EXISTS access_checked_at datetime NULL AFTER access_error;
