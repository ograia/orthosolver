from pathlib import Path


def test_phase34_migration_contains_worker_and_cost_tables() -> None:
    sql = Path("database/migrations/0002_phase34_hardening.up.sql").read_text()
    assert "CREATE TABLE worker_jobs" in sql
    assert "CREATE TABLE llm_usage_records" in sql
    assert "CREATE TABLE run_cost_rollups" in sql
    assert "CREATE INDEX idx_worker_jobs_kind_status" in sql
    assert "CREATE INDEX idx_llm_usage_problem_time" in sql
