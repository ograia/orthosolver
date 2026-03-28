from __future__ import annotations

import tempfile

from nl_engine.domain.models import LlmUsageRecordORM
from nl_engine.observability.costs import cached_input_tokens_from_raw_usage, estimate_cost_usd
from nl_engine.persistence.db import FileStore
from nl_engine.persistence.repositories import LlmUsageRepository, ProblemRepository, RunCostRollupRepository
from nl_engine.domain.models import ProblemORM


def test_estimate_cost_usd_positive() -> None:
    assert estimate_cost_usd(1000, 500) > 0


def test_estimate_cost_usd_with_cached_input_is_lower() -> None:
    uncached = estimate_cost_usd(1000, 0, cached_input_tokens=0, model="gpt-5.4")
    partially_cached = estimate_cost_usd(1000, 0, cached_input_tokens=500, model="gpt-5.4")
    assert partially_cached < uncached


def test_cached_input_tokens_from_raw_usage_extracts_nested_value() -> None:
    raw_usage = {"input_tokens_details": {"cached_tokens": 42}}
    assert cached_input_tokens_from_raw_usage(raw_usage) == 42


def test_estimate_cost_usd_uses_model_specific_pricing() -> None:
    base = estimate_cost_usd(1000, 1000, model="gpt-5.4")
    pro = estimate_cost_usd(1000, 1000, model="gpt-5.4-pro")
    mini = estimate_cost_usd(1000, 1000, model="gpt-5.4-mini")
    nano = estimate_cost_usd(1000, 1000, model="gpt-5.4-nano")
    assert pro > base
    assert base > mini
    assert mini > nano


def test_usage_and_rollup_repositories() -> None:
    store = FileStore(tempfile.mkdtemp())
    # Create a problem so the directory exists
    ProblemRepository(store).create(
        ProblemORM(problem_id="prob_1", title="t", lean_image_tag="img", config={})
    )

    usage_repo = LlmUsageRepository(store)
    rollup_repo = RunCostRollupRepository(store)

    usage_repo.create(
        LlmUsageRecordORM(
            usage_id="usage_1",
            problem_id="prob_1",
            lemma_id=None,
            worker_job_id=None,
            stage="agent1",
            provider="openai",
            model="gpt-5.4",
            input_tokens=1000,
            output_tokens=500,
            estimated_cost_usd=0.01,
            raw_usage={"input_tokens": 1000, "output_tokens": 500},
        )
    )
    rollup_repo.upsert_daily_rollup(
        problem_id="prob_1",
        rollup_date="2026-03-12",
        input_tokens=1000,
        output_tokens=500,
        estimated_cost_usd=0.01,
    )
    rollup_repo.upsert_daily_rollup(
        problem_id="prob_1",
        rollup_date="2026-03-12",
        input_tokens=100,
        output_tokens=50,
        estimated_cost_usd=0.001,
    )

    usage_rows = usage_repo.list_by_problem("prob_1")
    assert len(usage_rows) == 1
    rollup_rows = rollup_repo.list_by_problem("prob_1")
    assert len(rollup_rows) == 1
    assert rollup_rows[0].total_input_tokens == 1100
    assert rollup_rows[0].total_output_tokens == 550
    assert round(rollup_rows[0].total_estimated_cost_usd, 3) == 0.011
