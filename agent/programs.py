"""
agent/programs.py
=================
PROGRAM_SPRINT — deterministic FSM pipeline for sprint execution.

Flow (DA-4 transactional patching):

  read_store → patch_store(llm) → stage_store       ← in-memory, disk untouched
  read_handlers → patch_handlers(llm) → stage_handlers
  read_tools → patch_tools(llm) → stage_tools
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

PROGRAM_SPRINT: dict[str, Any] = {
    "name": "sprint_execution",
    "steps": [

        # ----------------------------------------------------------------
        # store.py
        # ----------------------------------------------------------------
        {
            "id": "read_store",
            "type": "tool",
            "tool": "read_repo_files",
            "args": {"paths": "$store_path"},
        },
        {
            "id": "patch_store",
            "type": "llm",
            "prompt": (
                "You are an expert Python developer.\n\n"
                "## Sprint specification\n$sprint_spec\n\n"
                "## Current file: nano_vm_mcp/store.py\n$read_store.output\n\n"
                "## Task\n"
                "Produce a Search&Replace patch for ONLY the store.py changes "
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
            ),
            "output_key": "store_patch",
            "on_error": "retry",
            "max_retries": 2,
            "timeout_seconds": 120,
            "on_timeout": "fail",
        },
        {
            "id": "stage_store",
            "type": "tool",
            "tool": "stage_patch",
            "args": {
                "file_path": "$store_file",
                "patch_text": "$store_patch",
            },
            "on_error": "retry",
            "max_retries": 2,
            "next_step": "read_handlers",
        },

        # ----------------------------------------------------------------
        # handlers.py
        # ----------------------------------------------------------------
        {
            "id": "read_handlers",
            "type": "tool",
            "tool": "read_repo_files",
            "args": {"paths": "$handlers_path"},
        },
        {
            "id": "patch_handlers",
            "type": "llm",
            "prompt": (
                "You are an expert Python developer.\n\n"
                "## Sprint specification\n$sprint_spec\n\n"
                "## Current file: nano_vm_mcp/handlers.py\n$read_handlers.output\n\n"
                "## Task\n"
                "Produce a Search&Replace patch for ONLY the handlers.py changes "
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
            ),
            "output_key": "handlers_patch",
            "on_error": "retry",
            "max_retries": 2,
            "timeout_seconds": 120,
            "on_timeout": "fail",
        },
        {
            "id": "stage_handlers",
            "type": "tool",
            "tool": "stage_patch",
            "args": {
                "file_path": "$handlers_file",
                "patch_text": "$handlers_patch",
            },
            "on_error": "retry",
            "max_retries": 2,
            "next_step": "read_tools",
        },

        # ----------------------------------------------------------------
        # tools.py
        # ----------------------------------------------------------------
        {
            "id": "read_tools",
            "type": "tool",
            "tool": "read_repo_files",
            "args": {"paths": "$tools_path"},
        },
        {
            "id": "patch_tools",
            "type": "llm",
            "prompt": (
                "You are an expert Python developer.\n\n"
                "## Sprint specification\n$sprint_spec\n\n"
                "## Current file: nano_vm_mcp/tools.py\n$read_tools.output\n\n"
                "## Task\n"
                "Produce a Search&Replace patch for ONLY the tools.py changes "
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
            ),
            "output_key": "tools_patch",
            "on_error": "retry",
            "max_retries": 2,
            "timeout_seconds": 120,
            "on_timeout": "fail",
        },
        {
            "id": "stage_tools",
            "type": "tool",
            "tool": "stage_patch",
            "args": {
                "file_path": "$tools_file",
                "patch_text": "$tools_patch",
            },
            "on_error": "retry",
            "max_retries": 2,
            "next_step": "generate_test",
        },

        # ----------------------------------------------------------------
        # test file — written to disk directly (disposable)
        # ----------------------------------------------------------------
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
                "- Use tmp_path fixture for SQLite db.\n"
                "- Import from nano_vm_mcp.store and nano_vm_mcp.handlers.\n\n"
                "## Output format — CRITICAL\n"
                "Return ONLY a valid JSON object with ONE key:\n"
                "{\"$test_file\": \"...complete test file content...\"}\n"
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

        # ----------------------------------------------------------------
        # mypy in tmpdir — disk untouched
        # ----------------------------------------------------------------
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

        # ----------------------------------------------------------------
        # mypy fail path — rollback buffer, disk never touched
        # ----------------------------------------------------------------
        {
            "id": "do_rollback_mypy",
            "type": "tool",
            "tool": "rollback_patches",
            "next_step": "reject_mypy",
        },

        # ----------------------------------------------------------------
        # commit — flush buffer to disk
        # ----------------------------------------------------------------
        {
            "id": "commit",
            "type": "tool",
            "tool": "commit_patches",
            "next_step": "run_tests",
        },

        # ----------------------------------------------------------------
        # pytest — against committed files on disk
        # ----------------------------------------------------------------
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

        # ----------------------------------------------------------------
        # pytest fail path — git checkout restores committed files to HEAD
        # ----------------------------------------------------------------
        {
            "id": "do_rollback_pytest",
            "type": "tool",
            "tool": "git_checkout_files",
            "args": {"paths": "$target_files"},
            "next_step": "reject_pytest",
        },

        # ----------------------------------------------------------------
        # Terminal leaf steps
        # ----------------------------------------------------------------
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
