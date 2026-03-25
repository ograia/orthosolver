from pathlib import Path


def test_lemma_rejection_counters_migration_contains_counter_columns() -> None:
    sql = Path("database/migrations/0006_lemma_rejection_counters.up.sql").read_text()
    assert "ALTER TABLE lemmas ADD COLUMN consecutive_fatal_rejections" in sql
    assert "ALTER TABLE lemmas ADD COLUMN minor_rejection_count" in sql
    assert "ALTER TABLE lemmas DROP COLUMN IF EXISTS decomposition_no_accept_rounds" in sql
