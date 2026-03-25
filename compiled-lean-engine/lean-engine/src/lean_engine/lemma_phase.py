from __future__ import annotations

import difflib
import hashlib
import json
import logging
import os
import re
import shutil
import stat
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

from .artifact_io import RunPaths, sanitize_component, write_json, write_text
from .claude_candidate_selection import select_lean_candidate
from .claude_runner import (
    ClaudeRunResult,
    ClaudeRunner,
    extract_search_discoveries,
    format_search_discoveries,
)
from .config import RuntimeConfig
from .contracts import NormalizedLemma
from .lean_checks import LeanCommandResult, check_lean_file
from .statement_phase import (
    extract_statement_signature,
    normalize_lean_text,
    normalize_signature_text,
    sanitize_lean_decl_suffix,
)

SubprocessRunner = Callable[..., subprocess.CompletedProcess[str]]

DECL_PATTERN = re.compile(
    r"(?m)^\s*(?:private\s+|protected\s+)?(?:noncomputable\s+)?"
    r"(theorem|lemma|def|abbrev|axiom|example)\s+([A-Za-z0-9_'.]+)\b"
)
SIGNATURE_PREFIX_PATTERN = re.compile(r"^(theorem|lemma)\s+([A-Za-z0-9_'.]+)\b")
ORTHOS_END_PATTERN = re.compile(r"^\s*end\s+Orthos(?:\.\w+)*\s*$")
_LEMMA_END_MARKER = "-- END LEMMAS"
MAJOR_GAP_PATTERN = re.compile(r"(major proof gap|missing mathematical step|cannot be justified)", re.IGNORECASE)

_log = logging.getLogger(__name__)


class SharedTrustedContext:
    """Thread-safe growing trusted context for parallel lemma proving.

    When multiple lemma workers run concurrently, they share a single
    ``SharedTrustedContext``.  Each worker takes a snapshot at the start of
    each repair round (so it sees proofs completed by other workers since its
    last round) and commits successful proofs under the merge lock.
    """

    def __init__(self, initial: list[TrustedContextEntry] | None = None) -> None:
        self._lock = threading.Lock()
        self._merge_lock = threading.Lock()
        self._entries: list[TrustedContextEntry] = list(initial or [])
        self._completed: set[str] = set()
        self._fatal_abort = threading.Event()
        self._fatal_reason: str | None = None

    def snapshot(self) -> list[TrustedContextEntry]:
        with self._lock:
            return list(self._entries)

    def add(self, entry: TrustedContextEntry) -> None:
        with self._lock:
            self._entries.append(entry)
            self._completed.add(entry.lemma_id)

    def is_completed(self, lemma_id: str) -> bool:
        with self._lock:
            return lemma_id in self._completed

    def signal_fatal(self, reason: str) -> None:
        """Signal all workers to abort (confirmed mathematical flaw)."""
        self._fatal_reason = reason
        self._fatal_abort.set()

    @property
    def should_abort(self) -> bool:
        return self._fatal_abort.is_set()

    @property
    def fatal_reason(self) -> str | None:
        return self._fatal_reason

    @property
    def merge_lock(self) -> threading.Lock:
        return self._merge_lock


@dataclass(frozen=True)
class PinnedLemmaSignature:
    lemma_id: str
    decl_name: str
    signature: str
    statement_nl: str

    def to_dict(self) -> dict[str, str]:
        return {
            "lemma_id": self.lemma_id,
            "decl_name": self.decl_name,
            "signature": self.signature,
            "statement_nl": self.statement_nl,
        }


@dataclass(frozen=True)
class TrustedContextEntry:
    lemma_id: str
    decl_name: str
    status: str
    source_file: str
    signature: str
    declaration: str

    def to_dict(self) -> dict[str, str]:
        return {
            "lemma_id": self.lemma_id,
            "decl_name": self.decl_name,
            "status": self.status,
            "source_file": self.source_file,
            "signature": self.signature,
            "declaration": self.declaration,
        }


@dataclass(frozen=True)
class LemmaAttemptResult:
    round_index: int
    scratch_path: Path
    prompt_path: Path
    claude_raw_path: Path
    diagnostics_path: Path
    diff_path: Path
    status: str
    classification: str | None
    diagnostics: tuple[str, ...]
    check_result: dict[str, Any] | None
    progress_guard_error: str | None
    used_mock_candidate: bool
    candidate_backend: str
    candidate_source: str | None = None
    candidate_selection_reasons: tuple[str, ...] = ()
    candidate_lean_score: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "round_index": self.round_index,
            "scratch_path": str(self.scratch_path),
            "prompt_path": str(self.prompt_path),
            "claude_raw_path": str(self.claude_raw_path),
            "diagnostics_path": str(self.diagnostics_path),
            "diff_path": str(self.diff_path),
            "status": self.status,
            "classification": self.classification,
            "diagnostics": list(self.diagnostics),
            "check_result": self.check_result,
            "progress_guard_error": self.progress_guard_error,
            "used_mock_candidate": self.used_mock_candidate,
            "candidate_backend": self.candidate_backend,
            "candidate_source": self.candidate_source,
            "candidate_selection_reasons": list(self.candidate_selection_reasons),
            "candidate_lean_score": self.candidate_lean_score,
        }


@dataclass(frozen=True)
class LemmaFormalizationResult:
    lemma_id: str
    decl_name: str
    status: str
    attempts_used: int
    lemma_artifact_dir: Path
    scratch_file: Path
    result_path: Path
    error_class: str | None = None
    message: str | None = None
    diagnostics: tuple[str, ...] = ()
    attempts: tuple[LemmaAttemptResult, ...] = ()
    final_success_path: Path | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "lemma_id": self.lemma_id,
            "decl_name": self.decl_name,
            "status": self.status,
            "attempts_used": self.attempts_used,
            "lemma_artifact_dir": str(self.lemma_artifact_dir),
            "scratch_file": str(self.scratch_file),
            "result_path": str(self.result_path),
            "error_class": self.error_class,
            "message": self.message,
            "diagnostics": list(self.diagnostics),
            "attempts": [attempt.to_dict() for attempt in self.attempts],
            "final_success_path": str(self.final_success_path) if self.final_success_path else None,
        }


@dataclass
class RoundMetrics:
    """Per-round structured metrics for observability."""
    lemma_id: str
    round_index: int
    model: str
    effort: str
    round_start_ts: float
    round_end_ts: float = 0.0
    wall_seconds: float = 0.0
    diagnostic_status: str = "unknown"
    false_negative: bool = False
    stall_killed: bool = False
    max_tokens_hit: bool = False
    scratch_hash_before: str = ""
    scratch_hash_after: str = ""
    candidate_source: str = "none"
    progress_made: bool = False
    free_retry_used: bool = False
    error_count_before: int = 0
    error_count_after: int = 0

    def finalize(self) -> None:
        self.round_end_ts = time.time()
        self.wall_seconds = self.round_end_ts - self.round_start_ts
        self.progress_made = self.scratch_hash_before != self.scratch_hash_after

    def to_dict(self) -> dict[str, Any]:
        return {
            "lemma_id": self.lemma_id,
            "round_index": self.round_index,
            "model": self.model,
            "effort": self.effort,
            "round_start_ts": self.round_start_ts,
            "round_end_ts": self.round_end_ts,
            "wall_seconds": round(self.wall_seconds, 2),
            "diagnostic_status": self.diagnostic_status,
            "false_negative": self.false_negative,
            "stall_killed": self.stall_killed,
            "max_tokens_hit": self.max_tokens_hit,
            "scratch_hash_before": self.scratch_hash_before,
            "scratch_hash_after": self.scratch_hash_after,
            "candidate_source": self.candidate_source,
            "progress_made": self.progress_made,
            "free_retry_used": self.free_retry_used,
            "error_count_before": self.error_count_before,
            "error_count_after": self.error_count_after,
        }


