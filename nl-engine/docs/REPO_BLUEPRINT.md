# Repository Blueprint

This document is now a thin index. The active implementation guidance lives in:

- `README.md`
- `docs/overview.md`
- `docs/ARCHITECTURE_SUMMARY.md`
- `docs/RUNBOOK.md`
- `docs/OPERATIONS.md`

Current high-level layout:

- `src/nl_engine/`: production code
- `tests/`: unit and integration tests
- `mock_lean/`: local Lean mock service
- `contracts/`: public-facing schemas and boundary docs
- `docs/`: architecture, runbook, and operations docs
- `infra/`: deployment baselines

There is no active SQL schema or migration directory in this repository.
