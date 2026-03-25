-- Initial schema based on docs/nl_engine.tex Section 8
CREATE TYPE problem_status AS ENUM ('created','running','paused','succeeded','failed');
CREATE TYPE input_mode AS ENUM ('nl_only','lean_only','both');
CREATE TYPE verification_level AS ENUM ('formal','nl_only');
CREATE TYPE node_kind AS ENUM ('theorem','lemma');
CREATE TYPE node_status AS ENUM ('open','running','succeeded','failed');
CREATE TYPE llm_vetting_status AS ENUM ('pending','accepted','rejected_minor','rejected_fatal');
CREATE TYPE lean_assembly_status AS ENUM ('pending','success','fatal','skipped');
CREATE TYPE controller_status AS ENUM ('pending','active','standby','failed','succeeded');
CREATE TYPE statement_status AS ENUM ('unvetted','plausible','suspect','false','formalized');
CREATE TYPE proof_status AS ENUM ('open','proof_found','proof_flawed','proof_vetted','proof_formalized','nl_accepted','failed','proof_exhausted');
CREATE TYPE routing_status AS ENUM ('open','retry_solver','decompose_further','send_to_lean','blocked','done');
CREATE TYPE lean_job_mode AS ENUM ('check_assembly','formalize_lemma','assemble_root','check_statement_plausibility');
CREATE TYPE lean_job_status AS ENUM ('queued','running','success','repairable','fatal','cancelled');
CREATE TYPE lean_error_class AS ENUM ('syntax','type_mismatch','missing_import','missing_library_fact','tactic_failure','false_lemma_suspected','major_proof_gap','assembly_invalid','assembly_composition_failure','bad_statement_translation','environment_mismatch','unknown_fatal');
CREATE TYPE lean_error_scope AS ENUM ('statement','proof','assembly','environment','infrastructure');
CREATE TYPE failure_reason AS ENUM ('lemma_false','proof_exhausted','lean_difficulty','assembly_composition_failure','global_timeout','unknown');

