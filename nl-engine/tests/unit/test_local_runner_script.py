from __future__ import annotations

import os
import subprocess
from pathlib import Path


def test_local_runner_script_requires_prob_or_create_flag() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    script = repo_root / "scripts" / "run_nl_only_local.sh"

    env = dict(os.environ)
    env.pop("PROB", None)

    result = subprocess.run(
        ["bash", str(script)],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    combined = f"{result.stdout}\n{result.stderr}"
    assert result.returncode == 1
    assert "PROB is empty" in combined
