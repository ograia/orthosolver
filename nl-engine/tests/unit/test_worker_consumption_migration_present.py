from pathlib import Path


def test_worker_consumption_migration_contains_controller_consumption_columns() -> None:
    sql = Path("database/migrations/0004_worker_job_controller_consumption.up.sql").read_text()
    assert "ALTER TABLE worker_jobs ADD COLUMN controller_consumed_at" in sql
    assert "ALTER TABLE worker_jobs ADD COLUMN controller_consumed_by_execution_id" in sql
    assert "CREATE INDEX idx_worker_jobs_controller_consumed" in sql
