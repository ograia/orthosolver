from __future__ import annotations

from datetime import UTC, datetime
import threading
from typing import Any

from nl_engine.domain.models import LlmUsageRecordORM
from nl_engine.persistence.db import FileStore, get_file_store
from nl_engine.persistence.repositories import LlmUsageRepository, RunCostRollupRepository
from nl_engine.services.ids import new_id
from nl_engine.settings import get_settings
from nl_engine.observability.metrics import MetricsExporter

MODEL_PRICING_USD_PER_1M: dict[str, dict[str, float]] = {
    "gpt-5.4": {"input": 2.50, "cached_input": 0.25, "output": 15.00},
    "gpt-5.4-pro": {"input": 30.00, "cached_input": 30.00, "output": 180.00},
    "gpt-5.4-mini": {"input": 0.75, "cached_input": 0.075, "output": 4.50},
    "gpt-5.4-nano": {"input": 0.20, "cached_input": 0.020, "output": 1.25},
}


def _token_usage_from_response(response: Any) -> tuple[int, int, dict[str, Any] | None]:
    usage = getattr(response, "usage", None)
    if usage is None:
        return 0, 0, None

    if hasattr(usage, "model_dump"):
        usage_dict = usage.model_dump()
    elif isinstance(usage, dict):
        usage_dict = usage
    else:
        usage_dict = {}
        for key in ("input_tokens", "output_tokens", "total_tokens"):
            if hasattr(usage, key):
                usage_dict[key] = getattr(usage, key)

    input_tokens = int(usage_dict.get("input_tokens", 0) or 0)
    output_tokens = int(usage_dict.get("output_tokens", 0) or 0)
    return input_tokens, output_tokens, usage_dict


def cached_input_tokens_from_raw_usage(raw_usage: dict[str, Any] | None) -> int:
    if not isinstance(raw_usage, dict):
        return 0
    details = raw_usage.get("input_tokens_details")
    if isinstance(details, dict):
        value = details.get("cached_tokens", details.get("cached_input_tokens", 0))
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0
    value = raw_usage.get("cached_input_tokens", 0)
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _pricing_per_1k(model: str | None) -> tuple[float, float, float]:
    settings = get_settings()
    normalized = str(model or "").strip().lower()
    model_pricing = MODEL_PRICING_USD_PER_1M.get(normalized)
    if not model_pricing:
        return (
            settings.openai_price_input_per_1k,
            settings.openai_price_cached_input_per_1k,
            settings.openai_price_output_per_1k,
        )
    return (
        model_pricing["input"] / 1000.0,
        model_pricing["cached_input"] / 1000.0,
        model_pricing["output"] / 1000.0,
    )


def estimate_cost_usd(
    input_tokens: int,
    output_tokens: int,
    *,
    cached_input_tokens: int = 0,
    model: str | None = None,
) -> float:
    input_per_1k, cached_input_per_1k, output_per_1k = _pricing_per_1k(model)
    cached = max(0, min(int(cached_input_tokens), int(input_tokens)))
    uncached_input = max(0, int(input_tokens) - cached)
    return round(
        (uncached_input / 1000.0) * input_per_1k
        + (cached / 1000.0) * cached_input_per_1k
        + (output_tokens / 1000.0) * output_per_1k,
        8,
    )


def _usage_row(
    *,
    problem_id: str | None,
    lemma_id: str | None,
    worker_job_id: str | None,
    stage: str,
    provider: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    estimated_cost_usd: float,
    raw_usage: dict[str, Any] | None,
) -> dict[str, Any]:
    now = datetime.now(UTC)
    return {
        "usage_id": new_id("usage"),
        "problem_id": problem_id,
        "lemma_id": lemma_id,
        "worker_job_id": worker_job_id,
        "stage": stage,
        "provider": provider,
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "estimated_cost_usd": estimated_cost_usd,
        "raw_usage": raw_usage,
        "created_at": now,
        "rollup_date": now.date().isoformat(),
    }


