ALTER TABLE trip_understanding_jobs
    ADD COLUMN IF NOT EXISTS progress_sequence BIGINT NOT NULL DEFAULT 0
        CHECK (progress_sequence >= 0);
