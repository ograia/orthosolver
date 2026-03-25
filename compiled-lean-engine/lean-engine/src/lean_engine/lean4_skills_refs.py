"""Read reference docs from the lean4-skills plugin for prompt injection.

The lean4-skills plugin ships ~35 Markdown reference docs covering Lean 4
tactics, error patterns, proof workflows, and Mathlib conventions.  This
module reads the 25 non-niche docs and concatenates them into a single
string suitable for injection into Claude prompts.

The 10 *niche* docs (FFI, custom syntax, linter authoring, metaprogramming,
profiling, review-hook schema, scaffold DSL, Verso docs, learn pathways,
and Mathlib style) are deliberately excluded — they add token cost without
helping the proof-compilation use case.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# ── The 25 reference docs to include (all except the 10 niche ones) ──────

_INCLUDED_REFS = (
    "compilation-errors.md",
    "tactics-reference.md",
    "lean-phrasebook.md",
    "sorry-filling.md",
    "compiler-guided-repair.md",
    "mathlib-guide.md",
    "lean-lsp-tools-api.md",
    "lean-lsp-server.md",
    "instance-pollution.md",
    "performance-optimization.md",
    "axiom-elimination.md",
    "calc-patterns.md",
    "measure-theory.md",
    "domain-patterns.md",
    "proof-refactoring.md",
    "grind-tactic.md",
    "simp-reference.md",
    "tactic-patterns.md",
    "proof-templates.md",
    "cycle-engine.md",
    "proof-golfing.md",
    "proof-golfing-patterns.md",
    "command-examples.md",
    "subagent-workflows.md",
    "agent-workflows.md",
)

# The 10 niche docs we skip (kept here for documentation only):
_SKIPPED_REFS = (
    "ffi-patterns.md",
    "lean4-custom-syntax.md",
    "linter-authoring.md",
    "metaprogramming-patterns.md",
    "profiling-workflows.md",
    "review-hook-schema.md",
    "scaffold-dsl.md",
    "verso-docs.md",
    "learn-pathways.md",
    "mathlib-style.md",
)


def _read_ref(refs_dir: Path, filename: str) -> str:
    """Read a single reference doc, returning empty string if missing."""
    path = refs_dir / filename
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        logger.warning("lean4-skills reference doc not found: %s", path)
        return ""


def get_all_proving_refs(lean4_skills_root: Path) -> str:
    """Read and concatenate all 25 CRITICAL+USEFUL reference docs.

    Returns ~32K tokens of Lean proving knowledge for prompt injection.
    Skips the 10 niche docs (FFI, custom syntax, linter, etc.).

    Parameters
    ----------
    lean4_skills_root:
        Path to the lean4-skills plugin root directory, e.g.
        ``../lean4-skills-main/plugins/lean4`` relative to the lean-engine
        project root.

    Returns
    -------
    str
        Concatenated reference material with per-file separators, or an
        empty string if the references directory does not exist or no docs
        could be read.
    """
    refs_dir = lean4_skills_root / "skills" / "lean4" / "references"
    if not refs_dir.is_dir():
        logger.warning(
            "lean4-skills references directory not found: %s", refs_dir
        )
        return ""

    parts: list[str] = []
    for filename in _INCLUDED_REFS:
        content = _read_ref(refs_dir, filename)
        if content.strip():
            # Add a clear separator with the filename
            parts.append(f"=== {filename} ===\n{content}")

    if not parts:
        return ""

    return (
        "# Lean 4 Proving Reference Library\n"
        "# The following reference material covers tactics, error fixes, "
        "patterns, and workflows.\n"
        "# Use this knowledge when writing and debugging Lean proofs.\n\n"
        + "\n\n".join(parts)
    )


# ── Compact subset for repair rounds ─────────────────────────────────────
# Only the docs most relevant when Claude has specific compiler errors to fix.
_COMPACT_REFS = (
    "compilation-errors.md",
    "compiler-guided-repair.md",
    "sorry-filling.md",
    "tactic-patterns.md",
)


def get_compact_proving_refs(lean4_skills_root: Path) -> str:
    """Read only the 4 most repair-relevant reference docs (~4K tokens).

    Used for repair rounds (round 2+) where the full 25-doc library is
    unnecessary — Claude already received it in round 1.
    """
    refs_dir = lean4_skills_root / "skills" / "lean4" / "references"
    if not refs_dir.is_dir():
        return ""

    parts: list[str] = []
    for filename in _COMPACT_REFS:
        content = _read_ref(refs_dir, filename)
        if content.strip():
            parts.append(f"=== {filename} ===\n{content}")

    if not parts:
        return ""

    return (
        "# Lean 4 Repair Reference (compact)\n"
        "# Error fixes and tactic patterns for this repair round.\n\n"
        + "\n\n".join(parts)
    )
