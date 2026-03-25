from __future__ import annotations

import threading

_RUN_GUARD_LOCK = threading.Lock()
_RUNNING_PROBLEMS: set[str] = set()
_ACTIVE_PROBLEM_WORK: dict[str, int] = {}
_STOP_REQUESTED_PROBLEMS: set[str] = set()
_GLOBAL_STOP_ACTIVE = False


def _increment_activity(problem_id: str) -> None:
    _ACTIVE_PROBLEM_WORK[problem_id] = _ACTIVE_PROBLEM_WORK.get(problem_id, 0) + 1


def _decrement_activity(problem_id: str) -> None:
    current = _ACTIVE_PROBLEM_WORK.get(problem_id, 0)
    if current <= 1:
        _ACTIVE_PROBLEM_WORK.pop(problem_id, None)
    else:
        _ACTIVE_PROBLEM_WORK[problem_id] = current - 1


def _maybe_clear_stop(problem_id: str) -> None:
    if problem_id not in _RUNNING_PROBLEMS and _ACTIVE_PROBLEM_WORK.get(problem_id, 0) <= 0:
        _STOP_REQUESTED_PROBLEMS.discard(problem_id)


def acquire_problem_run(problem_id: str) -> bool:
    with _RUN_GUARD_LOCK:
        if _GLOBAL_STOP_ACTIVE:
            return False
        if problem_id in _RUNNING_PROBLEMS:
            return False
        _RUNNING_PROBLEMS.add(problem_id)
        _increment_activity(problem_id)
        return True


def release_problem_run(problem_id: str) -> None:
    with _RUN_GUARD_LOCK:
        _RUNNING_PROBLEMS.discard(problem_id)
        _decrement_activity(problem_id)
        _maybe_clear_stop(problem_id)


def begin_problem_activity(problem_id: str) -> bool:
    with _RUN_GUARD_LOCK:
        if _GLOBAL_STOP_ACTIVE:
            return False
        _increment_activity(problem_id)
        return True


def end_problem_activity(problem_id: str) -> None:
    with _RUN_GUARD_LOCK:
        _decrement_activity(problem_id)
        _maybe_clear_stop(problem_id)


def is_problem_running(problem_id: str) -> bool:
    with _RUN_GUARD_LOCK:
        return problem_id in _RUNNING_PROBLEMS or _ACTIVE_PROBLEM_WORK.get(problem_id, 0) > 0


def has_any_running_problem() -> bool:
    with _RUN_GUARD_LOCK:
        return bool(_RUNNING_PROBLEMS or _ACTIVE_PROBLEM_WORK)


def list_running_problems() -> list[str]:
    with _RUN_GUARD_LOCK:
        return sorted(set(_RUNNING_PROBLEMS) | set(_ACTIVE_PROBLEM_WORK))


def request_problem_stop(problem_id: str) -> None:
    with _RUN_GUARD_LOCK:
        _STOP_REQUESTED_PROBLEMS.add(problem_id)


def request_stop_all_runs() -> list[str]:
    with _RUN_GUARD_LOCK:
        global _GLOBAL_STOP_ACTIVE
        _GLOBAL_STOP_ACTIVE = True
        active = set(_RUNNING_PROBLEMS) | set(_ACTIVE_PROBLEM_WORK)
        _STOP_REQUESTED_PROBLEMS.update(active)
        return sorted(active)


def clear_stop_all_runs() -> None:
    with _RUN_GUARD_LOCK:
        global _GLOBAL_STOP_ACTIVE
        _GLOBAL_STOP_ACTIVE = False
        _STOP_REQUESTED_PROBLEMS.clear()


def is_problem_stop_requested(problem_id: str) -> bool:
    with _RUN_GUARD_LOCK:
        return _GLOBAL_STOP_ACTIVE or problem_id in _STOP_REQUESTED_PROBLEMS


def reset_all_run_state() -> None:
    """Clear all in-memory run-state. Intended for test isolation."""
    with _RUN_GUARD_LOCK:
        global _GLOBAL_STOP_ACTIVE
        _GLOBAL_STOP_ACTIVE = False
        _RUNNING_PROBLEMS.clear()
        _ACTIVE_PROBLEM_WORK.clear()
        _STOP_REQUESTED_PROBLEMS.clear()


def is_global_stop_active() -> bool:
    with _RUN_GUARD_LOCK:
        return _GLOBAL_STOP_ACTIVE
