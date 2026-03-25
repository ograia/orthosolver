DROP INDEX IF EXISTS idx_worker_jobs_problem_generation_status;

ALTER TABLE worker_jobs DROP COLUMN IF EXISTS supersede_reason;
ALTER TABLE worker_jobs DROP COLUMN IF EXISTS superseded_by_execution_id;
ALTER TABLE worker_jobs DROP COLUMN IF EXISTS superseded_at;
ALTER TABLE worker_jobs DROP COLUMN IF EXISTS attempt_number;
ALTER TABLE worker_jobs DROP COLUMN IF EXISTS continuation_generation;

ALTER TABLE problem_executions DROP COLUMN IF EXISTS continuation_generation;

ALTER TABLE problems DROP COLUMN IF EXISTS continuation_generation;
