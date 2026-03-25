# Public API Contract (v1)

Every response includes:
- `request_id`
- `server_time`

## Problem lifecycle
- `POST /v1/problems`
- `GET /v1/problems/{problem_id}`
- `POST /v1/problems/{problem_id}/run`
- `GET /v1/problems/{problem_id}/tree`
- `GET /v1/problems/{problem_id}/failure-report`

`POST /v1/problems` accepts optional per-problem agent LLM overrides in `config.llm`:
- `config.llm.agent1..agent5.model`
- `config.llm.agent1..agent5.thinking_level` (`none|low|medium|high|xhigh`)
- `reasoning_effort` is accepted as an input alias for `thinking_level`

## Monitoring and replay
- `GET /v1/problems/{problem_id}/progress`
- `GET /v1/problems/{problem_id}/events?after_event_id=&limit=`
- `GET /v1/problems/{problem_id}/events/stream` (SSE with `Last-Event-ID` resume)
- `GET /v1/problems/{problem_id}/cost` (usage/cost rollup summary)

## Debug-only local interfaces
- `GET /v1/debug/problem-create-template`
- `GET /v1/debug/problems?limit=...`
- `GET /v1/debug/problems/{problem_id}/snapshot?events_limit=...`
- `GET /v1/debug/problems/{problem_id}/artifacts?prefix=...&include_worker_jobs=...&limit=...`
- `GET /v1/debug/artifacts/{artifact_key:path}`
- `GET /v1/debug/problems/{problem_id}/request-log?limit=...&source=...`
- `DELETE /v1/debug/problems/{problem_id}`
- `POST /v1/debug/reset-local`
- `GET /debug` (local lifecycle website)

`/v1/debug/problems/{problem_id}/request-log` `source` values:
- `api_create` (problem create request)
- `api_run` (run tick request)
- `agent1`..`agent5` (agent input payloads)
- `lean` (Lean request payloads)

`/v1/debug/problems/{problem_id}/request-log` entries include completion metadata:
- `completion_status`: `completed | pending | failed | unknown`
- `completion_detail`: short diagnostic detail

`/progress` includes:
- active/standby decomposition ids and controller statuses
- lemma counters by proof/routing status
- Lean job counts by mode/status
- latest stage, latest reason, and latest blocking reason

## Error behavior
- Error envelope:
  - `request_id`
  - `server_time`
  - `error.code`
  - `error.message`
  - `error.details`
- `404`: unknown problem or artifact not present
- `409`: invalid state for requested operation (for example failure report requested before failure)
- `422`: request payload validation failure
