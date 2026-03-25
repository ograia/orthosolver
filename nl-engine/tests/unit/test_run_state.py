from __future__ import annotations

from nl_engine.api import run_state


def test_run_lock_does_not_auto_expire_without_release() -> None:
    problem_id = "prob_no_auto_expire"
    run_state.clear_stop_all_runs()
    run_state.release_problem_run(problem_id)

    assert run_state.acquire_problem_run(problem_id) is True
    assert run_state.acquire_problem_run(problem_id) is False
    assert run_state.is_problem_running(problem_id) is True

    run_state.release_problem_run(problem_id)
    assert run_state.is_problem_running(problem_id) is False


def test_global_stop_blocks_new_runs_and_marks_active_problem_for_stop() -> None:
    active_problem = "prob_active_stop"
    blocked_problem = "prob_blocked_stop"
    run_state.release_problem_run(active_problem)
    run_state.release_problem_run(blocked_problem)
    run_state.clear_stop_all_runs()

    assert run_state.acquire_problem_run(active_problem) is True
    running = run_state.request_stop_all_runs()
    assert active_problem in running
    assert run_state.is_problem_stop_requested(active_problem) is True
    assert run_state.acquire_problem_run(blocked_problem) is False

    run_state.release_problem_run(active_problem)
    run_state.clear_stop_all_runs()

    assert run_state.acquire_problem_run(blocked_problem) is True
    run_state.release_problem_run(blocked_problem)
