"""
tests/test_agent.py
===================
DA-01  read_repo_files: existing file → content with header
DA-02  read_repo_files: missing file → FileNotFoundError
DA-03  run_mypy: valid file → 'OK'
DA-04  run_mypy: file with type error → error string, not 'OK'
DA-05  run_pytest: passing test → 'PASS'
DA-06  run_pytest: failing test → failure string, not 'PASS'
DA-07  write_repo_files: writes files → content on disk matches
DA-08  write_repo_files: invalid JSON → raises ValueError
DA-09  PROGRAM_SPRINT happy path: MockLLM → trace.SUCCESS, notify_done called
DA-10  PROGRAM_SPRINT mypy fail: mock run_mypy returns error → REJECTED_MYPY
DA-11  PROGRAM_SPRINT pytest fail: mypy OK, pytest fails → REJECTED_PYTEST
DA-12  PROGRAM_SPRINT LLM timeout: SlowAdapter → trace.FAILED
DA-13  apply_search_replace_patch: single block → file patched correctly
DA-14  apply_search_replace_patch: multiple blocks → all applied sequentially
DA-15  apply_search_replace_patch: SEARCH not found → ValueError
DA-16  apply_search_replace_patch: SEARCH matches >1 times → ValueError
"""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from agent.programs import PROGRAM_SPRINT
from agent.tools import (
    apply_search_replace_patch,
    commit_patches,
    read_repo_files,
    rollback_patches,
    run_mypy,
    run_pytest,
    stage_patch,
    write_repo_files,
)

# ---------------------------------------------------------------------------
# Inline mock adapters (no real API needed)
# ---------------------------------------------------------------------------

class MockLLMAdapter:
    """Returns a fixed string or sequences of strings."""

    def __init__(self, response: str | list[str]) -> None:
        self._response = response
        self._idx = 0

    async def complete(self, messages: list[dict]) -> str:
        if isinstance(self._response, str):
            return self._response
        val = self._response[min(self._idx, len(self._response) - 1)]
        self._idx += 1
        return val


class SlowLLMAdapter:
    """Simulates a hanging LLM call."""

    async def complete(self, messages: list[dict]) -> str:
        await asyncio.sleep(60)
        return "never"


# ---------------------------------------------------------------------------
# Unit tests — tools (sync)
# ---------------------------------------------------------------------------

def test_da_01_read_repo_files_existing(tmp_path: Path) -> None:
    f = tmp_path / "sample.py"
    f.write_text("x = 1\n", encoding="utf-8")
    result = read_repo_files(json.dumps([str(f)]))
    assert f"### FILE: {f}" in result
    assert "x = 1" in result


def test_da_02_read_repo_files_missing() -> None:
    with pytest.raises(FileNotFoundError, match="not found"):
        read_repo_files(json.dumps(["/nonexistent/path/file.py"]))


def test_da_03_run_mypy_valid(valid_py: str) -> None:
    import subprocess as _sp
    with patch("agent.tools.subprocess.run") as mock_run:
        mock_run.return_value = _sp.CompletedProcess([], 0, stdout="", stderr="")
        result = run_mypy(json.dumps([valid_py]))
    assert result == "OK"


def test_da_04_run_mypy_invalid(invalid_py: str) -> None:
    import subprocess as _sp
    with patch("agent.tools.subprocess.run") as mock_run:
        mock_run.return_value = _sp.CompletedProcess(
            [], 1, stdout="error: Incompatible return value type", stderr=""
        )
        result = run_mypy(json.dumps([invalid_py]))
    assert result != "OK"
    assert "error" in result


def test_da_05_run_pytest_passing(passing_test: str) -> None:
    result = run_pytest(passing_test)
    assert result == "PASS"


def test_da_06_run_pytest_failing(failing_test: str) -> None:
    result = run_pytest(failing_test)
    assert result != "PASS"
    assert len(result) > 0


def test_da_07_write_repo_files(tmp_path: Path) -> None:
    f1 = str(tmp_path / "a.py")
    f2 = str(tmp_path / "b.py")
    result = write_repo_files(json.dumps({f1: "x = 1\n", f2: "y = 2\n"}))
    assert "WRITTEN" in result
    assert Path(f1).read_text() == "x = 1\n"
    assert Path(f2).read_text() == "y = 2\n"


def test_da_08_write_repo_files_invalid_json() -> None:
    with pytest.raises(ValueError, match="files_json"):
        write_repo_files("not json at all")


# ---------------------------------------------------------------------------
# Unit tests — apply_search_replace_patch (DA-13..DA-16)
# ---------------------------------------------------------------------------

