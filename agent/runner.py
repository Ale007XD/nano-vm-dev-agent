"""
agent/runner.py
===============
Public API for the nano-vm dev-agent.

Usage:
    import asyncio
    from agent.runner import run_sprint

    trace = asyncio.run(run_sprint(
        sprint_spec=open("sprint.json").read(),
        target_files=["nano_vm/models.py", "nano_vm/vm.py"],
        test_file="tests/test_sprint.py",
    ))
    print(trace.status)   # TraceStatus.SUCCESS or FAILED
"""

from __future__ import annotations

import json
import os

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
) -> Trace:
    """Execute a sprint using the deterministic FSM pipeline.

    Args:
        sprint_spec:  Sprint specification as JSON or plain text string.
        target_files: List of source file paths to read and patch.
        test_file:    Path to the test file for this sprint.
        llm_model:    LiteLLM model string (default: claude-sonnet-4-20250514).
        repo_path:    Root path of the repository (default: current directory).

    Returns:
        Trace — full FSM execution trace.
        trace.status == TraceStatus.SUCCESS → files written to disk.
        trace.status == TraceStatus.FAILED  → nothing written, trace.error has details.
    """
    tools = {
        "read_repo_files":      read_repo_files,
        "run_mypy":             run_mypy,
        "run_pytest":           run_pytest,
        "write_repo_files":     write_repo_files,
        "notify_rejected_mypy":  notify_rejected_mypy,
        "notify_rejected_pytest": notify_rejected_pytest,
        "notify_done":          notify_done,
    }

    adapter = LiteLLMAdapter(llm_model)
    vm = ExecutionVM(llm=adapter, tools=tools)
    program = Program.from_dict(PROGRAM_SPRINT)

    # Resolve target_files relative to repo_path
    resolved = [
        p if os.path.isabs(p) else os.path.join(repo_path, p)
        for p in target_files
    ]
    test_file_resolved = (
        test_file if os.path.isabs(test_file) else os.path.join(repo_path, test_file)
    )

    context: dict[str, str] = {
        "sprint_spec":  sprint_spec,
        "target_files": json.dumps(resolved),
        "test_file":    test_file_resolved,
        "repo_path":    repo_path,
    }

    return await vm.run(program, context=context)
