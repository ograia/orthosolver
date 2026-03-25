ALTER TABLE worker_jobs ADD COLUMN controller_consumed_at TIMESTAMPTZ;
ALTER TABLE worker_jobs ADD COLUMN controller_consumed_by_execution_id TEXT;

CREATE INDEX idx_worker_jobs_controller_consumed
  ON worker_jobs(problem_id, worker_kind, status, controller_consumed_at);
