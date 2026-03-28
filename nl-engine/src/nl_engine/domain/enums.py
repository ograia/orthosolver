from enum import StrEnum


class ProblemStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    PAUSED = "paused"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class InputMode(StrEnum):
    NL_ONLY = "nl_only"
    LEAN_ONLY = "lean_only"
    BOTH = "both"


class VerificationLevel(StrEnum):
    FORMAL = "formal"
    NL_ONLY = "nl_only"


class NodeKind(StrEnum):
    THEOREM = "theorem"
    LEMMA = "lemma"


class NodeStatus(StrEnum):
    OPEN = "open"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class LlmVettingStatus(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED_MINOR = "rejected_minor"
    REJECTED_FATAL = "rejected_fatal"


class LeanAssemblyStatus(StrEnum):
    PENDING = "pending"
    SUCCESS = "success"
    FATAL = "fatal"
    SKIPPED = "skipped"


class ControllerStatus(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    STANDBY = "standby"
    FAILED = "failed"
    SUCCEEDED = "succeeded"


class StatementStatus(StrEnum):
    UNVETTED = "unvetted"
    PLAUSIBLE = "plausible"
    SUSPECT = "suspect"
    FALSE = "false"
    FORMALIZED = "formalized"


class ProofStatus(StrEnum):
    OPEN = "open"
    PROOF_FOUND = "proof_found"
    PROOF_FLAWED = "proof_flawed"
    PROOF_VETTED = "proof_vetted"
    PROOF_FORMALIZED = "proof_formalized"
    NL_ACCEPTED = "nl_accepted"
    FAILED = "failed"
    PROOF_EXHAUSTED = "proof_exhausted"


class RoutingStatus(StrEnum):
    OPEN = "open"
    RETRY_SOLVER = "retry_solver"
    DECOMPOSE_FURTHER = "decompose_further"
    SPLIT_EXISTING_PROOF = "split_existing_proof"
    READY_FOR_LEAN = "ready_for_lean"
    SEND_TO_LEAN = "send_to_lean"
    BLOCKED = "blocked"
    DONE = "done"


class LeanJobMode(StrEnum):
    CHECK_ASSEMBLY = "check_assembly"
    PREPARE_TRACK = "prepare_track"
    FORMALIZE_LEMMA = "formalize_lemma"
    FORMALIZE_LEMMA_FROM_NL = "formalize_lemma_from_nl"
    SPLIT_PROOF_INTO_SUBLEMMAS = "split_proof_into_sublemmas"
    ASSEMBLE_ROOT = "assemble_root"
    ASSEMBLE_ROOT_FROM_TRACK = "assemble_root_from_track"
    CHECK_STATEMENT_PLAUSIBILITY = "check_statement_plausibility"


class LeanJobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCESS = "success"
    REPAIRABLE = "repairable"
    FATAL = "fatal"
    CANCELLED = "cancelled"


class LeanErrorClass(StrEnum):
    SYNTAX = "syntax"
    TYPE_MISMATCH = "type_mismatch"
    MISSING_IMPORT = "missing_import"
    MISSING_LIBRARY_FACT = "missing_library_fact"
    TACTIC_FAILURE = "tactic_failure"
    FALSE_LEMMA_SUSPECTED = "false_lemma_suspected"
    MAJOR_PROOF_GAP = "major_proof_gap"
    ASSEMBLY_INVALID = "assembly_invalid"
    ASSEMBLY_COMPOSITION_FAILURE = "assembly_composition_failure"
    BAD_STATEMENT_TRANSLATION = "bad_statement_translation"
    ENVIRONMENT_MISMATCH = "environment_mismatch"
    UNKNOWN_FATAL = "unknown_fatal"


class LeanErrorScope(StrEnum):
    STATEMENT = "statement"
    PROOF = "proof"
    ASSEMBLY = "assembly"
    ENVIRONMENT = "environment"
    INFRASTRUCTURE = "infrastructure"


class FailureReason(StrEnum):
    LEMMA_FALSE = "lemma_false"
    PROOF_EXHAUSTED = "proof_exhausted"
    DEAD_FRONTIER = "dead_frontier"
    LEAN_DIFFICULTY = "lean_difficulty"
    ASSEMBLY_COMPOSITION_FAILURE = "assembly_composition_failure"
    GLOBAL_TIMEOUT = "global_timeout"
    UNKNOWN = "unknown"


class ExecutionStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING = "waiting"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"


class ExecutionDesiredState(StrEnum):
    RUNNING = "running"
    STOPPED = "stopped"


class VetterRecommendedAction(StrEnum):
    SEND_TO_LEAN = "send_to_lean"
    RETRY_SOLVER = "retry_solver"
    DECOMPOSE_CURRENT_LEMMA = "decompose_current_lemma"
    FLAG_SUSPECTED_FALSE = "flag_suspected_false"
