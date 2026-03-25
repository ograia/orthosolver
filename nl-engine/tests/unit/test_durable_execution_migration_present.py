from pathlib import Path


def test_durable_execution_migration_contains_execution_and_request_tables() -> None:
    sql = Path("database/migrations/0003_durable_problem_execution.up.sql").read_text()
    assert "CREATE TABLE problem_executions" in sql
    assert "CREATE TABLE request_records" in sql
    assert "ALTER TABLE worker_jobs ADD COLUMN target_id" in sql
    assert "ALTER TABLE worker_jobs ADD COLUMN execution_id" in sql