def test_da_13_apply_single_block(tmp_path: Path) -> None:
    """Single S&R block patches the file correctly."""
    f = tmp_path / "store.py"
    f.write_text("def foo():\n    return 1\n", encoding="utf-8")

    patch_text = (
        "<<<SEARCH\n"
        "    return 1\n"
        "=======\n"
        "    return 42\n"
        ">>>REPLACE"
    )
    result = apply_search_replace_patch(str(f), patch_text)

    assert result == f"PATCHED: {f}"
    assert f.read_text() == "def foo():\n    return 42\n"


def test_da_14_apply_multiple_blocks(tmp_path: Path) -> None:
    """Multiple S&R blocks are applied sequentially."""
    f = tmp_path / "handlers.py"
    f.write_text(
        "STATUS = 'pending'\nNAME = 'old'\nVERSION = 1\n",
        encoding="utf-8",
    )

    patch_text = (
        "<<<SEARCH\n"
        "STATUS = 'pending'\n"
        "=======\n"
        "STATUS = 'success'\n"
        ">>>REPLACE\n"
        "<<<SEARCH\n"
        "NAME = 'old'\n"
        "=======\n"
        "NAME = 'new'\n"
        ">>>REPLACE"
    )
    apply_search_replace_patch(str(f), patch_text)

    content = f.read_text()
    assert "STATUS = 'success'" in content
    assert "NAME = 'new'" in content
    assert "VERSION = 1" in content  # untouched


def test_da_15_apply_search_not_found(tmp_path: Path) -> None:
    """SEARCH block that doesn't exist in the file raises ValueError."""
    f = tmp_path / "tools.py"
    f.write_text("x = 1\n", encoding="utf-8")

    patch_text = (
        "<<<SEARCH\n"
        "this_line_does_not_exist\n"
        "=======\n"
        "replacement\n"
        ">>>REPLACE"
    )
    with pytest.raises(ValueError, match="SEARCH not found"):
        apply_search_replace_patch(str(f), patch_text)

    # File must be untouched on failure of a later block
    assert f.read_text() == "x = 1\n"


def test_da_16_apply_search_ambiguous(tmp_path: Path) -> None:
    """SEARCH block matching >1 times raises ValueError."""
    f = tmp_path / "ambiguous.py"
    f.write_text("x = 1\nx = 1\n", encoding="utf-8")

    patch_text = (
        "<<<SEARCH\n"
        "x = 1\n"
        "=======\n"
        "x = 2\n"
        ">>>REPLACE"
    )
    with pytest.raises(ValueError, match="matches 2 times"):
        apply_search_replace_patch(str(f), patch_text)


# ---------------------------------------------------------------------------
# Integration tests — PROGRAM_SPRINT FSM (async, MockLLMAdapter)
# ---------------------------------------------------------------------------

def _make_sr_patch(file_path: str) -> str:
    """Return a valid S&R patch string for the given file (no-op replacement)."""
    content = Path(file_path).read_text()
    # Replace the first line with itself — trivial but valid single-match patch
    first_line = content.split("\n")[0]
    return (
        f"<<<SEARCH\n"
        f"{first_line}\n"
        f"=======\n"
        f"{first_line}\n"
        f">>>REPLACE"
    )


def _build_vm(
    tmp_path: Path,
    mypy_result: str = "OK",
    pytest_result: str = "PASS",
) -> tuple[Any, Any, dict[str, str]]:
    """Build ExecutionVM + Program + context for integration tests."""
    from nano_vm.models import Program
    from nano_vm.vm import ExecutionVM

    # Create minimal source files so read_repo_files doesn't raise
    store    = tmp_path / "nano_vm_mcp" / "store.py"
    handlers = tmp_path / "nano_vm_mcp" / "handlers.py"
    tools_f  = tmp_path / "nano_vm_mcp" / "tools.py"
    for f in (store, handlers, tools_f):
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(f"# {f.name}\nVERSION = 1\n", encoding="utf-8")

    test_f = tmp_path / "tests" / "test_sprint.py"
    test_f.parent.mkdir(parents=True, exist_ok=True)
    test_f.write_text("def test_ok():\n    assert True\n", encoding="utf-8")

    # LLM response sequence: 3 S&R patches + 1 test file JSON
    test_json = json.dumps({str(test_f): "def test_ok():\n    assert True\n"})
    llm_responses = [
        _make_sr_patch(str(store)),
        _make_sr_patch(str(handlers)),
        _make_sr_patch(str(tools_f)),
        test_json,
    ]

    vm = ExecutionVM(
        llm=MockLLMAdapter(llm_responses),
        tools={
            "read_repo_files":            read_repo_files,
            "apply_search_replace_patch": apply_search_replace_patch,
            "stage_patch":                stage_patch,
            "validate_staged_mypy":       lambda paths, **kw: mypy_result,
            "commit_patches":             commit_patches,
            "rollback_patches":           rollback_patches,
            "git_checkout_files":         lambda paths, **kw: f"RESTORED: {paths}",
            "run_mypy":                   lambda paths, **kw: mypy_result,
            "run_pytest":                 lambda test_file, **kw: pytest_result,
            "write_repo_files":           write_repo_files,
            "notify_rejected_mypy":       lambda **kw: "REJECTED_MYPY",
            "notify_rejected_pytest":     lambda **kw: "REJECTED_PYTEST",
            "notify_done":                lambda **kw: "DONE",
        },
    )
    program = Program.from_dict(PROGRAM_SPRINT)

    abs_repo   = str(tmp_path)
    store_file = str(store)
    handlers_file = str(handlers)
    tools_file = str(tools_f)

    context: dict[str, str] = {
        "sprint_spec":   "add field y: int = 2",
        "target_files":  json.dumps([store_file, handlers_file, tools_file]),
        "test_file":     str(test_f),
        "repo_path":     abs_repo,
        "store_path":    json.dumps([store_file]),
        "handlers_path": json.dumps([handlers_file]),
        "tools_path":    json.dumps([tools_file]),
        "store_file":    store_file,
        "handlers_file": handlers_file,
        "tools_file":    tools_file,
    }

    return vm, program, context


