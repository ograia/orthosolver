#!/usr/bin/env python3
from __future__ import annotations

import json
import sys

from nl_engine.artifacts.store import ArtifactStore
from nl_engine.persistence.db import FileStore
from nl_engine.services.proof_bundles import ProofBundleService


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: reconstruct_legacy_proof_bundle.py <problem_id> [root_decomposition_id]", file=sys.stderr)
        return 2

    problem_id = sys.argv[1]
    root_decomposition_id = sys.argv[2] if len(sys.argv) > 2 else None
    service = ProofBundleService(FileStore(), ArtifactStore())
    bundle = service.reconstruct_legacy_root_bundle(
        problem_id,
        root_decomposition_id=root_decomposition_id,
    )
    if bundle is None:
        print(f"failed to reconstruct proof bundle for {problem_id}", file=sys.stderr)
        return 1

    print(json.dumps(bundle, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
