"""
agent/programs.py
=================
PROGRAM_SPRINT — deterministic FSM pipeline for sprint execution.

Flow:
  read_files → generate_patch (llm) → mypy_guard
                                           ↓ OK          ↓ fail
                                       run_tests     reject_mypy (terminal)
                                           ↓ PASS        ↓ fail
                                       write_files   reject_pytest (terminal)
                                           ↓
                                        notify_done (terminal)

Guards:
  - mypy_guard:   $run_mypy.output == 'OK'
  - pytest_guard: $run_pytest.output == 'PASS'

No patch reaches disk without both guards passing.
Terminal leaf steps are placed BEFORE inline-chain steps per DSL convention.
"""

from __future__ import annotations

PROGRAM_SPRINT: dict = {
    "name": "sprint_execution",
    "steps": [
        # ------------------------------------------------------------------
        # Main flow (FSM starts from index 0 = read_files)
        # ------------------------------------------------------------------

        # Step 1: read target files from repo
        {
            "id": "read_files",
            "type": "tool",
            "tool": "read_repo_files",
            "args": {"paths": "$target_files"},
        },

        # Step 2: LLM generates patch
        {
            "id": "generate_patch",
            "type": "llm",
            "prompt": (
                "You are an expert Python developer working on a deterministic FSM runtime.\n\n"
                "## Sprint specification\n"
                "$sprint_spec\n\n"
                "## Current files\n"
                "$read_files.output\n\n"
                "## Task\n"
                "Implement the specification above.\n"
                "- Follow existing code style exactly.\n"
                "- mypy --strict must pass (no type: ignore unless already present).\n"
                "- Do not change any public API unless the spec explicitly requires it.\n"
                "- File: $test_file must contain tests for every deliverable.\n\n"
                "## Output format\n"
                "Return ONLY a valid JSON object mapping filename to complete file content.\n"
                "Example: {\"nano_vm/models.py\": \"...full content...\", "
                "\"tests/test_sprint.py\": \"...full content...\"}\n"
                "No markdown fences. No explanation. No preamble. Pure JSON only."
            ),
            "output_key": "patch",
            "timeout_seconds": 120,
            "on_timeout": "fail",
        },

        # Step 3: run mypy on patched files
        {
            "id": "run_mypy",
            "type": "tool",
            "tool": "run_mypy",
            "args": {"paths": "$target_files"},
            "next_step": "mypy_guard",
        },

        # Step 4: guard — mypy must be OK
        {
            "id": "mypy_guard",
            "type": "condition",
            "condition": "$run_mypy.output == 'OK'",
            "then": "run_tests",
            "otherwise": "reject_mypy",
        },

        # Step 5: run pytest on test file
        {
            "id": "run_tests",
            "type": "tool",
            "tool": "run_pytest",
            "args": {"test_file": "$test_file"},
            "next_step": "pytest_guard",
        },

        # Step 6: guard — pytest must pass
        {
            "id": "pytest_guard",
            "type": "condition",
            "condition": "$run_tests.output == 'PASS'",
            "then": "write_files",
            "otherwise": "reject_pytest",
        },

        # Step 7: write patched files to disk
        {
            "id": "write_files",
            "type": "tool",
            "tool": "write_repo_files",
            "args": {"files_json": "$generate_patch.output"},
            "next_step": "notify_done",
        },

        # ------------------------------------------------------------------
        # Terminal leaf steps (after main flow — FSM jumps here by id)
        # ------------------------------------------------------------------
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
    ],
}
