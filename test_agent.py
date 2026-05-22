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
DA-12  PROGRAM_SPRINT LLM timeout: SlowAdapter → trace.FAILED, 'timed out' in error
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from agent.tools import (
    read_repo_files,
    run_mypy,
    run_pytest,
    write_repo_files,
)
from agent.programs import PROGRAM_SPRINT


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
# Integration tests — PROGRAM_SPRINT FSM (async, MockLLMAdapter)
# ---------------------------------------------------------------------------

def _make_patch_json(tmp_path: Path) -> tuple[str, str, str]:
    """Create a target file and test file; return (target_path, test_path, patch_json)."""
    target = tmp_path / "target.py"
    target.write_text("x: int = 0\n", encoding="utf-8")
    test_f = tmp_path / "test_sprint.py"
    test_f.write_text("def test_ok():\n    assert 1 + 1 == 2\n", encoding="utf-8")

    patch = json.dumps({
        str(target): "x: int = 1\n",
        str(test_f): "def test_ok():\n    assert 1 + 1 == 2\n",
    })
    return str(target), str(test_f), patch


@pytest.mark.asyncio
async def test_da_09_happy_path(tmp_path: Path) -> None:
    """Happy path: LLM returns valid patch, mypy OK, pytest PASS → SUCCESS."""
    from nano_vm.models import Program, TraceStatus
    from nano_vm.vm import ExecutionVM

    target, test_f, patch_json = _make_patch_json(tmp_path)

    vm = ExecutionVM(
        llm=MockLLMAdapter(patch_json),
        tools={
            "read_repo_files":       read_repo_files,
            "run_mypy":              lambda paths, **kw: "OK",
            "run_pytest":            lambda test_file, **kw: "PASS",
            "write_repo_files":      write_repo_files,
            "notify_rejected_mypy":  lambda **kw: "REJECTED_MYPY",
            "notify_rejected_pytest": lambda **kw: "REJECTED_PYTEST",
            "notify_done":           lambda **kw: "DONE",
        },
    )
    program = Program.from_dict(PROGRAM_SPRINT)
    trace = await vm.run(program, context={
        "sprint_spec":  "add field y: int = 2",
        "target_files": json.dumps([target]),
        "test_file":    test_f,
        "repo_path":    str(tmp_path),
    })

    assert trace.status == TraceStatus.SUCCESS
    # find notify_done step
    outputs = [s.output for s in trace.steps]
    assert "DONE" in outputs


@pytest.mark.asyncio
async def test_da_10_mypy_fail(tmp_path: Path) -> None:
    """mypy fails → FSM takes otherwise branch → REJECTED_MYPY, no files written."""
    from nano_vm.models import Program, TraceStatus
    from nano_vm.vm import ExecutionVM

    target, test_f, patch_json = _make_patch_json(tmp_path)
    original = Path(target).read_text()

    vm = ExecutionVM(
        llm=MockLLMAdapter(patch_json),
        tools={
            "read_repo_files":       read_repo_files,
            "run_mypy":              lambda paths, **kw: "error: Incompatible return value",
            "run_pytest":            lambda test_file, **kw: "PASS",
            "write_repo_files":      write_repo_files,
            "notify_rejected_mypy":  lambda **kw: "REJECTED_MYPY",
            "notify_rejected_pytest": lambda **kw: "REJECTED_PYTEST",
            "notify_done":           lambda **kw: "DONE",
        },
    )
    program = Program.from_dict(PROGRAM_SPRINT)
    trace = await vm.run(program, context={
        "sprint_spec":  "test",
        "target_files": json.dumps([target]),
        "test_file":    test_f,
        "repo_path":    str(tmp_path),
    })

    assert trace.status == TraceStatus.SUCCESS
    outputs = [s.output for s in trace.steps]
    assert "REJECTED_MYPY" in outputs
    # file must NOT be overwritten
    assert Path(target).read_text() == original


@pytest.mark.asyncio
async def test_da_11_pytest_fail(tmp_path: Path) -> None:
    """mypy OK but pytest fails → REJECTED_PYTEST, no files written."""
    from nano_vm.models import Program, TraceStatus
    from nano_vm.vm import ExecutionVM

    target, test_f, patch_json = _make_patch_json(tmp_path)
    original = Path(target).read_text()

    vm = ExecutionVM(
        llm=MockLLMAdapter(patch_json),
        tools={
            "read_repo_files":       read_repo_files,
            "run_mypy":              lambda paths, **kw: "OK",
            "run_pytest":            lambda test_file, **kw: "FAILED: assert 1 == 2",
            "write_repo_files":      write_repo_files,
            "notify_rejected_mypy":  lambda **kw: "REJECTED_MYPY",
            "notify_rejected_pytest": lambda **kw: "REJECTED_PYTEST",
            "notify_done":           lambda **kw: "DONE",
        },
    )
    program = Program.from_dict(PROGRAM_SPRINT)
    trace = await vm.run(program, context={
        "sprint_spec":  "test",
        "target_files": json.dumps([target]),
        "test_file":    test_f,
        "repo_path":    str(tmp_path),
    })

    assert trace.status == TraceStatus.SUCCESS
    outputs = [s.output for s in trace.steps]
    assert "REJECTED_PYTEST" in outputs
    assert Path(target).read_text() == original


@pytest.mark.asyncio
async def test_da_12_llm_timeout(tmp_path: Path) -> None:
    """LLM hangs → timeout → trace.FAILED, error contains 'timed out'."""
    from nano_vm.models import Program, TraceStatus
    from nano_vm.vm import ExecutionVM

    target, test_f, _ = _make_patch_json(tmp_path)

    # Patch PROGRAM_SPRINT with very short timeout for this test
    import copy
    program_dict = copy.deepcopy(PROGRAM_SPRINT)
    for step in program_dict["steps"]:
        if step["id"] == "generate_patch":
            step["timeout_seconds"] = 0.05
            step["on_timeout"] = "fail"

    vm = ExecutionVM(
        llm=SlowLLMAdapter(),
        tools={
            "read_repo_files":       read_repo_files,
            "run_mypy":              lambda paths, **kw: "OK",
            "run_pytest":            lambda test_file, **kw: "PASS",
            "write_repo_files":      write_repo_files,
            "notify_rejected_mypy":  lambda **kw: "REJECTED_MYPY",
            "notify_rejected_pytest": lambda **kw: "REJECTED_PYTEST",
            "notify_done":           lambda **kw: "DONE",
        },
    )
    program = Program.from_dict(program_dict)
    trace = await vm.run(program, context={
        "sprint_spec":  "test",
        "target_files": json.dumps([target]),
        "test_file":    test_f,
        "repo_path":    str(tmp_path),
    })

    assert trace.status == TraceStatus.FAILED
    assert trace.error is not None
    assert "timed out" in trace.error
