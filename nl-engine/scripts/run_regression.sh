#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

echo "Running regression-marked tests"
pytest -q -m regression

if [[ "${RUN_EVALUATOR:-0}" == "1" ]]; then
  echo "Running evaluator harness"
  PYTHONPATH=src python tools/evaluator/run_eval.py \
    --api-base-url "${EVAL_API_BASE_URL:-http://localhost:8000}" \
    --cases "${EVAL_CASES_PATH:-tools/evaluator/cases.sample.json}" \
    --out-json "${EVAL_OUT_JSON:-eval_results.json}" \
    --out-md "${EVAL_OUT_MD:-eval_results.md}"
fi
