ALTER TABLE trip_understanding_jobs
    DROP CONSTRAINT IF EXISTS trip_understanding_jobs_supplement_round_check;
ALTER TABLE trip_understanding_jobs
    ADD CONSTRAINT trip_understanding_jobs_supplement_round_check CHECK (
        supplement_round BETWEEN 0 AND 2 OR
        (supplement_round = 3 AND job_type = 'SUPPLEMENT' AND
         COALESCE(supplement_outcome_json->>'execution_reason' = 'RETIRED_KIMI_TO_QWEN', FALSE))
    );
ALTER TABLE trip_understanding_jobs DROP CONSTRAINT IF EXISTS supplement_job_base;
ALTER TABLE trip_understanding_jobs ADD CONSTRAINT supplement_job_base CHECK (
    (job_type = 'UNDERSTAND' AND supplement_round = 0) OR
    (job_type = 'SUPPLEMENT' AND supplement_round IN (1,2,3) AND base_public_resource_id IS NOT NULL
     AND base_etag IS NOT NULL AND ((base_owner_user_id IS NULL) <> (base_anonymous_session_id IS NULL)))
);
