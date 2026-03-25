from __future__ import annotations

import json
from dataclasses import dataclass

from .contracts import NormalizedProblemBundle


@dataclass(frozen=True)
class StatementPromptDeclNaming:
    root_decl_name: str
    lemma_decl_names: dict[str, str]


def build_statement_translation_prompt(
    bundle: NormalizedProblemBundle,
    decl_naming: StatementPromptDeclNaming,
    lean4_skills_refs: str = "",
) -> str:
    payload = _statement_payload(bundle, decl_naming)

    lines = [
        "You are generating Lean declarations (Phase 03).",
        "",
        "HARD RULE: STATEMENTS ONLY. DO NOT ATTEMPT OR SOLVE PROOFS.",
        "HARD RULE: Do not use `sorry`, `admit`, `constant`, or placeholder theorem injections.",
        "HARD RULE: declaration names must match the provided deterministic names exactly.",
        "",
        "Required outputs:",
        "1) root statement declaration header",
        "2) lemma statement declaration headers",
        "3) no proof bodies",
        "4) prefer `axiom` declarations in this phase so statements compile without proof terms",
        "",
        "Semantic coverage rules:",
        "- The Lean statement MUST capture ALL aspects of the NL statement.",
        "- If the NL statement says 'equivalently' or gives multiple characterizations,",
        "  include both directions (use `↔` or state both implications).",
        "- If the NL statement quantifies over a specific domain (e.g., 'for each i in {0,...,m}'),",
        "  the Lean formalization must reflect that exact domain.",
        "- DO NOT simplify or weaken the NL statement — formalize it fully.",
        "- Prefer the strongest statement that is faithful to the NL source.",
    ]

    if lean4_skills_refs:
        lines.extend([
            "",
            "Lean formalization reference (use these patterns when translating mathematical English to Lean):",
            lean4_skills_refs,
        ])

    lines.extend([
        "",
        "Compilation checking:",
        "- After writing Orthos/Statements.lean, call lean_diagnostic_messages to check",
        "  that the file compiles. Fix any errors before finishing.",
        "- Do NOT run `lake env lean`, `lake build`, or any Lean compilation command via Bash.",
        "  Use lean_diagnostic_messages instead — it keeps Mathlib in memory and is fast.",
        "- Do NOT use `sleep` or polling loops. MCP tools return when ready.",
        "",
        "File contract:",
        "- Return a COMPLETE replacement for `Orthos/Statements.lean`.",
        "- Output Lean code only. No markdown fences. No commentary.",
        "- If you use tools to edit files, keep the final response minimal and do not summarize.",
        "- Do NOT wrap declarations in any namespace.",
        "- Do NOT modify any other workspace files (especially Orthos/Lemmas.lean or Orthos/Root.lean).",
        "- Prefer explicit binders and assumptions.",
        "- If ambiguity remains, add a Lean comment beginning with `-- AMBIGUITY:`.",
        "",
        "Declaration naming:",
        f"- root: {decl_naming.root_decl_name}",
    ])

    for lemma_id in sorted(decl_naming.lemma_decl_names):
        lines.append(f"- lemma {lemma_id}: {decl_naming.lemma_decl_names[lemma_id]}")

    lines.extend(
        [
            "",
            "Input bundle fields (statement_nl + semantic_sketch are mandatory context):",
            json.dumps(payload, indent=2, ensure_ascii=True, sort_keys=True),
        ]
    )
    return "\n".join(lines).strip() + "\n"


def build_statement_repair_prompt(
    bundle: NormalizedProblemBundle,
    decl_naming: StatementPromptDeclNaming,
    *,
    current_statements_text: str,
    diagnostics_text: str,
    repair_round: int,
    lean4_skills_refs: str = "",
) -> str:
    payload = _statement_payload(bundle, decl_naming)
    lines = [
        "You are repairing Phase 03 statement declarations.",
        "",
        "HARD RULE: STATEMENTS ONLY. DO NOT ATTEMPT OR SOLVE PROOFS.",
        "HARD RULE: Keep declaration names exactly unchanged.",
        "HARD RULE: Do not use `sorry`, `admit`, `constant`, or placeholders.",
        "HARD RULE: Keep declaration headers as statements (prefer `axiom` declarations in this phase).",
        "",
        f"Repair round: {repair_round}",
        "",
        "Compilation checking:",
        "- After fixing Orthos/Statements.lean, call lean_diagnostic_messages to verify",
        "  the file compiles. Keep fixing until there are no errors.",
        "- Do NOT run `lake env lean` or `lake build` via Bash.",
        "  Use lean_diagnostic_messages instead — it keeps Mathlib in memory and is fast.",
        "- Do NOT use `sleep` or polling loops. MCP tools return when ready.",
        "",
        "Compiler diagnostics:",
        diagnostics_text.strip() or "<empty diagnostics>",
    ]

    if lean4_skills_refs:
        lines.extend([
            "",
            "Lean reference material (error fixes, tactics, patterns):",
            lean4_skills_refs,
        ])

    lines.extend([
        "",
        "Current `Orthos/Statements.lean` candidate:",
        current_statements_text.rstrip(),
        "",
        "Expected input contract:",
        json.dumps(payload, indent=2, ensure_ascii=True, sort_keys=True),
        "",
        "Return a COMPLETE replacement for `Orthos/Statements.lean`.",
        "Output Lean code only.",
        "If you used tools to update files, do not return prose summaries.",
    ])
    return "\n".join(lines).strip() + "\n"