def record_llm_usage(
    *,
    problem_id: str | None,
    lemma_id: str | None,
    worker_job_id: str | None,
    stage: str,
    provider: str,
    model: str,
    response: Any,
    db_session: Any = None,
    usage_persistence_mode: str | None = None,
    usage_buffer: list[dict[str, Any]] | None = None,
    usage_buffer_lock: threading.Lock | None = None,
) -> None:
    input_tokens, output_tokens, raw_usage = _token_usage_from_response(response)
    cached_input_tokens = cached_input_tokens_from_raw_usage(raw_usage)
    estimated_cost_usd = estimate_cost_usd(
        input_tokens,
        output_tokens,
        cached_input_tokens=cached_input_tokens,
        model=model,
    )
    record_usage_row(
        problem_id=problem_id,
        lemma_id=lemma_id,
        worker_job_id=worker_job_id,
        stage=stage,
        provider=provider,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        estimated_cost_usd=estimated_cost_usd,
        raw_usage=raw_usage,
        db_session=db_session,
        usage_persistence_mode=usage_persistence_mode,
        usage_buffer=usage_buffer,
        usage_buffer_lock=usage_buffer_lock,
    )


def record_usage_row(
    *,
    problem_id: str | None,
    lemma_id: str | None,
    worker_job_id: str | None,
    stage: str,
    provider: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    estimated_cost_usd: float,
    raw_usage: dict[str, Any] | None = None,
    db_session: Any = None,
    usage_persistence_mode: str | None = None,
    usage_buffer: list[dict[str, Any]] | None = None,
    usage_buffer_lock: threading.Lock | None = None,
) -> None:
    settings = get_settings()
    mode = usage_persistence_mode or settings.openai_usage_persistence_mode
    metrics = MetricsExporter()

    row = _usage_row(
        problem_id=problem_id,
        lemma_id=lemma_id,
        worker_job_id=worker_job_id,
        stage=stage,
        provider=provider,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        estimated_cost_usd=estimated_cost_usd,
        raw_usage=raw_usage,
    )

    if mode == "disabled":
        return

    if mode == "buffered":
        if usage_buffer is not None:
            if usage_buffer_lock is not None:
                with usage_buffer_lock:
                    usage_buffer.append(row)
            else:
                usage_buffer.append(row)
            return
        # Fallback to immediate if caller did not provide a buffer.
        mode = "immediate"

    def _write(store: FileStore, item: dict[str, Any]) -> None:
        usage_repo = LlmUsageRepository(store)
        rollup_repo = RunCostRollupRepository(store)
        usage_repo.create(
            LlmUsageRecordORM(
                usage_id=item["usage_id"],
                problem_id=item["problem_id"],
                lemma_id=item["lemma_id"],
                worker_job_id=item["worker_job_id"],
                stage=item["stage"],
                provider=item["provider"],
                model=item["model"],
                input_tokens=item["input_tokens"],
                output_tokens=item["output_tokens"],
                estimated_cost_usd=item["estimated_cost_usd"],
                raw_usage=item["raw_usage"],
                created_at=item["created_at"],
            )
        )
        if item["problem_id"]:
            rollup_repo.upsert_daily_rollup(
                problem_id=item["problem_id"],
                rollup_date=item["rollup_date"],
                input_tokens=item["input_tokens"],
                output_tokens=item["output_tokens"],
                estimated_cost_usd=item["estimated_cost_usd"],
            )

    store = db_session if isinstance(db_session, FileStore) else get_file_store()
    try:
        _write(store, row)
    except Exception:
        # Non-critical write path: swallow errors to avoid disrupting agent execution.
        pass

    metrics.emit("input_tokens", float(input_tokens), {"provider": provider, "model": model})
    metrics.emit("output_tokens", float(output_tokens), {"provider": provider, "model": model})
    metrics.emit("estimated_cost_usd", float(estimated_cost_usd), {"provider": provider, "model": model})


def flush_buffered_usage_rows(
    *,
    usage_buffer: list[dict[str, Any]],
    db_session: Any = None,
    usage_buffer_lock: threading.Lock | None = None,
) -> int:
    if usage_buffer_lock is not None:
        with usage_buffer_lock:
            rows = list(usage_buffer)
            usage_buffer.clear()
    else:
        rows = list(usage_buffer)
        usage_buffer.clear()

    for row in rows:
        record_usage_row(
            problem_id=row["problem_id"],
            lemma_id=row["lemma_id"],
            worker_job_id=row["worker_job_id"],
            stage=row["stage"],
            provider=row["provider"],
            model=row["model"],
            input_tokens=row["input_tokens"],
            output_tokens=row["output_tokens"],
            estimated_cost_usd=row["estimated_cost_usd"],
            raw_usage=row["raw_usage"],
            db_session=db_session,
            usage_persistence_mode="immediate",
        )
    return len(rows)
