# Requirement Matrix

The current runtime requirements are captured directly by the markdown architecture docs and the test suite.

Core enforced requirements:

- root theorem is immutable after creation
- non-root obligations are lemmas
- accepted decompositions immediately trigger Lean `prepare_track` in standard mode
- standard-mode lemma formalization uses Lean v2 `formalize_lemma_from_nl`
- Lean results classify into `proof_issue` or `lean_issue`
- NL-only mode can terminate without Lean formalization
- routing caps come from config, not hardcoded controller branches
- all routing-critical state is persisted as first-class JSON fields

Primary references:

- `docs/overview.md`
- `docs/ARCHITECTURE_SUMMARY.md`
- `contracts/lean_engine/http_contract.md`
- `tests/unit/`
- `tests/integration/`
