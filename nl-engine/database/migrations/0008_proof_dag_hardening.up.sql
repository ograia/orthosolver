ALTER TABLE problems ADD COLUMN active_proof_graph_id TEXT;
ALTER TABLE problems ADD COLUMN dependency_verification_level TEXT NOT NULL DEFAULT 'legacy';

ALTER TABLE decompositions ADD COLUMN proof_graph_id TEXT;
ALTER TABLE decompositions ADD COLUMN dependency_status TEXT NOT NULL DEFAULT 'legacy_unknown';
ALTER TABLE decompositions ADD COLUMN equivalence_risk TEXT NOT NULL DEFAULT 'none';

ALTER TABLE lemmas ADD COLUMN proof_graph_id TEXT;
ALTER TABLE lemmas ADD COLUMN claim_node_id TEXT;
ALTER TABLE lemmas ADD COLUMN dependency_status TEXT NOT NULL DEFAULT 'legacy_unknown';
ALTER TABLE lemmas ADD COLUMN last_dependency_check_id TEXT;

ALTER TABLE trusted_context ADD COLUMN proof_graph_id TEXT;
ALTER TABLE trusted_context ADD COLUMN context_scope TEXT NOT NULL DEFAULT 'problem_external';

CREATE TABLE proof_graphs (
  proof_graph_id TEXT PRIMARY KEY,
  problem_id TEXT NOT NULL,
  root_theorem_id TEXT NOT NULL,
  root_decomposition_id TEXT NOT NULL,
  graph_status TEXT NOT NULL,
  verification_status TEXT NOT NULL,
  dag_version TEXT NOT NULL,
  created_at TIMESTAMP NOT NULL,
  updated_at TIMESTAMP NOT NULL
);

CREATE TABLE proof_graph_nodes (
  graph_node_id TEXT PRIMARY KEY,
  proof_graph_id TEXT NOT NULL,
  node_kind TEXT NOT NULL,
  owner_kind TEXT NOT NULL,
  owner_id TEXT NOT NULL,
  statement_nl TEXT,
  semantic_sketch_json JSON NOT NULL DEFAULT '{}',
  normalized_claim_hash TEXT,
  node_status TEXT NOT NULL,
  metadata JSON NOT NULL DEFAULT '{}',
  created_at TIMESTAMP NOT NULL,
  updated_at TIMESTAMP NOT NULL
);

CREATE TABLE proof_graph_edges (
  edge_id TEXT PRIMARY KEY,
  proof_graph_id TEXT NOT NULL,
  from_node_id TEXT NOT NULL,
  to_node_id TEXT NOT NULL,
  edge_kind TEXT NOT NULL,
  dependency_scope TEXT NOT NULL,
  introduced_by_worker_job_id TEXT,
  metadata JSON NOT NULL DEFAULT '{}',
  created_at TIMESTAMP NOT NULL
);

CREATE TABLE proof_dependency_checks (
  dependency_check_id TEXT PRIMARY KEY,
  proof_graph_id TEXT NOT NULL,
  target_node_id TEXT NOT NULL,
  artifact_kind TEXT NOT NULL,
  artifact_id TEXT NOT NULL,
  check_status TEXT NOT NULL,
  violations_json JSON NOT NULL DEFAULT '[]',
  created_at TIMESTAMP NOT NULL
);

CREATE INDEX idx_proof_graphs_problem ON proof_graphs(problem_id);
CREATE INDEX idx_proof_graph_nodes_graph ON proof_graph_nodes(proof_graph_id);
CREATE INDEX idx_proof_graph_nodes_owner ON proof_graph_nodes(proof_graph_id, owner_kind, owner_id);
CREATE INDEX idx_proof_graph_edges_graph ON proof_graph_edges(proof_graph_id);
CREATE INDEX idx_proof_dependency_checks_graph_target ON proof_dependency_checks(proof_graph_id, target_node_id);
