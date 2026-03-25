from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .artifact_io import RunPaths, write_json, write_text


@dataclass(frozen=True)
class FinalOutputBundle:
    result_path: Path
    summary_path: Path

    def to_dict(self) -> dict[str, str]:
        return {
            "result_path": str(self.result_path),
            "summary_path": str(self.summary_path),
        }


def write_fatal_output_bundle(
    *,
    run_paths: RunPaths,
    payload: dict[str, Any],
) -> FinalOutputBundle:
    final_dir = run_paths.run_root / "final"
    result_path = write_json(final_dir / "result.json", payload)
    summary_path = write_text(final_dir / "fatal_summary.md", render_fatal_summary_markdown(payload))
    return FinalOutputBundle(result_path=result_path, summary_path=summary_path)


def write_success_output_bundle(
    *,
    run_paths: RunPaths,
    payload: dict[str, Any],
    summary_markdown: str,
) -> FinalOutputBundle:
    final_dir = run_paths.run_root / "final"
    result_path = write_json(final_dir / "result.json", payload)
    summary_path = write_text(final_dir / "summary.md", summary_markdown.rstrip() + "\n")
    return FinalOutputBundle(result_path=result_path, summary_path=summary_path)


def render_fatal_summary_markdown(payload: dict[str, Any]) -> str:
    target_id = str(payload.get("target_id", "unknown"))
    decl_name = str(payload.get("decl_name", "unknown"))
    error_class = str(payload.get("error_class", "unknown_fatal"))
    message = str(payload.get("message", "fatal failure"))
    math_gap_description = str(payload.get("math_gap_description", "")).strip()
    evidence = payload.get("evidence", {})
    diagnostics = evidence.get("latest_diagnostics", []) if isinstance(evidence, dict) else []
    diagnostics_lines = []
    if isinstance(diagnostics, list):
        for item in diagnostics[:8]:
            diagnostics_lines.append(f"- {str(item)}")

    lines = [
        f"# Fatal Summary ({error_class})",
        "",
        f"- Failing lemma id: `{target_id}`",
        f"- Declaration: `{decl_name}`",
        "",
        "## What failed",
        message,
        "",
    ]
    if math_gap_description:
        lines.extend(
            [
                "## Mathematical gap",
                math_gap_description,
                "",
            ]
        )
    lines.extend(
        [
            "## Why this is not only a Lean syntax issue",
            "The phase-5 semantic guards rejected acceptance after bounded semantic checks and repair attempts.",
            "",
        ]
    )
    if diagnostics_lines:
        lines.extend(
            [
                "## Diagnostics",
                *diagnostics_lines,
                "",
            ]
        )
    artifacts = payload.get("artifacts", {})
    if isinstance(artifacts, dict):
        lines.extend(
            [
                "## Artifact locations",
                *(f"- `{key}`: `{value}`" for key, value in sorted(artifacts.items())),
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"
