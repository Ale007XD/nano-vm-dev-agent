"""
agent/runner.py
===============
Public API for the nano-vm dev-agent.
"""

from __future__ import annotations

import json
import os
from typing import Any

from nano_vm.adapters.litellm_adapter import LiteLLMAdapter
from nano_vm.models import Program, Trace
from nano_vm.vm import ExecutionVM

from .programs import PROGRAM_SPRINT
from .tools import (
    notify_done,
    notify_rejected_mypy,
    notify_rejected_pytest,
    read_repo_files,
    run_mypy,
    run_pytest,
    write_repo_files,
)

_DEFAULT_MODEL = "claude-sonnet-4-20250514"


async def run_sprint(
    sprint_spec: str,
    target_files: list[str],
    test_file: str,
    llm_model: str = _DEFAULT_MODEL,
    repo_path: str = ".",
    adapter_kwargs: dict[str, Any] | None = None,
) -> Trace:
    tools = {
        "read_repo_files":        read_repo_files,
        "run_mypy":               run_mypy,
        "run_pytest":             run_pytest,
        "write_repo_files":       write_repo_files,
        "notify_rejected_mypy":   notify_rejected_mypy,
        "notify_rejected_pytest": notify_rejected_pytest,
        "notify_done":            notify_done,
    }

    extra: dict[str, Any] = adapter_kwargs or {}
    adapter = LiteLLMAdapter(llm_model, **extra)
    vm = ExecutionVM(llm=adapter, tools=tools)
    program = Program.from_dict(PROGRAM_SPRINT)

    # Resolve all paths to absolute
    abs_repo = os.path.abspath(repo_path)
    resolved = [
        p if os.path.isabs(p) else os.path.join(abs_repo, p)
        for p in target_files
    ]
    test_file_resolved = (
        test_file if os.path.isabs(test_file) else os.path.join(abs_repo, test_file)
    )

    context: dict[str, str] = {
        "sprint_spec":  sprint_spec,
        "target_files": json.dumps(resolved),
        "test_file":    test_file_resolved,
        "repo_path":    abs_repo,
        # Individual file paths for per-file LLM steps
        "store_path":    json.dumps([os.path.join(abs_repo, "nano_vm_mcp/store.py")]),
        "handlers_path": json.dumps([os.path.join(abs_repo, "nano_vm_mcp/handlers.py")]),
        "tools_path":    json.dumps([os.path.join(abs_repo, "nano_vm_mcp/tools.py")]),
    }

    return await vm.run(program, context=context)
