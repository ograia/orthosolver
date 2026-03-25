ALTER TABLE worker_jobs ADD COLUMN target_id TEXT;
ALTER TABLE worker_jobs ADD COLUMN target_kind TEXT;
ALTER TABLE worker_jobs ADD COLUMN execution_id TEXT;
ALTER TABLE worker_jobs ADD COLUMN artifact_prefix TEXT;
ALTER TABLE worker_jobs ADD COLUMN handler_key TEXT;
ALTER TABLE worker_jobs ADD COLUMN request_source TEXT;

CREATE INDEX idx_worker_jobs_problem_kind_status ON worker_jobs(problem_id, worker_kind, status);
CREATE INDEX idx_worker_jobs_execution ON worker_jobs(execution_id);

CREATE TABLE problem_executions (
  execution_id TEXT PRIMARY KEY,
  problem_id TEXT NOT NULL REFERENCES problems(problem_id) ON DELETE CASCADE,
  status TEXT NOT NULL DEFAULT 'queued',
  desired_state TEXT NOT NULL DEFAULT 'running',
  current_stage TEXT,
  blocking_kind TEXT NOT NULL DEFAULT 'none',
  blocking_ref_id TEXT,
  lease_owner TEXT,
  lease_expires_at TIMESTAMPTZ,
  wake_requested_at TIMESTAMPTZ,
  last_error_payload JSONB,
  trigger_source TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  started_at TIMESTAMPTZ,
  completed_at TIMESTAMPTZ
);
CREATE INDEX idx_problem_executions_problem ON problem_executions(problem_id, created_at);
CREATE INDEX idx_problem_executions_status ON problem_executions(status, desired_state);

CREATE TABLE request_records (
  request_record_id TEXT PRIMARY KEY,
  problem_id TEXT NOT NULL REFERENCES problems(problem_id) ON DELETE CASCADE,
  execution_id TEXT,
  worker_job_id TEXT,
  source TEXT NOT NULL,
  target_id TEXT,
  status TEXT NOT NULL DEFAULT 'queued',
  response_id TEXT,
  error_class TEXT,
  summary TEXT,
  request_artifact_key TEXT,
  response_artifact_key TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_request_records_problem_time ON request_records(problem_id, created_at);
CREATE INDEX idx_request_records_problem_source ON request_records(problem_id, source, created_at);
