#!/usr/bin/env bash
set -euo pipefail

API_BASE="${API_BASE:-http://localhost:8000}"
PROB="${PROB:-}"
CREATE_MODE=0
TITLE="${TITLE:-NL-only local test}"
STATEMENT_NL="${STATEMENT_NL:-For all natural numbers n, n = n.}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --create)
      CREATE_MODE=1
      shift
      ;;
    --problem-id)
      PROB="${2:-}"
      shift 2
      ;;
    --title)
      TITLE="${2:-}"
      shift 2
      ;;
    --statement)
      STATEMENT_NL="${2:-}"
      shift 2
      ;;
    *)
      echo "Unknown argument: $1"
      exit 1
      ;;
  esac
done

if [[ -z "${PROB}" && "${CREATE_MODE}" -ne 1 ]]; then
  echo "PROB is empty. Set PROB=<problem_id> or run with --create."
  exit 1
fi

if [[ "${CREATE_MODE}" -eq 1 ]]; then
  payload="$(jq -n \
    --arg title "${TITLE}" \
    --arg statement "${STATEMENT_NL}" \
    '{title:$title, statement_nl:$statement, config:{mode:{nl_only_mode:true}}}')"

  create_response="$(curl -sS -X POST "${API_BASE}/v1/problems" \
    -H "content-type: application/json" \
    -d "${payload}")"
  echo "${create_response}" | jq

  PROB="$(echo "${create_response}" | jq -r '.problem_id // empty')"
  if [[ -z "${PROB}" || "${PROB}" == "null" ]]; then
    echo "Could not extract problem_id from create response."
    exit 1
  fi
fi

echo "Using problem_id=${PROB}"
while true; do
  run_response="$(curl -sS -X POST "${API_BASE}/v1/problems/${PROB}/run")"
  echo "${run_response}" | jq
  status="$(echo "${run_response}" | jq -r '.status // empty')"
  if [[ "${status}" == "succeeded" || "${status}" == "failed" ]]; then
    break
  fi
  sleep 0.5
done

echo "Final problem:"
curl -sS "${API_BASE}/v1/problems/${PROB}" | jq
echo "Progress:"
curl -sS "${API_BASE}/v1/problems/${PROB}/progress" | jq
echo "Recent events:"
curl -sS "${API_BASE}/v1/problems/${PROB}/events?limit=200" | jq
