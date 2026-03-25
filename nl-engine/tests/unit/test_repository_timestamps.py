from __future__ import annotations

from nl_engine.domain.models import LemmaORM
from nl_engine.persistence.db import FileStore
from nl_engine.persistence.repositories import LemmaRepository


def test_lemma_repository_save_updates_timestamps(tmp_path) -> None:
    store = FileStore(str(tmp_path / "data"))
    repo = LemmaRepository(store)
    lemma = LemmaORM(
        lemma_id="lem_test",
        problem_id="prob_test",
        parent_id="thm_root",
        parent_kind="theorem",
        statement_nl="n = n",
    )
    repo.create(lemma)
    created_updated_at = lemma.updated_at
    created_last_activity_at = lemma.last_activity_at

    lemma.routing_status = "retry_solver"
    repo.save(lemma)

    assert lemma.updated_at >= created_updated_at
    assert lemma.last_activity_at >= created_last_activity_at
    assert lemma.routing_status == "retry_solver"
