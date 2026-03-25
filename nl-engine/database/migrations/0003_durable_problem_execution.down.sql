DROP INDEX IF EXISTS idx_request_records_problem_source;
DROP INDEX IF EXISTS idx_request_records_problem_time;
DROP TABLE IF EXISTS request_records;

DROP INDEX IF EXISTS idx_problem_executions_status;
DROP INDEX IF EXISTS idx_problem_executions_problem;
DROP TABLE IF EXISTS problem_executions;

DROP INDEX IF EXISTS idx_worker_jobs_execution;
DROP INDEX IF EXISTS idx_worker_jobs_problem_kind_status;

ALTER TABLE worker_jobs DROP COLUMN IF EXISTS request_source;
ALTER TABLE worker_jobs DROP COLUMN IF EXISTS handler_key;
ALTER TABLE worker_jobs DROP COLUMN IF EXISTS artifact_prefix;
ALTER TABLE worker_jobs DROP COLUMN IF EXISTS execution_id;
ALTER TABLE worker_jobs DROP COLUMN IF EXISTS target_kind;
ALTER TABLE worker_jobs DROP COLUMN IF EXISTS target_id;
