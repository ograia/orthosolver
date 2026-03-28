# Agent I/O Rules

## Global requirements
- Agents 1-5 must output valid JSON only (no markdown, no prose preamble).
- Prompt, injected input JSON, and raw output are persisted before parsing.
- JSON parse failure retries exactly once. Second parse failure is infrastructure-fatal.

## Agent surfaces
- Agent 1: root/lemma semantic sketch generator
- Agent 2: decomposition candidate generator
- Agent 3: decomposition vetter
- Agent 4: lemma solver
- Agent 5: lemma proof vetter

## Runtime defaults
- Temperature `0` for all agents except Agent 4 retries may use `0.2`.
- Model ids are configured in environment variables (`OPENAI_MODEL_AGENT1` ... `OPENAI_MODEL_AGENT5`).
