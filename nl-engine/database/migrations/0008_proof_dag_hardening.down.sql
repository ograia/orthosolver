DROP INDEX IF EXISTS idx_proof_dependency_checks_graph_target;
DROP INDEX IF EXISTS idx_proof_graph_edges_graph;
DROP INDEX IF EXISTS idx_proof_graph_nodes_owner;
DROP INDEX IF EXISTS idx_proof_graph_nodes_graph;
DROP INDEX IF EXISTS idx_proof_graphs_problem;

DROP TABLE IF EXISTS proof_dependency_checks;
DROP TABLE IF EXISTS proof_graph_edges;
DROP TABLE IF EXISTS proof_graph_nodes;
DROP TABLE IF EXISTS proof_graphs;

ALTER TABLE trusted_context DROP COLUMN IF EXISTS context_scope;
ALTER TABLE trusted_context DROP COLUMN IF EXISTS proof_graph_id;

ALTER TABLE lemmas DROP COLUMN IF EXISTS last_dependency_check_id;
ALTER TABLE lemmas DROP COLUMN IF EXISTS dependency_status;
ALTER TABLE lemmas DROP COLUMN IF EXISTS claim_node_id;
ALTER TABLE lemmas DROP COLUMN IF EXISTS proof_graph_id;

ALTER TABLE decompositions DROP COLUMN IF EXISTS equivalence_risk;
ALTER TABLE decompositions DROP COLUMN IF EXISTS dependency_status;
ALTER TABLE decompositions DROP COLUMN IF EXISTS proof_graph_id;

ALTER TABLE problems DROP COLUMN IF EXISTS dependency_verification_level;
ALTER TABLE problems DROP COLUMN IF EXISTS active_proof_graph_id;
