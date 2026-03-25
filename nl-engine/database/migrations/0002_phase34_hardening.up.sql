-- Phase 3/4 hardening tables from docs/nl_engine.tex implementation sequence.

CREATE TABLE worker_jobs (
  worker_job_id TEXT PRIMARY KEY,
  problem_id TEXT NOT NULL REFERENCES problems(problem_id) ON DELETE CASCADE,
  worker_kind TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'queued',
  payload JSONB NOT NULL DEFAULT '{}'::jsonb,
  result_payload JSONB,
  error_payload JSONB,
  lease_owner TEXT,
  lease_expires_at TIMESTAMPTZ,
  attempt_count INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL DEFAULT 5,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_worker_jobs_kind_status ON worker_jobs(worker_kind, status);
CREATE INDEX idx_worker_jobs_lease ON worker_jobs(worker_kind, lease_expires_at);

CREATE TABLE llm_usage_records (
  usage_id TEXT PRIMARY KEY,
  problem_id TEXT,
  lemma_id TEXT,
  worker_job_id TEXT,
  stage TEXT NOT NULL,
  provider TEXT NOT NULL,
  model TEXT NOT NULL,
  input_tokens INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0,
  estimated_cost_usd DOUBLE PRECISION NOT NULL DEFAULT 0,
  raw_usage JSONB,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_llm_usage_problem_time ON llm_usage_records(problem_id, created_at);
CREATE INDEX idx_llm_usage_model_time ON llm_usage_records(model, created_at);

CREATE TABLE run_cost_rollups (
  rollup_id TEXT PRIMARY KEY,
  problem_id TEXT NOT NULL,
  rollup_date TEXT NOT NULL,
  total_input_tokens INTEGER NOT NULL DEFAULT 0,
  total_output_tokens INTEGER NOT NULL DEFAULT 0,
  total_estimated_cost_usd DOUBLE PRECISION NOT NULL DEFAULT 0,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(problem_id, rollup_date)
);
CREATE INDEX idx_run_rollups_date ON run_cost_rollups(rollup_date);
