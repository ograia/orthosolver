from pathlib import Path


def test_proof_dag_migration_contains_graph_and_dependency_tables() -> None:
    sql = Path("database/migrations/0008_proof_dag_hardening.up.sql").read_text()
    assert "ALTER TABLE problems ADD COLUMN active_proof_graph_id" in sql
    assert "ALTER TABLE problems ADD COLUMN dependency_verification_level" in sql
    assert "CREATE TABLE proof_graphs" in sql
    assert "CREATE TABLE proof_graph_nodes" in sql
    assert "CREATE TABLE proof_graph_edges" in sql
    assert "CREATE TABLE proof_dependency_checks" in sql
