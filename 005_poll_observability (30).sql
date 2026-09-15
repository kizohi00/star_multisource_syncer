ALTER TABLE ms_poll_runs
    ADD COLUMN pages_scanned int NOT NULL DEFAULT 0 AFTER new_count,
    ADD COLUMN known_streak int NOT NULL DEFAULT 0 AFTER pages_scanned,
    ADD COLUMN stop_reason varchar(32) NULL AFTER known_streak;
