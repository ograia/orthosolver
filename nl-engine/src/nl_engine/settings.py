from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    env: str = "dev"
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    data_dir: str = "data"
    storage_backend: Literal["filesystem", "gcs"] = "filesystem"
    gcs_bucket: str | None = None
    gcs_state_prefix: str = "orthosolver/state"
    gcs_artifact_prefix: str = "orthosolver/artifacts"

    openai_api_key: str | None = None
    openai_model_agent1: str = "gpt-5.4-nano"
    openai_model_agent2: str = "gpt-5.4-mini"
    openai_model_agent3: str = "gpt-5.4-mini"
    openai_model_agent4: str = "gpt-5.4-mini"
    openai_model_agent5: str = "gpt-5.4-mini"
    openai_model_agent6: str = "gpt-5.4-mini"
    openai_model_agent7: str = "gpt-5.4-mini"
    openai_model_agent8: str = "gpt-5.4-mini"
    openai_reasoning_effort: Literal["none", "low", "medium", "high", "xhigh"] = "none"
    openai_text_verbosity: Literal["low", "medium", "high"] = "medium"
    openai_timeout_seconds: int = 180
    # Buffer worker-thread usage writes by default to reduce local I/O churn.
    openai_usage_persistence_mode: Literal["immediate", "buffered", "disabled"] = "buffered"
    openai_webhook_secret: str | None = None

    lean_engine_base_url: str = "http://localhost:8081"
    lean_engine_runtime_mode: Literal["managed", "external"] = "managed"
    lean_engine_api_version: str = "v1"
    lean_engine_timeout_seconds: int = 30
    lean_engine_submit_http_retries: int = 3
    lean_engine_poll_http_retries: int = 3
    lean_engine_retry_backoff_seconds: float = 0.5
    lean_poll_interval_seconds: int = 5
    lean_engine_auth_mode: str = "none"  # none | oidc
    lean_engine_oidc_audience: str | None = None
    lean_engine_oidc_token_source: str = "env"  # env | google_adc
    lean_engine_oidc_token_env_var: str = "LEAN_ENGINE_OIDC_TOKEN"
    mock_lean_default_delay_seconds: int = 2

    artifact_store_dir: str = ".artifacts"
    sse_heartbeat_seconds: int = 15

    # Worker concurrency/backpressure defaults.
    worker_default_max_concurrency: int = 8
    worker_default_max_pending: int = 128
    worker_lease_seconds: int = 120
    worker_poll_interval_seconds: float = 0.5
    worker_enable_embedded_supervisor: bool = True

    # Per-1K-token pricing for cost estimation (USD), configurable via env.
    # Default values align with GPT-5.4 short-context public pricing:
    # input $2.50 / 1M, cached input $0.25 / 1M, output $15.00 / 1M.
    openai_price_input_per_1k: float = 0.0025
    openai_price_cached_input_per_1k: float = 0.00025
    openai_price_output_per_1k: float = 0.015

    # Cloud Monitoring export toggle (best-effort).
    enable_cloud_monitoring: bool = False
    gcp_project_id: str | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()
