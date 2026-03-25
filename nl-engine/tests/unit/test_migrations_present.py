from pathlib import Path


def test_migration_contains_required_tables_enums_and_indexes() -> None:
    sql = Path("database/migrations/0001_initial.up.sql").read_text()

    # Tables
    assert "CREATE TABLE problems" in sql
    assert "CREATE TABLE theorems" in sql
    assert "CREATE TABLE decompositions" in sql
    assert "CREATE TABLE assembly_plans" in sql
    assert "CREATE TABLE lemmas" in sql
    assert "CREATE TABLE vetter_reports" in sql
    assert "CREATE TABLE lean_jobs" in sql
    assert "CREATE TABLE lean_results" in sql
    assert "CREATE TABLE trusted_context" in sql
    assert "CREATE TABLE failure_reports" in sql
    assert "CREATE TABLE events" in sql

    # Critical enums
    assert "CREATE TYPE verification_level" in sql
    assert "CREATE TYPE routing_status" in sql
    assert "CREATE TYPE lean_job_mode" in sql
    assert "CREATE TYPE lean_error_class" in sql

    # Routing-critical indexes
    assert "CREATE INDEX idx_lemmas_routing" in sql
    assert "CREATE INDEX idx_lemmas_proof" in sql
    assert "CREATE INDEX idx_decomp_controller" in sql
    assert "CREATE INDEX idx_events_time" in sql