def _content_hash(text: str) -> str:
    """Short SHA256 hash for scratch file change detection."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _append_round_metrics(run_root: Path, metrics: RoundMetrics) -> None:
    """Append a round metrics record to diagnostics/round_metrics.jsonl."""
    metrics_path = run_root / "diagnostics" / "round_metrics.jsonl"
    metrics_path.parent.mkdir(parents=True, exist_ok=True)
    with open(metrics_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(metrics.to_dict(), ensure_ascii=True) + "\n")


def load_pinned_lemma_signatures(
    path: Path,
    *,
    expected_problem_id: str | None = None,
    expected_run_name: str | None = None,
) -> dict[str, PinnedLemmaSignature]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    expected_problem = sanitize_component(expected_problem_id) if expected_problem_id else None
    if expected_problem is not None:
        payload_problem_id = str(payload.get("problem_id", "")).strip()
        if not payload_problem_id:
            raise ValueError("pinned_signatures.json is missing top-level `problem_id` binding")
        payload_problem = sanitize_component(payload_problem_id)
        if payload_problem != expected_problem:
            raise ValueError(
                "pinned_signatures.json problem_id mismatch: "
                f"expected `{expected_problem}`, found `{payload_problem}`"
            )

    if expected_run_name is not None:
        payload_run_name = str(payload.get("run_name", "")).strip()
        if not payload_run_name:
            raise ValueError("pinned_signatures.json is missing top-level `run_name` binding")
        if payload_run_name != expected_run_name:
            raise ValueError(
                "pinned_signatures.json run_name mismatch: "
                f"expected `{expected_run_name}`, found `{payload_run_name}`"
            )

    raw_lemmas = payload.get("lemmas")
    if not isinstance(raw_lemmas, list):
        raise ValueError("pinned_signatures.json is missing `lemmas` list")

    signatures: dict[str, PinnedLemmaSignature] = {}
    for item in raw_lemmas:
        if not isinstance(item, dict):
            continue
        lemma_id = str(item.get("lemma_id", "")).strip()
        decl_name = str(item.get("decl_name", "")).strip()
        signature = normalize_signature_text(str(item.get("signature", "")))
        keyword = str(item.get("keyword", "")).strip().lower()
        statement_nl = str(item.get("statement_nl", "")).strip()
        if not lemma_id or not decl_name or not signature:
            continue
        if keyword and keyword not in {"theorem", "lemma"}:
            raise ValueError(
                f"pinned lemma `{lemma_id}` must use `theorem`/`lemma`, found keyword `{keyword}`"
            )
        signature_match = SIGNATURE_PREFIX_PATTERN.match(signature)
        if signature_match is None:
            raise ValueError(
                f"pinned lemma `{lemma_id}` must use a `theorem`/`lemma` signature: `{signature}`"
            )
        signature_keyword = signature_match.group(1)
        signature_decl_name = signature_match.group(2)
        if keyword and keyword != signature_keyword:
            raise ValueError(
                f"pinned lemma `{lemma_id}` keyword/signature mismatch: "
                f"keyword `{keyword}` but signature starts with `{signature_keyword}`"
            )
        if signature_decl_name != decl_name:
            raise ValueError(
                f"pinned lemma `{lemma_id}` decl_name/signature mismatch: "
                f"decl_name `{decl_name}` but signature targets `{signature_decl_name}`"
            )
        if lemma_id in signatures:
            raise ValueError(f"pinned_signatures.json contains duplicate lemma_id `{lemma_id}`")
        signatures[lemma_id] = PinnedLemmaSignature(
            lemma_id=lemma_id,
            decl_name=decl_name,
            signature=signature,
            statement_nl=statement_nl,
        )
    if not signatures:
        raise ValueError("pinned_signatures.json does not contain any usable lemma signatures")
    return signatures


def load_trusted_manifest(path: Path, *, expected_problem_id: str | None = None) -> list[TrustedContextEntry]:
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    manifest_problem_id = str(payload.get("problem_id", "")).strip()
    expected = sanitize_component(expected_problem_id) if expected_problem_id else None
    if expected is not None:
        if not manifest_problem_id:
            raise ValueError("trusted_context_manifest.json is missing top-level `problem_id` binding")
        manifest_sanitized = sanitize_component(manifest_problem_id)
        if manifest_sanitized != expected:
            raise ValueError(
                "trusted_context_manifest.json problem_id mismatch: "
                f"expected `{expected}`, found `{manifest_sanitized}`"
            )
    raw_entries = payload.get("entries")
    if not isinstance(raw_entries, list):
        return []
    entries: list[TrustedContextEntry] = []
    for item in raw_entries:
        if not isinstance(item, dict):
            continue
        lemma_id = str(item.get("lemma_id", "")).strip()
        decl_name = str(item.get("decl_name", "")).strip()
        status = str(item.get("status", "")).strip() or "compiled"
        source_file = str(item.get("source_file", "")).strip() or "Orthos/Lemmas.lean"
        signature = str(item.get("signature", "")).strip()
        declaration = str(item.get("declaration", ""))
        if not lemma_id or not decl_name:
            continue
        entries.append(
            TrustedContextEntry(
                lemma_id=lemma_id,
                decl_name=decl_name,
                status=status,
                source_file=source_file,
                signature=signature,
                declaration=declaration,
            )
        )
    return entries


def write_trusted_manifest(path: Path, problem_id: str, entries: list[TrustedContextEntry]) -> Path:
    payload = {
        "problem_id": problem_id,
        "entry_count": len(entries),
        "entries": [entry.to_dict() for entry in entries],
    }
    return write_json(path, payload)


def _extract_open_lines(statements_path: Path | None) -> list[str]:
    """Extract ``open`` declarations from Statements.lean.

    In Lean 4, ``open`` does not propagate through ``import``, so scratch files
    that ``import Orthos.Statements`` need their own ``open`` lines to resolve
    names like ``Icc``, ``Tendsto``, ``nhdsWithin``, etc.
    """
    if statements_path is None or not statements_path.exists():
        return []
    try:
        text = statements_path.read_text(encoding="utf-8")
    except OSError:
        return []
    opens: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("open "):
            opens.append(stripped)
    return opens


def build_lemma_scratch_text(
    *,
    lemma_id: str,
    pinned_signature: str,
    trusted_entries: list[TrustedContextEntry] | None = None,
    statements_path: Path | None = None,
) -> str:
    sanitized = sanitize_lean_decl_suffix(lemma_id)
    lines = [
        "import Mathlib",
        "import Orthos.Statements",
        "",
    ]
    # Replicate open declarations from Statements.lean so that unqualified
    # names (Icc, Tendsto, nhdsWithin, etc.) resolve in the scratch file.
    open_lines = _extract_open_lines(statements_path)
    if open_lines:
        lines.extend(open_lines)
        lines.append("")
    # Include sorry-stubbed signatures for previously-proven lemmas so that
    # Lean elaborates the target proof in a context matching Combined.lean
    # (all declarations in one flat namespace).
    if trusted_entries:
        lines.append("-- Trusted context: previously-proven lemma signatures")
        for entry in trusted_entries:
            lines.append(f"{entry.signature} := by sorry")
        lines.append("")
    lines.extend([
        f"-- Phase 04 scratch file for {lemma_id}.",
        f"-- edit_scope: declaration target_{sanitized}",
        "",
        f"{pinned_signature} := by",
        "  -- Replace this placeholder proof.",
        "  sorry",
        "",
    ])
    return "\n".join(lines)


def build_lemma_formalization_prompt(
    *,
    lemma: NormalizedLemma,
    pinned: PinnedLemmaSignature,
    trusted_entries: list[TrustedContextEntry],
    scratch_relative_path: str,
    repair_round: int,
    diagnostics_text: str | None = None,
    previous_candidate: str | None = None,
    search_discoveries: str | None = None,
    false_negative_warning: bool = False,
    previous_sorry_count: int | None = None,
    best_partial_candidate: str | None = None,
    best_partial_sorry_count: int | None = None,
    lean4_skills_refs: str = "",
) -> str:
    trusted_payload = [
        {
            "lemma_id": entry.lemma_id,
            "decl_name": entry.decl_name,
            "signature": entry.signature,
        }
        for entry in trusted_entries
    ]
    lines = [
        "You are formalizing one pinned Lean lemma (Phase 04).",
        "",
        "Hard rules:",
        "- Keep the target declaration header EXACTLY equal to the pinned signature.",
        "- Do not edit pinned signatures or trusted declarations.",
        "- You may freely adjust imports (add/remove `import` lines) to fix compilation.",
        "- Use `lean_diagnostic_messages` via MCP to check compilation. This is fast (~1-5s).",
        "- The final proof MUST NOT contain `sorry` or `admit`.",
        "  During development you MAY use `sorry` as a temporary sub-goal placeholder",
        "  while building the proof incrementally. The engine accepts partial progress",
        "  (compiling proof with fewer sorries than before) and preserves it across rounds.",
        "- Never use `admit`, fake axioms, or hidden theorem injection.",
        "- If there is a major mathematical gap, report it plainly instead of faking progress.",
        "- Do NOT add any `namespace` declarations.",
        "  Do NOT rename the target theorem. Keep the exact pinned signature.",
        "",
        "HELPER LEMMAS — actively encouraged:",
        "- You MAY (and should, when useful) define private helper lemmas in the scratch file",
        "  to break the proof into manageable sub-goals.",
        "- Define helpers BEFORE the target theorem so the target can reference them.",
        "- Helpers are automatically extracted and included in the final output — they will",
        "  be available in the final Combined.lean.",
        "- IMPORTANT: use fully-qualified Mathlib names in helpers (e.g., `Real.sin`, `Real.pi`,",
        "  `Real.pi_gt_three`). The open directives in the scratch file (e.g., `open Real`) are",
        "  carried over but relying solely on them can cause issues. Prefer explicit qualification.",
        "- Do NOT define helpers AFTER the target — they cannot be used by the target.",
        "",
        "PROHIBITED ACTIONS (violating these wastes time and breaks the pipeline):",
        "- Do NOT run `lake env lean`, `lake build`, or any Lean compilation command via Bash.",
        "  Each invocation reloads Mathlib from scratch (~30s) and saturates CPU.",
        "  The engine runs its own `lake env lean` verification after you finish — your job is",
        "  to use `lean_diagnostic_messages` via MCP for fast iterative checking.",
        "- Do NOT run `lean_build` via MCP. It sees scratch files from parallel workers",
        "  and fails with 'already declared' errors.",
        "- Do NOT use `sleep` or polling loops — MCP tools return when ready.",
        "- Do NOT debug infrastructure issues (inspecting processes, memory, olean files).",
        "",
        "Proof strategy:",
        "- The proof must faithfully follow the NL proof strategy provided below.",
        "- If the NL proof references a specific technique (e.g., induction, floor division",
        "  properties, digit extraction), use the corresponding Lean/Mathlib formalization.",
        "- If a direct `rw` or `simp` chain doesn't work, try `omega`, `norm_num`, `decide`,",
        "  or break the proof into intermediate `have` steps.",
    ]
    if lean4_skills_refs:
        lines.extend([
            "",
            "Lean 4 proving reference library (tactics, error fixes, patterns, search strategies):",
            lean4_skills_refs,
        ])
    lines.extend([
        "",
        "For complex proofs, use an INCREMENTAL strategy:",
        "- Write a proof SKELETON first using `sorry` for sub-goals you haven't solved.",
        f"- Verify the skeleton compiles with `lean_diagnostic_messages(\"{scratch_relative_path}\")`.",
        "- Then fill in each `sorry` one at a time, re-checking compilation after each.",
        "- A compiling proof with 2 sorries is BETTER than no output at all.",
        "  The engine preserves partial progress across rounds.",
        "",
        "When stuck on a sorry, use `lean_run_code` via MCP to test proof fragments for individual",
        "sub-goals in isolation. This lets you verify a tactic works before inserting it into the",
        "full proof. Example: if a goal is `⊢ 2 * 1012 = 2025 - 1`, test",
        "`example : 2 * 1012 = 2025 - 1 := by omega` via `lean_run_code` first.",
        "Also use `lean_run_code` to search for the right Mathlib lemma name — write a small",
        "`#check @LemmaName` snippet and see if it compiles.",
        "",
        "Workflow (FOLLOW EXACTLY — write first, search only on failure):",
        "1. Read the scratch file. Understand the goal.",
        "2. Write the complete proof using Mathlib lemmas you know from training.",
        "   Most standard Mathlib lemmas (Nat.gcd_dvd_left, Nat.Coprime.dvd_of_dvd_mul_left,",
        "   Finset.prod_range_succ, etc.) are well-known — just use them directly.",
        f"3. Run `lean_diagnostic_messages(\"{scratch_relative_path}\")` via MCP to check compilation.",
        "   '[STATUS: clean]' = success. Any errors = fix them.",
        "4. If compilation fails with 'unknown identifier' or type mismatch on a specific",
        "   Mathlib lemma name, FIRST try lean_local_search (fast, local ripgrep — zero latency),",
        "   THEN loogle/leandex/leanfinder for external API search if local search fails.",
        "   Call ALL needed searches in ONE message (parallel tool calls).",
        f"5. Fix the proof and run `lean_diagnostic_messages(\"{scratch_relative_path}\")` again.",
        "6. Once diagnostics are clean, stop immediately.",
        "- Do NOT do upfront bulk searching before writing the proof.",
        "- PREFER lean_local_search over external search APIs — it's instant with no rate limits.",
        "- Use `lean_diagnostic_messages` for ALL compilation checking. Do NOT use Bash.",
        "  MCP tools (lean_local_search, lean_hover_info, lean_goal, loogle, etc.) remain",
        "  available for proof search and goal inspection.",
        "",
        "Search protocol (FOLLOW THIS ORDER):",
        "1. lean_local_search — instant, no rate limits, always try first",
        "2. lean_leanfinder — semantic + goal-aware, up to 3 calls per round",
        "3. lean_loogle — type-pattern search (\"?a → ?b → _\"), unlimited",
        "4. lean_leansearch — natural language, 3 calls per 30s",
        "",
        "When you have 2-3 candidate tactics for a sorry, test them ALL at once using",
        "lean_multi_attempt(file, line, snippets=[\"tactic1\", \"tactic2\", \"tactic3\"]).",
        "This tests all candidates in parallel and returns which ones pass.",
        "Pick the shortest passing candidate.",
        "",
        f"Target scratch file: `{scratch_relative_path}`",
        f"Repair round: {repair_round}",
        "",
        "Pinned signature (CRITICAL — your theorem declaration MUST start with this EXACT text,",
        "character-for-character. Do NOT reformat, reorder, or expand `let` bindings):",
        "```",
        pinned.signature,
        "```",
        "",
        "Lemma context from NL engine:",
        f"- lemma_id: {lemma.lemma_id}",
        f"- statement_nl: {lemma.statement_nl}",
        f"- semantic_sketch: {json.dumps(lemma.semantic_sketch, ensure_ascii=True, sort_keys=True)}",
        f"- proof_nl: {lemma.proof_nl}",
        "",
        "Trusted context declarations (already compiler-accepted):",
        json.dumps(trusted_payload, ensure_ascii=True, sort_keys=True),
    ])
    if repair_round > 1 and previous_candidate:
        lines.extend([
            "",
            f"LATEST DRAFT FROM ROUND {repair_round - 1} (this is what is currently in the scratch file).",
            "Start from this draft and make targeted fixes instead of restarting from scratch.",
            "```lean",
            previous_candidate.rstrip(),
            "```",
        ])
    if repair_round > 1 and best_partial_candidate and best_partial_candidate != previous_candidate:
        lines.extend([
            "",
            f"OLDER COMPILING REFERENCE (best partial so far; compiles with {best_partial_sorry_count} sorry placeholder(s)).",
            "This is for reference only. It is NOT the current scratch file unless it matches the latest draft above.",
            "```lean",
            best_partial_candidate.rstrip(),
            "```",
        ])
    if repair_round > 1:
        lines.extend([
            "",
            "IMPORTANT: This is a REPAIR round. You have specific error diagnostics below.",
            "Focus on fixing these exact errors — do not re-derive the entire proof strategy.",
            f"Make targeted changes and verify compilation immediately with `lean_diagnostic_messages(\"{scratch_relative_path}\")`.",
        ])
    if false_negative_warning and diagnostics_text:
        lines.extend([
            "",
            "WARNING: The previous round's compilation check missed errors.",
            "The errors shown below were found by post-verification. Trust these errors.",
        ])
    if diagnostics_text:
        lines.extend(
            [
                "",
                "Latest diagnostics to repair:",
                diagnostics_text.rstrip(),
                "",
                "Fix the issues above. Do NOT repeat the same approach if it already failed.",
                "If the error mentions 'pinned signature' drift, copy the EXACT pinned signature above.",
                "If the error is about missing definitions, check Orthos/Statements.lean for them.",
                "If a tactic is failing, try a different proof strategy or break into smaller steps.",
            ]
        )
    if search_discoveries:
        lines.extend(
            [
                "",
                search_discoveries,
            ]
        )
    if repair_round > 1:
        lines.extend(
            [
                "",
                "Make targeted edits to the scratch file to fix the errors above.",
                "Do NOT rewrite the file from scratch — preserve working parts and only change what is broken.",
                "Output Lean code only; no markdown fences and no commentary.",
            ]
        )
    else:
        lines.extend(
            [
                "",
                "Return a COMPLETE replacement for the scratch Lean file.",
                "Output Lean code only; no markdown fences and no commentary.",
            ]
        )
    return "\n".join(lines).strip() + "\n"


def _select_model_and_effort(
    round_index: int,
    max_attempts: int,
    runtime_config: RuntimeConfig,
    model_override: str | None,
    has_prior_candidate: bool,
    has_concrete_errors: bool,
) -> tuple[str, str, dict[str, str]]:
    """Pick model, effort level, and extra env vars for a lemma round.

    Returns (model, effort_label, extra_env).
    """
    primary = model_override or runtime_config.claude.model
    # Use primary model (Opus) for all rounds with no thinking token caps.
    # Opus needs full thinking budget to solve hard Lean proofs.
    return primary, "high", {}


def _round_stall_timeout(round_index: int, default_stall: int) -> int:
    """Stall timeout in seconds for a lemma round."""
    _ = round_index
    return default_stall


# Free retry budget caps per failure class.
_MAX_FREE_RETRIES_TOTAL = 2
_MAX_FREE_RETRIES_STALL = 2
_MAX_FREE_RETRIES_API = 1
_MAX_FREE_RETRIES_MAX_TOKENS = 1


def _is_concrete_error(diagnostics_text: str) -> bool:
    """Check if diagnostics contain concrete compiler errors (tactic/type/syntax)."""
    if not diagnostics_text:
        return False
    concrete_patterns = (
        "tactic", "type mismatch", "unknown identifier", "unknown constant",
        "unsolved goals", "parse error", "unexpected token", "expected token",
        "application type mismatch", "not found in environment",
    )
    text_lower = diagnostics_text.lower()
    return any(pat in text_lower for pat in concrete_patterns)


def run_lemma_formalization(
    *,
    run_paths: RunPaths,
    runtime_config: RuntimeConfig,
    lemma: NormalizedLemma,
    pinned: PinnedLemmaSignature,
    trusted_entries: list[TrustedContextEntry],
    manifest_path: Path,
    manifest_problem_id: str,
    max_attempts: int,
    timeout_seconds: int,
    lean_check_timeout_seconds: int | None = None,
    model: str | None,
    claude_runner: ClaudeRunner | None = None,
    runner: SubprocessRunner = subprocess.run,
    mock_candidates: list[str] | None = None,
    commit_on_success: bool = True,
    shared_context: SharedTrustedContext | None = None,
    lean4_skills_refs: str = "",
    lean4_skills_refs_compact: str = "",
) -> LemmaFormalizationResult:
    if max_attempts <= 0:
        raise ValueError("max_attempts must be > 0")

    lemma_path_token = lemma_id_to_path_token(lemma.lemma_id)
    workspace_relative_scratch = f"Orthos/Scratch_{lemma_path_token}.lean"
    workspace_scratch_path = run_paths.workspace_dir / workspace_relative_scratch
    lemmas_file_path = run_paths.workspace_dir / "Orthos" / "Lemmas.lean"

    lemma_artifact_dir = run_paths.run_root / "lemmas" / lemma_path_token
    lemma_artifact_dir.mkdir(parents=True, exist_ok=True)
    result_path = lemma_artifact_dir / "result.json"

    previous_text = build_lemma_scratch_text(
        lemma_id=lemma.lemma_id,
        pinned_signature=pinned.signature,
        trusted_entries=trusted_entries,
        statements_path=run_paths.workspace_dir / "Orthos" / "Statements.lean",
    )
    write_text(workspace_scratch_path, previous_text)

    # Protect non-scratch, non-Lemmas Lean files from accidental writes by Claude.
    # Lemmas.lean is excluded because it must remain writable for merge operations.
    _protected_files: list[tuple[Path, int]] = []
    for _lean_file in (run_paths.workspace_dir / "Orthos").glob("*.lean"):
        if _lean_file.name.startswith("Scratch_") or _lean_file.name == "Lemmas.lean":
            continue
        try:
            _orig_mode = _lean_file.stat().st_mode
            _lean_file.chmod(_orig_mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
            _protected_files.append((_lean_file, _orig_mode))
        except OSError:
            pass

    def _restore_permissions() -> None:
        for _path, _mode in _protected_files:
            try:
                _path.chmod(_mode)
            except OSError:
                pass

    # --- Solver cascade: try cheap tactics BEFORE invoking Claude ---
    from .lean4_skills_scripts import run_solver_cascade as _run_solver_cascade
    if runtime_config.integrations.lean4_skills_root:
        cascade_result = _run_solver_cascade(
            lean4_skills_root=runtime_config.integrations.lean4_skills_root,
            workspace_root=run_paths.workspace_dir,
            target_file=workspace_scratch_path,
            timeout_seconds=60,
        )
        if cascade_result.get("status") == "ok":
            _log.info("Lemma %s: solver cascade succeeded — zero LLM cost.", lemma.lemma_id)
            # Verify the cascade result compiles
            check = check_lean_file(
                run_paths.workspace_dir,
                workspace_relative_scratch,
                timeout_seconds=timeout_seconds if timeout_seconds > 0 else 30,
                runner=runner,
                lake_jobs=runtime_config.lean.lake_jobs,
            )
            if check.ok:
                solved_text = workspace_scratch_path.read_text(encoding="utf-8")
                _restore_permissions()
                result = LemmaFormalizationResult(
                    lemma_id=lemma.lemma_id,
                    decl_name=pinned.decl_name,
                    status="ok",
                    attempts_used=0,
                    lemma_artifact_dir=lemma_artifact_dir,
                    scratch_file=workspace_scratch_path,
                    result_path=result_path,
                    attempts=(),
                    message="Solved by lean4-skills solver cascade (zero LLM cost).",
                    diagnostics=(),
                )
                write_json(result_path, result.to_dict())
                return result

    # --- Structured error tracking for stuck detection ---
    error_hash_history: list[str] = []

    attempt_results: list[LemmaAttemptResult] = []
    latest_diagnostics = ""
    accumulated_discoveries: list[dict] = []
    last_round_false_negative = False  # Track MCP false negatives for prompt warning.
    previous_sorry_count: int | None = None  # Track sorry count for partial progress.
    best_candidate_text: str | None = None  # Best compiling candidate across rounds.
    best_sorry_count: int | None = None  # Sorry count in best candidate.

    # Free retry tracking (Phase 2).
    productive_round_count = 0
    free_retries_used = 0
    free_retries_stall = 0
    free_retries_api = 0
    free_retries_max_tokens = 0
    lemma_start_time = time.time()

    round_index = 0
    while productive_round_count < max_attempts:
        round_index += 1
        # Safety cap: prevent infinite loop from free retries.
        if round_index > max_attempts + _MAX_FREE_RETRIES_TOTAL + 2:
            _log.warning("Lemma %s: hard round cap reached (%d), stopping.", lemma.lemma_id, round_index)
            break

        # In parallel mode, refresh trusted entries and check for fatal abort.
        if shared_context is not None:
            if shared_context.should_abort:
                _log.info("Lemma %s: aborting (fatal signal from another worker).", lemma.lemma_id)
                break
            trusted_entries = shared_context.snapshot()

        prompt_path = lemma_artifact_dir / f"prompt_round_{round_index:02d}.md"
        raw_path = lemma_artifact_dir / f"claude_round_{round_index:02d}.jsonl"
        scratch_path = lemma_artifact_dir / f"scratch_round_{round_index:02d}.lean"
        diagnostics_path = lemma_artifact_dir / f"diagnostics_round_{round_index:02d}.txt"
        diff_path = lemma_artifact_dir / f"diff_round_{round_index:02d}.patch"

        # --- Model/effort routing (Phase 2) ---
        has_prior_candidate = round_index > 1 and previous_text.strip() != ""
        has_concrete = _is_concrete_error(latest_diagnostics)
        effective_model, effort_label, extra_env = _select_model_and_effort(
            round_index=productive_round_count + 1,
            max_attempts=max_attempts,
            runtime_config=runtime_config,
            model_override=model,
            has_prior_candidate=has_prior_candidate,
            has_concrete_errors=has_concrete,
        )

        # --- Metrics (Phase 1) ---
        scratch_hash_before = _content_hash(previous_text)
        metrics = RoundMetrics(
            lemma_id=lemma.lemma_id,
            round_index=round_index,
            model=effective_model,
            effort=effort_label,
            round_start_ts=time.time(),
            scratch_hash_before=scratch_hash_before,
            error_count_before=len(latest_diagnostics.splitlines()) if latest_diagnostics else 0,
        )

        # Seed repair rounds from the latest draft, even if the last attempt
        # failed or was rejected. This preserves Claude's newest scaffolding
        # instead of rewinding to an older compiling partial proof.
        if round_index > 1:
            write_text(workspace_scratch_path, previous_text)

        # Round 1 gets the full reference library; repair rounds get compact refs.
        prompt_refs = lean4_skills_refs if round_index == 1 else lean4_skills_refs_compact

        prompt = build_lemma_formalization_prompt(
            lemma=lemma,
            pinned=pinned,
            trusted_entries=trusted_entries,
            scratch_relative_path=workspace_relative_scratch,
            repair_round=round_index,
            diagnostics_text=latest_diagnostics if round_index > 1 else None,
            previous_candidate=previous_text if round_index > 1 else None,
            search_discoveries=format_search_discoveries(accumulated_discoveries) if accumulated_discoveries else None,
            false_negative_warning=last_round_false_negative,
            previous_sorry_count=previous_sorry_count,
            best_partial_candidate=best_candidate_text,
            best_partial_sorry_count=best_sorry_count,
            lean4_skills_refs=prompt_refs,
        )
        last_round_false_negative = False  # Reset for this round.
        write_text(prompt_path, prompt)

        used_mock_candidate = False
        candidate_backend = "internal"
        candidate_source = "none"
        candidate_selection_reasons: tuple[str, ...] = ()
        candidate_lean_score = 0
        round_stall_killed = False
        if mock_candidates and round_index - 1 < len(mock_candidates):
            used_mock_candidate = True
            candidate_text = normalize_lean_text(mock_candidates[round_index - 1])
            candidate_backend = "mock"
            candidate_source = "mock_candidate"
            candidate_selection_reasons = ("selected mock candidate",)
            _write_mock_claude_raw(raw_path, candidate_text)
        else:
            if claude_runner is None:
                claude_runner = ClaudeRunner(runtime_config)

            # Per-round hard timeout is policy-driven via function/runtime config.
            round_hard_timeout = timeout_seconds if timeout_seconds > 0 else runtime_config.claude.timeout_seconds
            round_stall = _round_stall_timeout(
                productive_round_count + 1,
                runtime_config.claude.stall_timeout_seconds,
            )

            # Build per-round extra env (effort + thinking caps).
            round_env = dict(extra_env)

            claude_result = claude_runner.run_prompt(
                run_paths=run_paths,
                prompt=prompt,
                phase_name=f"phase04_{sanitize_component(lemma.lemma_id)}_round_{round_index:02d}",
                model=effective_model,
                timeout_seconds=round_hard_timeout,
                idle_timeout_seconds=round_stall,
                tool_wait_timeout_seconds=runtime_config.claude.tool_wait_timeout_seconds,
                init_timeout_seconds=runtime_config.claude.init_timeout_seconds,
                permission_mode="bypassPermissions",
                allowed_tools="mcp,Bash,Read,Glob,Grep,Write,Edit,Agent",
                # No max_turns limit — hard timeout is the real guardrail.
                # Claude needs unlimited turns for iterative MCP testing.
                extra_env=round_env if round_env else None,
            )
            round_stall_killed = claude_result.stall_killed
            # Always save claude raw output for debuggability, regardless
            # of quota status.
            _copy_claude_raw(claude_result, raw_path)
            # Only treat as quota exhaustion if Claude did NOT complete
            # successfully.  Transient rate-limit rejections that the CLI
            # retried (and eventually succeeded past) should not discard a
            # valid proof.
            if claude_result.provider_limit_detected and not claude_result.ok:
                provider_text = "provider_quota_exhausted: Claude reported account/org limit exhaustion."
                write_text(diagnostics_path, provider_text + "\n")
                # Snapshot the workspace scratch file so we preserve
                # whatever partial work Claude produced before quota hit.
                try:
                    ws_text = workspace_scratch_path.read_text(encoding="utf-8")
                except OSError:
                    ws_text = previous_text
                write_text(scratch_path, ws_text)
                write_text(diff_path, _render_diff(previous_text, ws_text))
                attempt = LemmaAttemptResult(
                    round_index=round_index,
                    scratch_path=scratch_path,
                    prompt_path=prompt_path,
                    claude_raw_path=raw_path,
                    diagnostics_path=diagnostics_path,
                    diff_path=diff_path,
                    status="failed",
                    classification="provider_quota_exhausted",
                    diagnostics=(provider_text,),
                    check_result=None,
                    progress_guard_error=None,
                    used_mock_candidate=used_mock_candidate,
                    candidate_backend=candidate_backend,
                    candidate_source="none",
                    candidate_selection_reasons=("provider quota exhausted",),
                    candidate_lean_score=0,
                )
                attempt_results.append(attempt)
                metrics.diagnostic_status = "provider_quota_exhausted"
                metrics.finalize()
                _append_round_metrics(run_paths.run_root, metrics)
                result = LemmaFormalizationResult(
                    lemma_id=lemma.lemma_id,
                    decl_name=pinned.decl_name,
                    status="failed",
                    attempts_used=round_index,
                    lemma_artifact_dir=lemma_artifact_dir,
                    scratch_file=workspace_scratch_path,
                    result_path=result_path,
                    attempts=tuple(attempt_results),
                    error_class="provider_quota_exhausted",
                    message="Claude provider quota was exhausted during lemma formalization.",
                    diagnostics=(provider_text,),
                )
                write_json(result_path, result.to_dict())
                _restore_permissions()
                return result
            # Extract search discoveries for cross-round context.
            round_discoveries = extract_search_discoveries(raw_path)
            if round_discoveries:
                accumulated_discoveries.extend(round_discoveries)
            selected = select_lean_candidate(
                trace=claude_result.trace,
                target_path=workspace_scratch_path,
                baseline_text=previous_text,
                normalize_text=normalize_lean_text,
                stage_validator=_build_lemma_stage_validator(pinned.decl_name),
            )
            candidate_source = selected.source
            candidate_selection_reasons = selected.reasons
            candidate_lean_score = selected.lean_score
            candidate_text = selected.text if selected.text.strip() else previous_text

            # If candidate extraction scored 0 or returned empty, check if Claude
            # wrote a valid proof directly to the workspace file via MCP tool_update.
            if candidate_lean_score == 0 or not selected.text.strip():
                workspace_current = workspace_scratch_path.read_text(encoding="utf-8")
                if workspace_current.strip() and workspace_current != previous_text:
                    candidate_text = workspace_current
                    candidate_source = "workspace_mcp_edit"
                    candidate_selection_reasons = ("fallback: Claude wrote to workspace via MCP",)

        # --- Check for nonproductive round (Phase 2: free retries) ---
        scratch_hash_after = _content_hash(candidate_text)
        metrics.scratch_hash_after = scratch_hash_after
        metrics.candidate_source = candidate_source
        metrics.stall_killed = round_stall_killed
        round_is_nonproductive = (
            scratch_hash_before == scratch_hash_after
            and candidate_source in ("none", "")
            and not used_mock_candidate
        )

        if round_is_nonproductive and free_retries_used < _MAX_FREE_RETRIES_TOTAL:
            can_retry = False
            if round_stall_killed and free_retries_stall < _MAX_FREE_RETRIES_STALL:
                free_retries_stall += 1
                can_retry = True
            elif not round_stall_killed and free_retries_api < _MAX_FREE_RETRIES_API:
                free_retries_api += 1
                can_retry = True

            if can_retry:
                free_retries_used += 1
                metrics.free_retry_used = True
                metrics.progress_made = False
                metrics.finalize()
                _append_round_metrics(run_paths.run_root, metrics)
                _log.info(
                    "Lemma %s round %d: nonproductive (stall=%s), free retry %d/%d.",
                    lemma.lemma_id, round_index, round_stall_killed,
                    free_retries_used, _MAX_FREE_RETRIES_TOTAL,
                )
                continue  # Don't count this round

        productive_round_count += 1

        write_text(workspace_scratch_path, candidate_text)
        write_text(scratch_path, candidate_text)
        write_text(diff_path, _render_diff(previous_text, candidate_text))

        guard_error = progress_guard_error(
            candidate_text=candidate_text,
            pinned_signature=pinned.signature,
            target_decl_name=pinned.decl_name,
            trusted_entries=trusted_entries,
            previous_sorry_count=previous_sorry_count,
        )
        if guard_error:
            latest_diagnostics = guard_error
            write_text(diagnostics_path, guard_error + "\n")
            classification = classify_lemma_failure(guard_error, guard_error=guard_error)
            attempt = LemmaAttemptResult(
                round_index=round_index,
                scratch_path=scratch_path,
                prompt_path=prompt_path,
                claude_raw_path=raw_path,
                diagnostics_path=diagnostics_path,
                diff_path=diff_path,
                status="rejected",
                classification=classification,
                diagnostics=(guard_error,),
                check_result=None,
                progress_guard_error=guard_error,
                used_mock_candidate=used_mock_candidate,
                candidate_backend=candidate_backend,
                candidate_source=candidate_source,
                candidate_selection_reasons=candidate_selection_reasons,
                candidate_lean_score=candidate_lean_score,
            )
            attempt_results.append(attempt)
            previous_text = candidate_text
            metrics.diagnostic_status = "rejected"
            metrics.finalize()
            _append_round_metrics(run_paths.run_root, metrics)
            continue

        # Independent verification: compile the scratch file with lake env lean.
        # This catches errors that MCP diagnostics may have missed (stale oleans,
        # infrastructure failures, context mismatches).
        _log.info("Lemma %s round %d: running lake env lean verification...", lemma.lemma_id, round_index)
        lean_check = check_lean_file(
            run_paths.workspace_dir,
            workspace_relative_scratch,
            timeout_seconds=(
                lean_check_timeout_seconds
                if isinstance(lean_check_timeout_seconds, int) and lean_check_timeout_seconds > 0
                else timeout_seconds
            ),
            lake_jobs=runtime_config.lean.lake_jobs,
        )
        if not lean_check.ok:
            diagnostics_text = lean_check.stderr or lean_check.stdout or "lake env lean failed with no output"
            latest_diagnostics = diagnostics_text
            write_text(diagnostics_path, diagnostics_text)
            classification = classify_lemma_failure(diagnostics_text)
            # --- Structured error parsing + stuck detection ---
            from .lean4_skills_scripts import parse_lean_errors as _parse_errors
            if runtime_config.integrations.lean4_skills_root:
                parsed_errors = _parse_errors(
                    lean4_skills_root=runtime_config.integrations.lean4_skills_root,
                    workspace_root=run_paths.workspace_dir,
                    stderr_text=diagnostics_text,
                )
                for pe in parsed_errors:
                    eh = str(pe.get("error_hash", ""))
                    if eh:
                        error_hash_history.append(eh)
                # Stuck detection: same error_hash 2+ times consecutively
                if len(error_hash_history) >= 2 and error_hash_history[-1] == error_hash_history[-2]:
                    _log.warning(
                        "Lemma %s round %d: STUCK — same error repeated (hash=%s).",
                        lemma.lemma_id, round_index, error_hash_history[-1][:16],
                    )
            metrics.diagnostic_status = "errors"
            # Detect false negative: MCP said clean but lake env lean found errors.
            metrics.false_negative = True
            last_round_false_negative = True
            metrics.error_count_after = len(diagnostics_text.splitlines())
            attempt = LemmaAttemptResult(
                round_index=round_index,
                scratch_path=scratch_path,
                prompt_path=prompt_path,
                claude_raw_path=raw_path,
                diagnostics_path=diagnostics_path,
                diff_path=diff_path,
                status="failed",
                classification=classification,
                diagnostics=tuple(_diagnostic_lines(diagnostics_text)),
                check_result=lean_check.to_dict(),
                progress_guard_error=None,
                used_mock_candidate=used_mock_candidate,
                candidate_backend=candidate_backend,
                candidate_source=candidate_source,
                candidate_selection_reasons=candidate_selection_reasons,
                candidate_lean_score=candidate_lean_score,
            )
            attempt_results.append(attempt)
            previous_text = candidate_text
            _log.info("Lemma %s round %d: lake env lean FAILED, will retry.", lemma.lemma_id, round_index)
            metrics.finalize()
            _append_round_metrics(run_paths.run_root, metrics)
            continue
        diagnostics_text = "Verified: lake env lean passed."
        latest_diagnostics = diagnostics_text
        write_text(diagnostics_path, diagnostics_text)
        _log.info("Lemma %s round %d: lake env lean PASSED.", lemma.lemma_id, round_index)
        metrics.diagnostic_status = "clean"

        # Extract target + any helper lemmas Claude defined before it.
        # Helpers are private lemmas used by the target proof; they must be
        # carried into Lemmas.lean so Combined.lean compiles correctly.
        _statements_path = run_paths.workspace_dir / "Orthos" / "Statements.lean"
        _trusted_names = {e.decl_name for e in trusted_entries}
        target_declaration = extract_proof_block_with_helpers(
            candidate_text,
            pinned.decl_name,
            trusted_names=_trusted_names,
            statements_open_lines=_extract_open_lines(_statements_path),
        )
        current_sorry_count = _count_target_sorry(candidate_text, pinned.decl_name)
        # Also check the full extracted block (target + helpers) for sorry/admit.
        # _count_target_sorry only inspects the target theorem itself, but
        # extract_proof_block_with_helpers may pull in private helper lemmas
        # that still contain sorry.
        block_sorry_count = len(re.findall(r"\bsorry\b", target_declaration))
        has_admit = bool(re.search(r"\badmit\b", target_declaration))

        if current_sorry_count == 0 and block_sorry_count == 0 and not has_admit:
            # COMPLETE proof — no sorry, no admit.
            final_success_path = lemma_artifact_dir / "final_success.lean"
            write_text(final_success_path, target_declaration.strip() + "\n")

            if not commit_on_success:
                attempt = LemmaAttemptResult(
                    round_index=round_index,
                    scratch_path=scratch_path,
                    prompt_path=prompt_path,
                    claude_raw_path=raw_path,
                    diagnostics_path=diagnostics_path,
                    diff_path=diff_path,
                    status="ok",
                    classification=None,
                    diagnostics=(),
                    check_result=None,
                    progress_guard_error=None,
                    used_mock_candidate=used_mock_candidate,
                    candidate_backend=candidate_backend,
                    candidate_source=candidate_source,
                    candidate_selection_reasons=candidate_selection_reasons,
                    candidate_lean_score=candidate_lean_score,
                )
                attempt_results.append(attempt)

                result = LemmaFormalizationResult(
                    lemma_id=lemma.lemma_id,
                    decl_name=pinned.decl_name,
                    status="ok",
                    attempts_used=round_index,
                    lemma_artifact_dir=lemma_artifact_dir,
                    scratch_file=workspace_scratch_path,
                    result_path=result_path,
                    attempts=tuple(attempt_results),
                    final_success_path=final_success_path,
                )
                write_json(result_path, result.to_dict())
                metrics.finalize()
                _append_round_metrics(run_paths.run_root, metrics)
                _restore_permissions()
                return result

            # Remove scratch file before merge to prevent lean_lib name collisions.
            workspace_scratch_path.unlink(missing_ok=True)

            # Acquire merge lock when in parallel mode to serialize Lemmas.lean writes.
            _merge_ctx = shared_context.merge_lock if shared_context is not None else _noop_lock()
            with _merge_ctx:
                original_lemmas_text = lemmas_file_path.read_text(encoding="utf-8")
                merged_lemmas_text = merge_declaration_into_lemmas_file(
                    original_text=original_lemmas_text,
                    declaration_block=target_declaration,
                )
                write_text(lemmas_file_path, merged_lemmas_text)
                if _has_disallowed_proof_tokens(merged_lemmas_text):
                    write_text(lemmas_file_path, original_lemmas_text)
                    merge_fail_text = "policy_violation: disallowed token `sorry` or `admit` in merged Lemmas.lean."
                    write_text(diagnostics_path, merge_fail_text)
                    latest_diagnostics = merge_fail_text
                else:
                    new_entry = TrustedContextEntry(
                        lemma_id=lemma.lemma_id,
                        decl_name=pinned.decl_name,
                        status="compiled",
                        source_file="Orthos/Lemmas.lean",
                        signature=pinned.signature,
                        declaration=target_declaration.strip(),
                    )
                    trusted_entries.append(new_entry)
                    if shared_context is not None:
                        shared_context.add(new_entry)
                    write_trusted_manifest(manifest_path, manifest_problem_id, trusted_entries)

                    attempt = LemmaAttemptResult(
                        round_index=round_index,
                        scratch_path=scratch_path,
                        prompt_path=prompt_path,
                        claude_raw_path=raw_path,
                        diagnostics_path=diagnostics_path,
                        diff_path=diff_path,
                        status="ok",
                        classification=None,
                        diagnostics=(),
                        check_result=None,
                        progress_guard_error=None,
                        used_mock_candidate=used_mock_candidate,
                        candidate_backend=candidate_backend,
                        candidate_source=candidate_source,
                        candidate_selection_reasons=candidate_selection_reasons,
                        candidate_lean_score=candidate_lean_score,
                    )
                    attempt_results.append(attempt)

                    result = LemmaFormalizationResult(
                        lemma_id=lemma.lemma_id,
                        decl_name=pinned.decl_name,
                        status="ok",
                        attempts_used=round_index,
                        lemma_artifact_dir=lemma_artifact_dir,
                        scratch_file=workspace_scratch_path,
                        result_path=result_path,
                        attempts=tuple(attempt_results),
                        final_success_path=final_success_path,
                    )
                    write_json(result_path, result.to_dict())
                    metrics.finalize()
                    _append_round_metrics(run_paths.run_root, metrics)
                    _restore_permissions()
                    return result
            classification = "policy_violation"
            attempt = LemmaAttemptResult(
                round_index=round_index,
                scratch_path=scratch_path,
                prompt_path=prompt_path,
                claude_raw_path=raw_path,
                diagnostics_path=diagnostics_path,
                diff_path=diff_path,
                status="failed",
                classification=classification,
                diagnostics=(merge_fail_text,),
                check_result=None,
                progress_guard_error=None,
                used_mock_candidate=used_mock_candidate,
                candidate_backend=candidate_backend,
                candidate_source=candidate_source,
                candidate_selection_reasons=candidate_selection_reasons,
                candidate_lean_score=candidate_lean_score,
            )
            attempt_results.append(attempt)
            previous_text = candidate_text
            metrics.finalize()
            _append_round_metrics(run_paths.run_root, metrics)
            continue

        elif (current_sorry_count > 0 or block_sorry_count > 0) and not has_admit:
            # PARTIAL PROGRESS: compiles with sorry stubs (in target and/or helpers).
            # Use the larger of the two counts to track progress — helpers with sorry
            # are just as incomplete as the target having sorry.
            effective_sorry_count = max(current_sorry_count, block_sorry_count)
            # Accept same-or-fewer sorry count so Claude can try different
            # approaches without being rejected.  Best candidate tracking
            # ensures we always keep the version with fewest sorries.
            is_progress = (
                previous_sorry_count is None
                or effective_sorry_count <= previous_sorry_count
            )
            if is_progress:
                previous_sorry_count = effective_sorry_count
                # Track the best compiling candidate (fewest sorries).
                if best_sorry_count is None or effective_sorry_count < best_sorry_count:
                    best_candidate_text = candidate_text
                    best_sorry_count = effective_sorry_count
                sorry_detail = (
                    f"{current_sorry_count} in target, {block_sorry_count} in block incl. helpers"
                    if block_sorry_count != current_sorry_count
                    else f"{current_sorry_count}"
                )
                partial_text = (
                    f"Partial progress: proof compiles with {effective_sorry_count} sorry placeholder(s) "
                    f"({sorry_detail}). "
                    "Fill in the remaining sorry stubs."
                )
                write_text(diagnostics_path, partial_text)
                latest_diagnostics = partial_text
                _log.info(
                    "Lemma %s round %d: partial progress (%d sorry stubs, %d in helpers).",
                    lemma.lemma_id, round_index, current_sorry_count, block_sorry_count - current_sorry_count,
                )
                attempt = LemmaAttemptResult(
                    round_index=round_index,
                    scratch_path=scratch_path,
                    prompt_path=prompt_path,
                    claude_raw_path=raw_path,
                    diagnostics_path=diagnostics_path,
                    diff_path=diff_path,
                    status="failed",
                    classification="sorry_remaining",
                    diagnostics=(partial_text,),
                    check_result=None,
                    progress_guard_error=None,
                    used_mock_candidate=used_mock_candidate,
                    candidate_backend=candidate_backend,
                    candidate_source=candidate_source,
                    candidate_selection_reasons=candidate_selection_reasons,
                    candidate_lean_score=candidate_lean_score,
                )
                attempt_results.append(attempt)
                previous_text = candidate_text
                metrics.diagnostic_status = "partial_progress"
                metrics.finalize()
                _append_round_metrics(run_paths.run_root, metrics)
                continue
            else:
                # Sorry count increased — regression.  Still track as best
                # if this is the first compiling candidate we've seen.
                if best_candidate_text is None:
                    best_candidate_text = candidate_text
                    best_sorry_count = effective_sorry_count
                failure_text = (
                    f"No progress: {effective_sorry_count} sorry placeholder(s) "
                    f"(previous had {previous_sorry_count}). Reduce sorry count."
                )
                write_text(diagnostics_path, failure_text)
                latest_diagnostics = failure_text
                attempt = LemmaAttemptResult(
                    round_index=round_index,
                    scratch_path=scratch_path,
                    prompt_path=prompt_path,
                    claude_raw_path=raw_path,
                    diagnostics_path=diagnostics_path,
                    diff_path=diff_path,
                    status="failed",
                    classification="sorry_remaining",
                    diagnostics=(failure_text,),
                    check_result=None,
                    progress_guard_error=None,
                    used_mock_candidate=used_mock_candidate,
                    candidate_backend=candidate_backend,
                    candidate_source=candidate_source,
                    candidate_selection_reasons=candidate_selection_reasons,
                    candidate_lean_score=candidate_lean_score,
                )
                attempt_results.append(attempt)
                previous_text = candidate_text
                metrics.finalize()
                _append_round_metrics(run_paths.run_root, metrics)
                continue

        # Has admit — always rejected.
        failure_text = "policy_violation: disallowed token `admit` in target declaration."
        write_text(diagnostics_path, failure_text)
        latest_diagnostics = failure_text
        classification = classify_lemma_failure(failure_text)
        attempt = LemmaAttemptResult(
            round_index=round_index,
            scratch_path=scratch_path,
            prompt_path=prompt_path,
            claude_raw_path=raw_path,
            diagnostics_path=diagnostics_path,
            diff_path=diff_path,
            status="failed",
            classification=classification,
            diagnostics=tuple(_diagnostic_lines(failure_text)),
            check_result=None,
            progress_guard_error=None,
            used_mock_candidate=used_mock_candidate,
            candidate_backend=candidate_backend,
            candidate_source=candidate_source,
            candidate_selection_reasons=candidate_selection_reasons,
            candidate_lean_score=candidate_lean_score,
        )
        attempt_results.append(attempt)
        previous_text = candidate_text
        metrics.finalize()
        _append_round_metrics(run_paths.run_root, metrics)

    error_class = attempt_results[-1].classification if attempt_results else "tactic_failure"
    diagnostics = attempt_results[-1].diagnostics if attempt_results else ("no attempts executed",)

    # Signal fatal abort to other parallel workers on confirmed mathematical flaw.
    if shared_context is not None and error_class == "major_proof_gap":
        shared_context.signal_fatal(
            f"Lemma {lemma.lemma_id} identified a major proof gap in the NL proof."
        )

    _restore_permissions()

    result = LemmaFormalizationResult(
        lemma_id=lemma.lemma_id,
        decl_name=pinned.decl_name,
        status="failed",
        attempts_used=len(attempt_results),
        lemma_artifact_dir=lemma_artifact_dir,
        scratch_file=workspace_scratch_path,
        result_path=result_path,
        error_class=error_class,
        message="Lemma formalization exhausted bounded retry budget without success.",
        diagnostics=diagnostics,
        attempts=tuple(attempt_results),
    )
    write_json(result_path, result.to_dict())
    return result


def _normalize_for_sig_comparison(text: str) -> str:
    """Normalize a signature for comparison, treating ```;``` as whitespace.

    In Lean 4 type signatures, ``;`` only appears as a let-binding separator.
    ``normalize_signature_text()`` collapses newlines (which also serve as
    let-separators) into spaces.  Claude may re-insert ``;`` to fix the syntax.
    Both forms should compare equal for pinned-signature matching.
    """
    return " ".join(normalize_signature_text(text).replace(";", " ").split())


def progress_guard_error(
    *,
    candidate_text: str,
    pinned_signature: str,
    target_decl_name: str,
    trusted_entries: list[TrustedContextEntry],
    previous_sorry_count: int | None = None,
) -> str | None:
    normalized_candidate = normalize_lean_text(candidate_text)

    decl_blocks = _extract_declaration_blocks(normalized_candidate)
    if not decl_blocks:
        return "Progress guard rejected candidate: no Lean declarations were found."

    trusted_names = {entry.decl_name for entry in trusted_entries}
    target_blocks = [block for block in decl_blocks if block["name"] == target_decl_name]
    if not target_blocks:
        return f"Progress guard rejected candidate: target declaration `{target_decl_name}` is missing."
    if len(target_blocks) != 1:
        return f"Progress guard rejected candidate: declaration `{target_decl_name}` appears multiple times."
    target_idx = next(i for i, b in enumerate(decl_blocks) if b["name"] == target_decl_name)

    # Check sorry/admit in the target declaration.
    # Scratch files include sorry-stubbed trusted entries — only the target block matters.
    target_block_text = target_blocks[0]["block"]
    has_admit = bool(re.search(r"\badmit\b", target_block_text))
    if has_admit:
        return "Progress guard rejected: `admit` found in the target declaration. `admit` is never accepted."

    current_sorry_count = len(re.findall(r"\bsorry\b", target_block_text))
    if current_sorry_count > 0:
        # Accept sorry-containing candidates if they represent progress or
        # maintain the same sorry count (allows trying different approaches).
        is_progress = (
            previous_sorry_count is None  # First round: any skeleton is progress
            or current_sorry_count <= previous_sorry_count  # Same or fewer = accepted
        )
        if not is_progress:
            return (
                f"Progress guard rejected: `sorry` found ({current_sorry_count} instance(s)), "
                f"no improvement over previous ({previous_sorry_count}).\n"
                "Reduce the sorry count or provide a complete proof.\n"
                "Strategies:\n"
                "- Decompose the goal into smaller `have` steps you can prove individually.\n"
                "- Use `suffices` to reduce to a simpler sub-goal.\n"
                "- Search for relevant Mathlib lemmas with lean_loogle or lean_leandex.\n"
                "- If `linarith` fails, try `field_simp` first to clear denominators, or `nlinarith`.\n"
                "- If a specific API name is unknown, search for it — names may have subscripts like ₀."
            )
        # else: fall through — sorry accepted as partial progress

    for i, block in enumerate(decl_blocks):
        name = block["name"]
        if name == target_decl_name:
            continue
        if name in trusted_names:
            # Trusted entries appear as sorry-stubs in scratch files — allowed.
            continue
        if i < target_idx:
            # Helper lemma defined BEFORE the target — allowed and encouraged.
            # It will be extracted alongside the target by extract_proof_block_with_helpers().
            continue
        return (
            f"Progress guard rejected candidate: declaration `{name}` appears after the "
            f"target `{target_decl_name}`. Helper lemmas must be defined BEFORE the target "
            "so the target proof can reference them."
        )

    try:
        extracted = extract_statement_signature(normalized_candidate, target_decl_name).signature
    except ValueError as exc:
        return f"Progress guard rejected candidate: unable to parse target declaration signature ({exc})."

    if _normalize_for_sig_comparison(extracted) != _normalize_for_sig_comparison(pinned_signature):
        return (
            "Progress guard rejected candidate: target declaration header drifted from pinned signature.\n"
            f"expected: {pinned_signature}\n"
            f"actual:   {extracted}"
        )

    return None


def extract_target_declaration_block(candidate_text: str, target_decl_name: str) -> str:
    decl_blocks = _extract_declaration_blocks(candidate_text)
    matches = [block for block in decl_blocks if block["name"] == target_decl_name]
    if len(matches) != 1:
        raise ValueError(f"expected exactly one declaration block for {target_decl_name}")
    block_text = _strip_trailing_orthos_end(matches[0]["block"])
    # Strip namespace/open lines that Claude may have injected.
    cleaned_lines = []
    for line in block_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("namespace ") or stripped.startswith("open "):
            continue
        cleaned_lines.append(line)
    block_text = "\n".join(cleaned_lines)
    if not block_text.strip():
        raise ValueError(f"declaration block for `{target_decl_name}` is empty")
    return block_text.rstrip() + "\n"


def extract_proof_block_with_helpers(
    candidate_text: str,
    target_decl_name: str,
    trusted_names: set[str],
    statements_open_lines: list[str] | None = None,
) -> str:
    """Extract the target declaration and any helper lemmas defined before it.

    Claude may define private helper lemmas in the scratch file to support the
    main proof.  This function extracts those helpers alongside the target so
    they are all merged into Lemmas.lean and available in Combined.lean.

    Skips:
    - Trusted-context entries (identified by name being in ``trusted_names``).
      These are sorry-stubs injected by the engine and are separately managed.
    - Declarations that appear AFTER the target (unreachable from target proof).

    Also carries over any extra ``open`` directives Claude added to the scratch
    file beyond those already provided by Statements.lean, so that unqualified
    names in helpers (e.g. ``sin`` when ``open Real`` was used) resolve
    correctly in Lemmas.lean.
    """
    decl_blocks = _extract_declaration_blocks(candidate_text)

    # Verify target exists exactly once.
    target_matches = [b for b in decl_blocks if b["name"] == target_decl_name]
    if len(target_matches) != 1:
        raise ValueError(f"expected exactly one declaration block for {target_decl_name}")

    target_idx = next(i for i, b in enumerate(decl_blocks) if b["name"] == target_decl_name)

    # Detect extra ``open`` directives Claude added (beyond Statements.lean defaults).
    # Grab the file header (before the first declaration).
    first_decl_start = decl_blocks[0]["start"] if decl_blocks else len(candidate_text)
    header_text = candidate_text[:first_decl_start]
    scratch_open_names: set[str] = set()
    for line in header_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("open "):
            scratch_open_names.update(stripped[len("open "):].split())

    standard_open_names: set[str] = set()
    if statements_open_lines:
        for line in statements_open_lines:
            standard_open_names.update(line.strip()[len("open "):].split())

    extra_open_names = scratch_open_names - standard_open_names

    # Collect helpers (before target) + target, skipping trusted entries.
    result_blocks: list[str] = []
    for i, block in enumerate(decl_blocks):
        if i > target_idx:
            break  # Nothing after the target is reachable from the target proof.
        name = block["name"]
        if name in trusted_names:
            # Trusted-context sorry-stubs — already managed by the engine.
            continue
        # Strip namespace/open lines from the individual block text.
        cleaned_lines = []
        for line in _strip_trailing_orthos_end(block["block"]).splitlines():
            s = line.strip()
            if s.startswith("namespace ") or s.startswith("open "):
                continue
            cleaned_lines.append(line)
        cleaned = "\n".join(cleaned_lines).rstrip()
        if cleaned.strip():
            result_blocks.append(cleaned)

    if not result_blocks:
        raise ValueError(f"no declarations found for extraction around `{target_decl_name}`")

    # Prepend any extra open directives so helpers that rely on them still compile.
    parts: list[str] = []
    if extra_open_names:
        parts.append("open " + " ".join(sorted(extra_open_names)))
    parts.extend(result_blocks)

    return "\n\n".join(parts) + "\n"


def _count_target_sorry(candidate_text: str, target_decl_name: str) -> int:
    """Count ``sorry`` instances in the target declaration block only."""
    try:
        block = extract_target_declaration_block(candidate_text, target_decl_name)
    except ValueError:
        return 0
    return len(re.findall(r"\bsorry\b", block))


def merge_declaration_into_lemmas_file(*, original_text: str, declaration_block: str) -> str:
    # Try new-style marker first, fall back to legacy `end Orthos` for old runs.
    marker = _LEMMA_END_MARKER
    insert_at = original_text.rfind(marker)
    if insert_at < 0:
        marker = "end Orthos"
        insert_at = original_text.rfind(marker)
    if insert_at < 0:
        # No marker at all — just append to the end of the file.
        block = _strip_trailing_orthos_end(declaration_block).strip()
        if not block:
            raise ValueError("declaration block is empty after trimming")
        return original_text.rstrip() + "\n\n" + block + "\n"

    before = original_text[:insert_at].rstrip()
    after = original_text[insert_at:]
    block = _strip_trailing_orthos_end(declaration_block).strip()
    if not block:
        raise ValueError("declaration block is empty after trimming")
    merged = f"{before}\n\n{block}\n\n{after.lstrip()}"
    return merged.rstrip() + "\n"


def render_lean_check_diagnostics(check_result: LeanCommandResult) -> str:
    lines = [
        f"command: {' '.join(check_result.command)}",
        f"returncode: {check_result.returncode}",
        "",
        "stdout:",
        check_result.stdout.rstrip(),
        "",
        "stderr:",
        check_result.stderr.rstrip(),
        "",
    ]
    return "\n".join(lines)


def classify_lemma_failure(diagnostics_text: str, *, guard_error: str | None = None) -> str:
    text = (guard_error or diagnostics_text or "").lower()
    if "provider_quota_exhausted" in text or "hit your limit" in text or "rate_limit_event" in text:
        return "provider_quota_exhausted"
    if "provider_api_timeout" in text or "request timed out" in text:
        return "provider_api_timeout"
    if MAJOR_GAP_PATTERN.search(text):
        return "major_proof_gap"
    if "has already been declared" in text or "already declared" in text:
        return "stale_olean"
    if (
        "pinned signature" in text
        or "unrelated declaration" in text
        or "already-trusted declaration" in text
    ):
        return "false_lemma_suspected"
    if (
        "reservoir lookup failed" in text
        or "could not materialize package" in text
        or "failed to download" in text
        or "network is unreachable" in text
        or "curl:" in text
    ):
        return "environment_dependency_missing"
    if "unknown package" in text or "unknown import" in text or "unknown module prefix" in text:
        return "missing_import"
    if "parse error" in text or "unexpected token" in text or "invalid syntax" in text or "expected token" in text:
        return "syntax"
    if "unknown constant" in text or "unknown identifier" in text or "not found in environment" in text:
        return "missing_library_fact"
    if "type mismatch" in text or "application type mismatch" in text or "expected type" in text:
        return "type_mismatch"
    if "stall" in text or "timed out" in text or "timeout" in text:
        return "stall_timeout"
    if ("returncode: 139" in text or "sigsegv" in text or "signal 11" in text
            or "returncode: 135" in text or "sigbus" in text or "signal 7" in text):
        return "lean_crash"
    if "tactic" in text or "unsolved goals" in text or "no goals to be solved" in text:
        return "tactic_failure"
    return "tactic_failure"


def _extract_declaration_blocks(text: str) -> list[dict[str, Any]]:
    matches = list(DECL_PATTERN.finditer(text))
    blocks: list[dict[str, Any]] = []
    for idx, match in enumerate(matches):
        start = match.start()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        block = text[start:end].strip("\n")
        blocks.append(
            {
                "keyword": match.group(1),
                "name": match.group(2),
                "block": block + "\n",
                "start": start,
                "end": end,
            }
        )
    return blocks


def _without_target_declaration(text: str, target_decl_name: str) -> str | None:
    blocks = _extract_declaration_blocks(text)
    target_blocks = [block for block in blocks if block["name"] == target_decl_name]
    if len(target_blocks) != 1:
        return None
    target = target_blocks[0]
    start = int(target["start"])
    end = int(target["end"])
    return (text[:start] + text[end:]).strip()


def _build_lemma_stage_validator(target_decl_name: str) -> Callable[[str], tuple[bool, str | None]]:
    def _validate(candidate_text: str) -> tuple[bool, str | None]:
        try:
            _ = extract_target_declaration_block(candidate_text, target_decl_name)
        except ValueError as exc:
            return False, str(exc)
        return True, None

    return _validate


def _render_diff(before: str, after: str) -> str:
    diff = difflib.unified_diff(
        before.splitlines(),
        after.splitlines(),
        fromfile="before.lean",
        tofile="after.lean",
        lineterm="",
    )
    return "\n".join(diff) + "\n"


def _copy_claude_raw(result: ClaudeRunResult, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(result.raw_output_path, destination)


def _write_mock_claude_raw(path: Path, candidate_text: str) -> None:
    payload = {"type": "result", "result": candidate_text, "mock": True}
    write_text(path, json.dumps(payload, ensure_ascii=True) + "\n")


def _collapse_ws(text: str) -> str:
    return " ".join(text.split())


class _noop_lock:
    """A no-op context manager that replaces a real Lock when not in parallel mode."""
    def __enter__(self) -> None:
        pass
    def __exit__(self, *args: object) -> None:
        pass


def _has_disallowed_proof_tokens(text: str) -> bool:
    return bool(re.search(r"\b(sorry|admit)\b", text))


def _diagnostic_lines(text: str, limit: int = 8) -> list[str]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return ["no diagnostics emitted"]
    return lines[:limit]


def lemma_id_to_path_token(lemma_id: str) -> str:
    text = lemma_id.strip() or "unnamed"
    encoded = quote(text, safe="_.-")
    return f"lemma_{encoded}"


def _strip_trailing_orthos_end(text: str) -> str:
    lines = text.splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    if lines and ORTHOS_END_PATTERN.match(lines[-1]):
        lines.pop()
        while lines and not lines[-1].strip():
            lines.pop()
    return "\n".join(lines)
