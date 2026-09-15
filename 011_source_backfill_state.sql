-- Persist a separate historical-feed cursor so old source pages are not
-- skipped when the normal latest-feed frontier is already known.
CREATE TABLE IF NOT EXISTS ms_source_backfill_state (
    source_key varchar(32) NOT NULL,
    next_page int unsigned NOT NULL DEFAULT 2,
    in_flight_page int unsigned NULL,
    lease_until datetime NULL,
    last_scanned_page int unsigned NULL,
    last_scanned_at datetime NULL,
    cycle_completed_at datetime NULL,
    last_error varchar(2000) NULL,
    updated_at datetime NOT NULL DEFAULT current_timestamp() ON UPDATE current_timestamp(),
    PRIMARY KEY (source_key),
    CONSTRAINT fk_ms_source_backfill_state_source
        FOREIGN KEY (source_key) REFERENCES ms_sources (source_key)
        ON UPDATE CASCADE ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
