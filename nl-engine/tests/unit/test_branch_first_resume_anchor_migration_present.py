from pathlib import Path


def test_branch_first_resume_anchor_migration_contains_anchor_and_round_columns() -> None:
    sql = Path("database/migrations/0005_branch_first_resume_anchor.up.sql").read_text()
    assert "ALTER TABLE problems ADD COLUMN resume_anchor_lemma_id" in sql
    assert "ALTER TABLE problems ADD COLUMN resume_anchor_owner_decomposition_id" in sql
    assert "ALTER TABLE lemmas ADD COLUMN decomposition_no_accept_rounds" in sql
    assert "CREATE INDEX idx_problems_resume_anchor_lemma" in sql
