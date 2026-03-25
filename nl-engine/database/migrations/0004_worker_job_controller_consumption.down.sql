DROP INDEX IF EXISTS idx_worker_jobs_controller_consumed;

ALTER TABLE worker_jobs DROP COLUMN IF EXISTS controller_consumed_by_execution_id;
ALTER TABLE worker_jobs DROP COLUMN IF EXISTS controller_consumed_at;
