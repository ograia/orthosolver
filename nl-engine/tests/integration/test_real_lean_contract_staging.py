from __future__ import annotations

import os
import time

import pytest

from nl_engine.lean_client.client import LeanClient
from nl_engine.settings import get_settings


pytestmark = pytest.mark.staging


def _enabled() -> bool:
    return os.getenv("RUN_STAGING_LEAN_TESTS") == "1"


@pytest.mark.skipif(not _enabled(), reason="staging tests disabled; set RUN_STAGING_LEAN_TESTS=1")
def test_real_lean_health() -> None:
    get_settings.cache_clear()
    client = LeanClient()
    health = client.health()
    assert isinstance(health, dict)


@pytest.mark.skipif(not _enabled(), reason="staging tests disabled; set RUN_STAGING_LEAN_TESTS=1")
@pytest.mark.parametrize(
    "mode,payload",
    [
        (
            "check_statement_plausibility",
            {"statement_nl": "For all n, n = n", "semantic_sketch": {}, "imports": ["Mathlib"], "timeout_seconds": 30},
        ),
        (
            "formalize_lemma",
            {
                "lemma_id": "staging_lemma",
                "statement_nl": "For all n, n = n",
                "semantic_sketch": {},
                "proof_nl": "Trivial by reflexivity.",
                "imports": ["Mathlib"],
                "timeout_seconds": 60,
            },
        ),
    ],
)
def test_real_lean_submit_and_poll(mode: str, payload: dict) -> None:
    get_settings.cache_clear()
    client = LeanClient()
    job_id = f"staging_job_{mode}_{int(time.time())}"
    submit = client.submit_job(
        {
            "job_id": job_id,
            "problem_id": "staging_problem",
            "target_id": "staging_target",
            "target_kind": "lemma",
            "mode": mode,
            "lean_image_tag": "Orthosolver-lean-4.18.0-mathlib-v4.18.0",
            "callback_url": None,
            "payload": payload,
        },
        request_id=f"req_{job_id}",
    )
    assert submit["job_id"] == job_id

    terminal = None
    for _ in range(60):
        polled = client.get_job(job_id)
        terminal = polled.get("status")
        if terminal in {"success", "repairable", "fatal", "cancelled"}:
            break
        time.sleep(1)

    assert terminal in {"success", "repairable", "fatal", "cancelled"}