def build_assembly_repair_prompt(
    bundle: NormalizedProblemBundle,
    decl_naming: StatementPromptDeclNaming,
    *,
    current_statements_text: str,
    assembly_diagnostics_text: str,
    repair_round: int,
    lean4_skills_refs: str = "",
) -> str:
    lines = [
        "You are repairing `Orthos/Statements.lean` so the assembly precheck passes.",
        "",
        "HARD RULE: STATEMENTS ONLY. DO NOT ATTEMPT OR SOLVE PROOFS.",
        "HARD RULE: Keep all declaration names exactly unchanged.",
        "HARD RULE: Do not use `sorry`, `admit`, `constant`, or placeholders.",
        "",
        f"Assembly repair round: {repair_round}",
        "",
        "The declarations in `Orthos/Statements.lean` compile successfully on their own.",
        "However, the assembly precheck file (`Orthos/AssemblyCheck.lean`) failed.",
        "That file builds a composition skeleton using the declared signatures.",
        "Your job: fix signatures in `Orthos/Statements.lean` so the skeleton typechecks.",
        "",
        "Common causes and fixes:",
        "- Root theorem uses implicit parameters (e.g. `{m : ℕ}`) that Lean cannot infer",
        "  in the skeleton context — change them to explicit (`(m : ℕ)`).",
        "- A lemma uses an implicit type variable not inferrable from the skeleton —",
        "  make it explicit.",
        "",
        "Assembly precheck errors:",
        assembly_diagnostics_text.strip() or "<empty diagnostics>",
    ]

    if lean4_skills_refs:
        lines.extend([
            "",
            "Lean reference material (error fixes, tactics, patterns):",
            lean4_skills_refs,
        ])

    lines.extend([
        "",
        "Current `Orthos/Statements.lean`:",
        current_statements_text.rstrip(),
        "",
        "Checking tools:",
        "- After editing `Orthos/Statements.lean`, call lean_diagnostic_messages to verify",
        "  the file still compiles clean (no errors).",
        "- Do NOT run `lake env lean` or `lake build` via Bash.",
        "- Do NOT use `sleep` or polling loops. MCP tools return when ready.",
        "",
        "Return a COMPLETE replacement for `Orthos/Statements.lean`.",
        "Output Lean code only.",
        "If you used tools to update files, do not return prose summaries.",
    ])
    return "\n".join(lines).strip() + "\n"


def _statement_payload(
    bundle: NormalizedProblemBundle,
    decl_naming: StatementPromptDeclNaming,
) -> dict[str, object]:
    return {
        "problem_id": bundle.problem_id,
        "title": bundle.title,
        "root_theorem": {
            "decl_name": decl_naming.root_decl_name,
            "statement_nl": bundle.root_theorem.statement_nl,
            "semantic_sketch": bundle.root_theorem.semantic_sketch,
        },
        "lemmas": [
            {
                "lemma_id": lemma.lemma_id,
                "decl_name": decl_naming.lemma_decl_names[lemma.lemma_id],
                "statement_nl": lemma.statement_nl,
                "semantic_sketch": lemma.semantic_sketch,
            }
            for lemma in bundle.lemmas
        ],
        "assembly_plan": {
            "proof_skeleton_nl": bundle.selected_decomposition.assembly_plan.proof_skeleton_nl,
            "steps": [
                {
                    "step_id": step.step_id,
                    "uses_lemmas": list(step.uses_lemmas),
                    "uses_prior_steps": list(step.uses_prior_steps),
                    "derives": step.derives,
                }
                for step in bundle.selected_decomposition.assembly_plan.steps
            ],
        },
    }
