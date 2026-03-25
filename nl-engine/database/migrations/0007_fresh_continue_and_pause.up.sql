ALTER TABLE problems ADD COLUMN continuation_generation INTEGER NOT NULL DEFAULT 0;

ALTER TABLE problem_executions ADD COLUMN continuation_generation INTEGER NOT NULL DEFAULT 0;

ALTER TABLE worker_jobs ADD COLUMN continuation_generation INTEGER NOT NULL DEFAULT 0;
ALTER TABLE worker_jobs ADD COLUMN attempt_number INTEGER;
ALTER TABLE worker_jobs ADD COLUMN superseded_at TIMESTAMPTZ;
ALTER TABLE worker_jobs ADD COLUMN superseded_by_execution_id TEXT;
ALTER TABLE worker_jobs ADD COLUMN supersede_reason TEXT;

CREATE INDEX idx_worker_jobs_problem_generation_status
    ON worker_jobs(problem_id, continuation_generation, status);
