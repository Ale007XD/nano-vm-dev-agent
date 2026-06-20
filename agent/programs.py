"""
agent/programs.py
=================
build_program_sprint(n) — generates a deterministic FSM pipeline for sprint execution
with dynamic per-file read/patch/stage steps for N target files.

Flow (DA-4 transactional patching, generalized for N files):

  For each file i in range(n):
    read_file_i → patch_file_i(llm) → stage_file_i  ← in-memory, disk untouched

  generate_test(llm) → write_test                   ← test written to disk (disposable)

  validate_staged_mypy                              ← mypy in tmpdir, disk untouched
  mypy_guard
    ↓ fail → rollback_patches → reject_mypy         ← buffer cleared, disk clean
    ↓ pass → commit_patches                         ← flush buffer to disk

  run_tests                                         ← pytest against committed files
  pytest_guard
    ↓ fail → git_checkout_files → reject_pytest     ← git restores HEAD
    ↓ pass → notify_done

Guarantees:
- Disk is never touched by source patches until mypy passes.
- If mypy fails: rollback_patches() — disk remains at HEAD.
- If pytest fails: git_checkout_files() restores committed files to HEAD.
- Test file is written directly (disposable — safe to overwrite on retry).
"""

from __future__ import annotations

from typing import Any

_PATCH_PROMPT_TEMPLATE = (
    "You are an expert Python developer.\n\n"
    "## Sprint specification\n$sprint_spec\n\n"
    "## Current file: $file_{i}_file\n$read_{i}.output\n\n"
    "## Task\n"
    "Produce a Search&Replace patch for ONLY the changes to this file "
    "described in the specification above.\n\n"
    "Rules:\n"
    "- Each SEARCH block must match exactly once in the file shown above.\n"
    "- Preserve indentation and code style exactly.\n"
    "- mypy --strict must pass after the patch (0 errors).\n"
    "- Output ONLY patch blocks — no explanation, no markdown prose.\n\n"
    "## Output format — CRITICAL\n"
    "Return one or more blocks in this exact format:\n\n"
    "<<<SEARCH\n"
    "<exact lines from current file>\n"
    "=======\n"
    "<replacement lines>\n"
    ">>>REPLACE\n\n"
    "Multiple blocks are allowed. No other text."
)

_TAIL_STEPS: list[dict[str, Any]] = [
    {
        "id": "generate_test",
        "type": "llm",
        "prompt": (
            "You are an expert Python developer.\n\n"
            "## Sprint specification\n$sprint_spec\n\n"
            "## Task\n"
            "Write the complete test file '$test_file' for the sprint above.\n\n"
            "Rules:\n"
            "- Use pytest (no unittest).\n"
            "- Use 'from __future__ import annotations' (double underscores).\n"
            "- Tests must be async-compatible if needed (pytest-asyncio).\n"
            "- Cover all test cases listed in spec.\n"
            "- Use tmp_path fixture for SQLite db.\n\n"
            "## Output format — CRITICAL\n"
            "Return ONLY a valid JSON object with ONE key:\n"
            '{"$test_file": "...complete test file content..."}\n'
            "No markdown fences. No explanation. Pure JSON only."
        ),
        "output_key": "test_patch",
        "timeout_seconds": 120,
        "on_timeout": "fail",
    },
    {
        "id": "write_test",
        "type": "tool",
        "tool": "write_repo_files",
        "args": {"files_json": "$test_patch"},
        "next_step": "validate_mypy",
    },
    {
        "id": "validate_mypy",
        "type": "tool",
        "tool": "validate_staged_mypy",
        "args": {"paths": "$target_files"},
        "next_step": "mypy_guard",
    },
    {
        "id": "mypy_guard",
        "type": "condition",
        "condition": "$validate_mypy.output == 'OK'",
        "then": "commit",
        "otherwise": "do_rollback_mypy",
    },
    {
        "id": "do_rollback_mypy",
        "type": "tool",
        "tool": "rollback_patches",
        "next_step": "reject_mypy",
    },
    {
        "id": "commit",
        "type": "tool",
        "tool": "commit_patches",
        "next_step": "run_tests",
    },
    {
        "id": "run_tests",
        "type": "tool",
        "tool": "run_pytest",
        "args": {"test_file": "$test_file"},
        "next_step": "pytest_guard",
    },
    {
        "id": "pytest_guard",
        "type": "condition",
        "condition": "$run_tests.output == 'PASS'",
        "then": "notify_done",
        "otherwise": "do_rollback_pytest",
    },
    {
        "id": "do_rollback_pytest",
        "type": "tool",
        "tool": "git_checkout_files",
        "args": {"paths": "$target_files"},
        "next_step": "reject_pytest",
    },
    {
        "id": "notify_done",
        "type": "tool",
        "tool": "notify_done",
        "is_terminal": True,
    },
    {
        "id": "reject_mypy",
        "type": "tool",
        "tool": "notify_rejected_mypy",
        "is_terminal": True,
    },
    {
        "id": "reject_pytest",
        "type": "tool",
        "tool": "notify_rejected_pytest",
        "is_terminal": True,
    },
]


def _file_steps(index: int, next_after: str) -> list[dict[str, Any]]:
    prompt = _PATCH_PROMPT_TEMPLATE.format(i=index)
    return [
        {
            "id": f"read_{index}",
            "type": "tool",
            "tool": "read_repo_files",
            "args": {"paths": f"$file_{index}_paths"},
        },
        {
            "id": f"patch_{index}",
            "type": "llm",
            "prompt": prompt,
            "output_key": f"file_{index}_patch",
            "on_error": "retry",
            "max_retries": 2,
            "timeout_seconds": 120,
            "on_timeout": "fail",
        },
        {
            "id": f"stage_{index}",
            "type": "tool",
            "tool": "stage_patch",
            "args": {
                "file_path": f"$file_{index}_file",
                "patch_text": f"$file_{index}_patch",
            },
            "on_error": "retry",
            "max_retries": 2,
            "next_step": next_after,
        },
    ]


def build_program_sprint(num_files: int) -> dict[str, Any]:
    if num_files < 1:
        raise ValueError("build_program_sprint: num_files must be >= 1")

    steps: list[dict[str, Any]] = []
    for i in range(num_files):
        next_after = f"read_{i + 1}" if i + 1 < num_files else "generate_test"
        steps.extend(_file_steps(i, next_after))
    steps.extend(_TAIL_STEPS)

    return {"name": "sprint_execution", "steps": steps}


PROGRAM_SPRINT: dict[str, Any] = build_program_sprint(3)
