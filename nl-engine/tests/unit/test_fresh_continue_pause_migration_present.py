from pathlib import Path


def test_fresh_continue_pause_migration_contains_generation_and_supersede_columns() -> None:
    sql = Path("database/migrations/0007_fresh_continue_and_pause.up.sql").read_text()
    assert "ALTER TABLE problems ADD COLUMN continuation_generation" in sql
    assert "ALTER TABLE problem_executions ADD COLUMN continuation_generation" in sql
    assert "ALTER TABLE worker_jobs ADD COLUMN continuation_generation" in sql
    assert "ALTER TABLE worker_jobs ADD COLUMN attempt_number" in sql
    assert "ALTER TABLE worker_jobs ADD COLUMN superseded_at" in sql
    assert "ALTER TABLE worker_jobs ADD COLUMN superseded_by_execution_id" in sql
    assert "ALTER TABLE worker_jobs ADD COLUMN supersede_reason" in sql
