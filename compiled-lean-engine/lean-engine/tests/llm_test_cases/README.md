# LLM Test Cases

Hard lemmas that Claude failed to prove in pipeline runs.
Use these to benchmark other LLMs on Lean formalization.

Each subdirectory contains:
- `prompt.md` — Full prompt to give the LLM (self-contained)
- `nl_proof.md` — The natural language proof
- `pinned_signature.lean` — The exact Lean statement the LLM must prove
- `scratch_file.lean` — The scratch file (starting point with imports + trusted context)
- `best_attempt.lean` — Claude's best partial attempt (for reference)
- `notes.md` — What went wrong and what the remaining sorries need