@pytest.mark.asyncio
async def test_da_09_happy_path(tmp_path: Path) -> None:
    """Happy path: S&R patches apply, mypy OK, pytest PASS → SUCCESS, DONE."""
    from nano_vm.models import TraceStatus

    vm, program, context = _build_vm(tmp_path)
    trace = await vm.run(program, context=context)

    assert trace.status == TraceStatus.SUCCESS
    outputs = [s.output for s in trace.steps]
    assert "DONE" in outputs


@pytest.mark.asyncio
async def test_da_10_mypy_fail(tmp_path: Path) -> None:
    """mypy fails → REJECTED_MYPY."""
    from nano_vm.models import TraceStatus

    vm, program, context = _build_vm(
        tmp_path, mypy_result="error: Incompatible return value"
    )
    trace = await vm.run(program, context=context)

    assert trace.status == TraceStatus.SUCCESS
    outputs = [s.output for s in trace.steps]
    assert "REJECTED_MYPY" in outputs
    assert "DONE" not in outputs


@pytest.mark.asyncio
async def test_da_11_pytest_fail(tmp_path: Path) -> None:
    """mypy OK but pytest fails → REJECTED_PYTEST."""
    from nano_vm.models import TraceStatus

    vm, program, context = _build_vm(
        tmp_path, pytest_result="FAILED: assert 1 == 2"
    )
    trace = await vm.run(program, context=context)

    assert trace.status == TraceStatus.SUCCESS
    outputs = [s.output for s in trace.steps]
    assert "REJECTED_PYTEST" in outputs
    assert "DONE" not in outputs


@pytest.mark.asyncio
async def test_da_12_llm_timeout(tmp_path: Path) -> None:
    """LLM hangs on first patch step → timeout → trace.FAILED."""
    from nano_vm.models import Program, TraceStatus
    from nano_vm.vm import ExecutionVM

    store = tmp_path / "nano_vm_mcp" / "store.py"
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text("# store.py\nVERSION = 1\n", encoding="utf-8")

    # Patch patch_store step with tiny timeout
    program_dict = copy.deepcopy(PROGRAM_SPRINT)
    for step in program_dict["steps"]:
        if step["id"] == "patch_store":
            step["timeout_seconds"] = 0.05
            step["on_timeout"] = "fail"
            step.pop("on_error", None)  # disable retry so test is fast

    vm = ExecutionVM(
        llm=SlowLLMAdapter(),
        tools={
            "read_repo_files":            read_repo_files,
            "apply_search_replace_patch": apply_search_replace_patch,
            "stage_patch":                stage_patch,
            "validate_staged_mypy":       lambda paths, **kw: "OK",
            "commit_patches":             commit_patches,
            "rollback_patches":           rollback_patches,
            "git_checkout_files":         lambda paths, **kw: "RESTORED:",
            "run_mypy":                   lambda paths, **kw: "OK",
            "run_pytest":                 lambda test_file, **kw: "PASS",
            "write_repo_files":           write_repo_files,
            "notify_rejected_mypy":       lambda **kw: "REJECTED_MYPY",
            "notify_rejected_pytest":     lambda **kw: "REJECTED_PYTEST",
            "notify_done":                lambda **kw: "DONE",
        },
    )
    program = Program.from_dict(program_dict)
    trace = await vm.run(program, context={
        "sprint_spec":   "test",
        "target_files":  json.dumps([str(store)]),
        "test_file":     str(tmp_path / "test_sprint.py"),
        "repo_path":     str(tmp_path),
        "store_path":    json.dumps([str(store)]),
        "handlers_path": json.dumps([str(store)]),
        "tools_path":    json.dumps([str(store)]),
        "store_file":    str(store),
        "handlers_file": str(store),
        "tools_file":    str(store),
    })

    assert trace.status == TraceStatus.FAILED
    assert trace.error is not None
    assert "timed out" in trace.error
