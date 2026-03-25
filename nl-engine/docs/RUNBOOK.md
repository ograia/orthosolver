# NL Engine Runbook

Practical run/debug guide for local and staging-like workflows.

## 1. Local Setup
```bash
cd /Users/Omar/orthos-ai/nl-engine
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env
```

Required env:
- `OPENAI_API_KEY`
- `OPENAI_MODEL_AGENT1` ... `OPENAI_MODEL_AGENT5`
- `LEAN_ENGINE_BASE_URL` (mock default `http://localhost:8081`)
- optional auth for real Lean (`LEAN_ENGINE_AUTH_MODE=oidc` and token config)

Start services:
```bash
uvicorn mock_lean.main:app --reload --host 0.0.0.0 --port 8081
uvicorn --app-dir src nl_engine.api.main:app --reload --host 0.0.0.0 --port 8000
```

Optional standalone durable worker loop:
```bash
python -m nl_engine.worker.main
```

## 2. Minimal API Flow
1. Create:
```bash
curl -sS -X POST "http://localhost:8000/v1/problems" \
  -H "content-type: application/json" \
  -d '{"title":"Demo","statement_nl":"For all n, n = n","config":{"mode":{"nl_only_mode":true}}}' | jq
```

2. Start durable execution:
```bash
PROB=<problem_id>
curl -sS -X POST "http://localhost:8000/v1/problems/${PROB}/start" | jq
```

3. Poll until terminal:
```bash
while true; do
  STATUS=$(curl -sS "http://localhost:8000/v1/problems/${PROB}" | jq -r '.problem.status')
  EXEC_STATUS=$(curl -sS "http://localhost:8000/v1/problems/${PROB}/execution" | jq -r '.execution.status // "none"')
  echo "problem=${STATUS} execution=${EXEC_STATUS}"
  if [ "$STATUS" = "succeeded" ] || [ "$STATUS" = "failed" ]; then
    break
  fi
  sleep 0.5
done
```

4. Inspect:
```bash
curl -sS "http://localhost:8000/v1/problems/${PROB}/progress" | jq
curl -sS "http://localhost:8000/v1/problems/${PROB}/events?limit=200" | jq
curl -sS "http://localhost:8000/v1/problems/${PROB}/cost" | jq
```

## 3. Important Lifecycle Semantics
- `POST /v1/problems` is create-only (includes Agent1 sketch).
- Decomposition starts only after `POST /start` or compatibility `POST /run`.
- `POST /start` creates or reuses a durable execution and returns immediately.
- `POST /start` with `X-Debug-Run-Trigger: continue_button` creates a fresh continuation generation and supersedes stale in-flight jobs.
- `POST /run` is a compatibility alias; it no longer performs controller work inline.
- `POST /resume` resumes only failed problems and appends a new durable execution.
- `POST /pause` pauses non-terminal problems and requests execution stop.
- Execution correctness depends on DB-backed `problem_executions` and `worker_jobs`, not repeated API ticks.

## 4. Debug UI Workflow (`/debug`)
Open:
```bash
http://localhost:8000/debug
```

Recommended loop:
1. Submit request from guided form or raw JSON.
2. Keep `Auto-start after submit` and `Live Refresh` on for normal debugging.
3. Use the `Pause` toolbar button to stop active non-terminal runs.
4. Use the `Continue` toolbar button to resume failed runs (infra-first auto-routing) or re-trigger non-terminal runs via a fresh continuation generation.
5. Use `Requests` tab to see API/agent/Lean request progression and completion state.
6. Use `Tree Explorer` for theorem/decomposition/lemma topology.
   When root parallel decomposition is enabled and more than one root track is carried forward, use the `Root Track` selector to inspect each disjoint tree separately.
7. Use `Artifacts` tab to inspect exact prompt/input/output artifacts.
8. Use `Overview` panel for aggregate status, token/cost stats, NL-only final output JSON, and the original per-problem input JSON payload.
9. Use delete/reset endpoints for clean local repro loops.
   `Reset Local` stops UI auto-run/live-refresh first, blocks new execution starts on the backend, waits for active in-process work to unwind, and then deletes all local rows/artifacts.

Visibility behavior:
- Failed decomposition nodes remain visible.
- Only lemmas owned by accepted decompositions are shown in operational lemma views.

## 5. Per-Agent LLM Overrides
Supported in create payload under `config.llm.agent1..agent5`:
- `model` (`gpt-5.4|gpt-5.4-pro|gpt-5-mini`)
- `thinking_level` (`none|low|medium|high|xhigh`)
- `verbosity` (`low|medium|high`)
- `timeout_seconds` (default `600`)
- Local defaults are tuned for cheap validation: `gpt-5-mini`, `thinking_level=none`, `verbosity=low`.

Example:
```json
{
  "config": {
    "llm": {
      "agent1": { "model": "gpt-5.4", "thinking_level": "low", "verbosity": "medium", "timeout_seconds": 600 },
      "agent2": { "model": "gpt-5.4-pro", "thinking_level": "high", "verbosity": "high", "timeout_seconds": 600 }
    }
  }
}
```

## 6. Retry/Decompose Behavior (Lemma Stage)
- Medium-severity vetter findings force solver retry with feedback.
- High-severity findings increment consecutive fatal rejections and retry until fatal cap.
- Minor-severity findings increment cumulative minor rejections and reset consecutive fatal rejections.
- Lemma decomposition is triggered when either cap is reached:
  - `max_consecutive_fatal_rejections_per_lemma`
  - `max_minor_rejections_per_lemma`
- Lemma decomposition attempts are limited by `max_decompositions_per_failed_lemma`.
- Major drift and false/suspect statement routes remain hard-stop/escalation paths.

## 7. Root Parallel Decomposition
- `parallel_root_decompositions_n` sends that many root Agent2 decomposition requests as durable worker jobs.
- `parallel_root_take_k` keeps the first `K` vetted root tracks to finish alive for lemma solving.
- Only the root supports multiple parallel tracks; recursive lemma decomposition stays single-track.

## 8. Compatibility `/run` and Interrupted Legacy Requests
If a process interruption occurs while an old compatibility `/run` request artifact is left incomplete:
```bash
curl -sS -X POST "http://localhost:8000/v1/debug/problems/${PROB}/reconcile-incomplete-runs" | jq
```

This writes terminal interruption markers for orphaned compatibility run-request artifacts when no active in-process work exists.

## 9. Troubleshooting Quick Reference
- `OPENAI_API_KEY is not configured`:
  - ensure env is loaded in the process running uvicorn
- `execution already active` or delete blocked by active execution:
  - a durable background execution already exists; inspect `/execution` or the debug execution list
- `db_locked`:
  - local SQLite contention; retry
- only Agent1 artifacts with no decomposition:
  - execution was not started
- agent input exists but no parsed output:
  - inspect `*_request_state_attempt_*.json`, `*_raw_output_attempt_*.txt`, and validation artifacts

## 10. Migrations
SQL source of truth:
- `database/migrations/0001_initial.up.sql`
- `database/migrations/0002_phase34_hardening.up.sql`

Using `golang-migrate`:
```bash
migrate -path database/migrations -database "$DATABASE_URL" up
```

## 11. Test Commands
All tests:
```bash
pytest -q
```

Regression subset:
```bash
pytest -q -m regression
```

Staging Lean contract tests:
```bash
RUN_STAGING_LEAN_TESTS=1 pytest -q -m staging
```