CREATE TABLE problems (
  problem_id TEXT PRIMARY KEY,
  status problem_status NOT NULL DEFAULT 'created',
  title TEXT NOT NULL,
  input_mode input_mode NOT NULL DEFAULT 'nl_only',
  root_theorem_id TEXT,
  active_decomposition_id TEXT,
  standby_decomposition_id TEXT,
  lean_image_tag TEXT NOT NULL,
  config JSONB NOT NULL,
  nl_only_mode BOOLEAN NOT NULL DEFAULT false,
  verification_level verification_level DEFAULT 'formal',
  failure_report_artifact_id TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE theorems (
  theorem_id TEXT PRIMARY KEY,
  problem_id TEXT NOT NULL REFERENCES problems(problem_id) ON DELETE CASCADE,
  kind node_kind NOT NULL DEFAULT 'theorem',
  statement_nl TEXT NOT NULL,
  statement_lean TEXT,
  statement_semantic_sketch JSONB NOT NULL,
  status node_status NOT NULL DEFAULT 'open',
  active_decomposition_id TEXT,
  final_decl_name TEXT,
  artifact_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_theorems_problem ON theorems(problem_id);

CREATE TABLE decompositions (
  decomposition_id TEXT PRIMARY KEY,
  problem_id TEXT NOT NULL REFERENCES problems(problem_id) ON DELETE CASCADE,
  node_id TEXT NOT NULL,
  node_kind node_kind NOT NULL,
  strategy_summary TEXT NOT NULL,
  shared_context JSONB NOT NULL DEFAULT '[]'::jsonb,
  lemma_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
  assembly_plan_id TEXT,
  pinned_statement_signatures JSONB,
  formalization_cost_estimate REAL,
  llm_vetting_status llm_vetting_status NOT NULL DEFAULT 'pending',
  lean_assembly_status lean_assembly_status NOT NULL DEFAULT 'pending',
  controller_status controller_status NOT NULL DEFAULT 'pending',
  previous_attempt_summaries JSONB NOT NULL DEFAULT '[]'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_decomp_problem ON decompositions(problem_id);
CREATE INDEX idx_decomp_node ON decompositions(node_id);
CREATE INDEX idx_decomp_controller ON decompositions(controller_status);

CREATE TABLE assembly_plans (
  assembly_plan_id TEXT PRIMARY KEY,
  problem_id TEXT NOT NULL REFERENCES problems(problem_id) ON DELETE CASCADE,
  decomposition_id TEXT NOT NULL REFERENCES decompositions(decomposition_id) ON DELETE CASCADE,
  root_node_id TEXT NOT NULL,
  steps JSONB NOT NULL,
  proof_skeleton_nl TEXT NOT NULL,
  is_trivially_composable BOOLEAN NOT NULL DEFAULT true,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE lemmas (
  lemma_id TEXT PRIMARY KEY,
  problem_id TEXT NOT NULL REFERENCES problems(problem_id) ON DELETE CASCADE,
  parent_id TEXT NOT NULL,
  parent_kind node_kind NOT NULL,
  kind node_kind NOT NULL DEFAULT 'lemma',
  depth INTEGER NOT NULL DEFAULT 1,
  statement_nl TEXT NOT NULL,
  statement_lean TEXT,
  statement_semantic_sketch JSONB NOT NULL,
  shared_context_refs JSONB NOT NULL DEFAULT '[]'::jsonb,
  role_in_parent TEXT,
  assembly_step_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
  difficulty_estimate REAL,
  formalization_cost_estimate REAL,
  statement_status statement_status NOT NULL DEFAULT 'unvetted',
  proof_status proof_status NOT NULL DEFAULT 'open',
  routing_status routing_status NOT NULL DEFAULT 'open',
  latest_nl_proof TEXT,
  latest_vetter_report_id TEXT,
  latest_lean_result_id TEXT,
  solver_attempt_count INTEGER NOT NULL DEFAULT 0,
  decomposition_count INTEGER NOT NULL DEFAULT 0,
  lean_attempt_count INTEGER NOT NULL DEFAULT 0,
  lean_identical_fatal_count INTEGER NOT NULL DEFAULT 0,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_lemmas_problem ON lemmas(problem_id);
CREATE INDEX idx_lemmas_parent ON lemmas(parent_id);
CREATE INDEX idx_lemmas_routing ON lemmas(problem_id, routing_status);
CREATE INDEX idx_lemmas_proof ON lemmas(problem_id, proof_status);

CREATE TABLE vetter_reports (
  report_id TEXT PRIMARY KEY,
  problem_id TEXT NOT NULL REFERENCES problems(problem_id) ON DELETE CASCADE,
  target_id TEXT NOT NULL,
  target_kind TEXT NOT NULL,
  statement_status TEXT,
  proof_status TEXT,
  drift_assessment JSONB,
  recommended_action TEXT,
  reason TEXT,
  confidence REAL,
  feedback_for_solver TEXT,
  detailed_findings JSONB NOT NULL DEFAULT '[]'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_vetter_target ON vetter_reports(target_id);

CREATE TABLE lean_jobs (
  job_id TEXT PRIMARY KEY,
  problem_id TEXT NOT NULL REFERENCES problems(problem_id) ON DELETE CASCADE,
  target_id TEXT NOT NULL,
  target_kind TEXT NOT NULL,
  mode lean_job_mode NOT NULL,
  status lean_job_status NOT NULL DEFAULT 'queued',
  attempt_index INTEGER NOT NULL DEFAULT 1,
  lean_image_tag TEXT NOT NULL,
  request_artifact_id TEXT,
  result_artifact_id TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_lean_jobs_problem ON lean_jobs(problem_id);
CREATE INDEX idx_lean_jobs_target ON lean_jobs(target_id);
CREATE INDEX idx_lean_jobs_status ON lean_jobs(status);

CREATE TABLE lean_results (
  result_id TEXT PRIMARY KEY,
  job_id TEXT NOT NULL REFERENCES lean_jobs(job_id) ON DELETE CASCADE,
  status lean_job_status NOT NULL,
  error_class lean_error_class,
  error_scope lean_error_scope,
  error_message TEXT,
  diagnostics JSONB NOT NULL DEFAULT '[]'::jsonb,
  decl_name TEXT,
  lean_code_artifact_id TEXT,
  compiler_log_artifact_id TEXT,
  recommended_next_step TEXT,
  routing_confidence REAL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_lean_results_job ON lean_results(job_id);

CREATE TABLE trusted_context (
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  problem_id TEXT NOT NULL REFERENCES problems(problem_id) ON DELETE CASCADE,
  decl_name TEXT NOT NULL,
  lean_code TEXT NOT NULL,
  source_lemma_id TEXT NOT NULL,
  source_job_id TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(problem_id, decl_name)
);
CREATE INDEX idx_trusted_ctx_problem ON trusted_context(problem_id);

CREATE TABLE failure_reports (
  failure_report_id TEXT PRIMARY KEY,
  problem_id TEXT NOT NULL REFERENCES problems(problem_id) ON DELETE CASCADE,
  failure_reason failure_reason NOT NULL,
  terminal_lemma_id TEXT,
  terminal_error_class TEXT,
  terminal_error_message TEXT,
  partial_tree JSONB NOT NULL,
  trusted_context_at_failure JSONB NOT NULL DEFAULT '[]'::jsonb,
  all_decomposition_attempts JSONB NOT NULL DEFAULT '[]'::jsonb,
  routing_log_summary JSONB NOT NULL DEFAULT '[]'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE events (
  event_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  problem_id TEXT NOT NULL REFERENCES problems(problem_id) ON DELETE CASCADE,
  target_node_id TEXT,
  stage TEXT NOT NULL,
  old_status TEXT,
  new_status TEXT,
  worker_job_id TEXT,
  reason TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_events_problem ON events(problem_id);
CREATE INDEX idx_events_target ON events(target_node_id);
CREATE INDEX idx_events_time ON events(problem_id, created_at);
