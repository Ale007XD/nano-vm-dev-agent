"""
agent/programs.py
=================
PROGRAM_SPRINT — deterministic FSM pipeline for sprint execution.

Flow (one file per LLM call to avoid token limit):
  read_store → patch_store → write_store
  → read_handlers → patch_handlers → write_handlers
  → read_tools → patch_tools → write_tools
  → generate_test → write_test
  → run_mypy_all → mypy_guard
  → run_pytest → pytest_guard
  → notify_done

Each LLM step receives ONE file → returns ONE file as JSON {"path": "content"}.
This keeps each response under ~5k chars, well within token limits.
Test is generated last so it can reference all patched source files.
"""

from __future__ import annotations

PROGRAM_SPRINT: dict = {
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
                "Implement ONLY the store.py changes from the specification above.\n"
                "Rules:\n"
                "- Follow existing code style exactly.\n"
                "- Use 'from __future__ import annotations' (double underscores).\n"
                "- mypy --strict must pass (0 errors).\n"
                "- Return the COMPLETE file content (not a diff).\n\n"
                "## Output format — CRITICAL\n"
                "Return ONLY a valid JSON object with ONE key:\n"
                "{\"nano_vm_mcp/store.py\": \"...complete file content...\"}\n"
                "No markdown fences. No explanation. Pure JSON only."
            ),
            "output_key": "store_patch",
            "timeout_seconds": 300,
            "on_timeout": "fail",
        },
        {
            "id": "write_store",
            "type": "tool",
            "tool": "write_repo_files",
            "args": {"files_json": "$store_patch"},
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
                "Implement ONLY the handlers.py changes from the specification above.\n"
                "Rules:\n"
                "- Follow existing code style exactly.\n"
                "- Use 'from __future__ import annotations' (double underscores).\n"
                "- mypy --strict must pass (0 errors).\n"
                "- Return the COMPLETE file content (not a diff).\n\n"
                "## Output format — CRITICAL\n"
                "Return ONLY a valid JSON object with ONE key:\n"
                "{\"nano_vm_mcp/handlers.py\": \"...complete file content...\"}\n"
                "No markdown fences. No explanation. Pure JSON only."
            ),
            "output_key": "handlers_patch",
            "timeout_seconds": 300,
            "on_timeout": "fail",
        },
        {
            "id": "write_handlers",
            "type": "tool",
            "tool": "write_repo_files",
            "args": {"files_json": "$handlers_patch"},
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
                "Implement ONLY the tools.py changes from the specification above.\n"
                "Rules:\n"
                "- Follow existing code style exactly.\n"
                "- Use 'from __future__ import annotations' (double underscores).\n"
                "- mypy --strict must pass (0 errors).\n"
                "- Return the COMPLETE file content (not a diff).\n\n"
                "## Output format — CRITICAL\n"
                "Return ONLY a valid JSON object with ONE key:\n"
                "{\"nano_vm_mcp/tools.py\": \"...complete file content...\"}\n"
                "No markdown fences. No explanation. Pure JSON only."
            ),
            "output_key": "tools_patch",
            "timeout_seconds": 300,
            "on_timeout": "fail",
        },
        {
            "id": "write_tools",
            "type": "tool",
            "tool": "write_repo_files",
            "args": {"files_json": "$tools_patch"},
            "next_step": "generate_test",
        },

        # ----------------------------------------------------------------
        # test file (generated last — all source files already patched)
        # ----------------------------------------------------------------
        {
            "id": "generate_test",
            "type": "llm",
            "prompt": (
                "You are an expert Python developer.\n\n"
                "## Sprint specification\n$sprint_spec\n\n"
                "## Task\n"
                "Write the test file '$test_file' for the sprint above.\n"
                "Rules:\n"
                "- Use pytest (no unittest).\n"
                "- Use 'from __future__ import annotations' (double underscores).\n"
                "- Tests must be async-compatible if needed (pytest-asyncio).\n"
                "- Cover all test cases listed in spec (IP-01..IP-10).\n"
                "- Use tmp_path fixture for SQLite db.\n"
                "- Import from nano_vm_mcp.store and nano_vm_mcp.handlers.\n\n"
                "## Output format — CRITICAL\n"
                "Return ONLY a valid JSON object with ONE key:\n"
                "{\"$test_file\": \"...complete test file content...\"}\n"
                "No markdown fences. No explanation. Pure JSON only."
            ),
            "output_key": "test_patch",
            "timeout_seconds": 300,
            "on_timeout": "fail",
        },
        {
            "id": "write_test",
            "type": "tool",
            "tool": "write_repo_files",
            "args": {"files_json": "$test_patch"},
            "next_step": "run_mypy",
        },

        # ----------------------------------------------------------------
        # Validation
        # ----------------------------------------------------------------
        {
            "id": "run_mypy",
            "type": "tool",
            "tool": "run_mypy",
            "args": {"paths": "$target_files"},
            "next_step": "mypy_guard",
        },
        {
            "id": "mypy_guard",
            "type": "condition",
            "condition": "$run_mypy.output == 'OK'",
            "then": "run_tests",
            "otherwise": "reject_mypy",
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
            "otherwise": "reject_pytest",
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
