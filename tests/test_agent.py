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
DA-19  build_program_sprint(is_new=[True]): new-file chain has no read_0 step
DA-20  new-file sprint E2E: single new file, full-content patch, no S&R → trace.SUCCESS
DA-21  mixed sprint E2E: file_0 existing (S&R) + file_1 new (full-content) → trace.SUCCESS
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from agent.programs import build_program_sprint
from agent.tools import (
    apply_search_replace_patch,
    commit_patches,
    read_repo_files,
    read_staged_files,
    rollback_patches,
    run_mypy,
    run_pytest,
    stage_new_file,
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
    """Build ExecutionVM + Program + context for integration tests (3 files)."""
    from nano_vm.models import Program
    from nano_vm.vm import ExecutionVM

    # Create minimal source files so read_repo_files doesn't raise.
    # Names are arbitrary — the pipeline no longer hardcodes filenames.
    file_a = tmp_path / "domains" / "a.py"
    file_b = tmp_path / "domains" / "b.py"
    file_c = tmp_path / "domains" / "c.py"
    target_paths = [file_a, file_b, file_c]
    for f in target_paths:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(f"# {f.name}\nVERSION = 1\n", encoding="utf-8")

    test_f = tmp_path / "tests" / "test_sprint.py"
    test_f.parent.mkdir(parents=True, exist_ok=True)
    test_f.write_text("def test_ok():\n    assert True\n", encoding="utf-8")

    # LLM response sequence: 3 S&R patches + 1 test file JSON
    test_json = json.dumps({str(test_f): "def test_ok():\n    assert True\n"})
    llm_responses = [_make_sr_patch(str(p)) for p in target_paths] + [test_json]

    vm = ExecutionVM(
        llm=MockLLMAdapter(llm_responses),
        tools={
            "read_repo_files":            read_repo_files,
            "read_staged_files":          read_staged_files,
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
    program = Program.from_dict(build_program_sprint(len(target_paths)))

    abs_repo = str(tmp_path)
    resolved = [str(p) for p in target_paths]

    context: dict[str, str] = {
        "sprint_spec":  "add field y: int = 2",
        "target_files": json.dumps(resolved),
        "test_file":    str(test_f),
        "repo_path":    abs_repo,
    }
    for i, path in enumerate(resolved):
        context[f"file_{i}_paths"] = json.dumps([path])
        context[f"file_{i}_file"] = path

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

    target = tmp_path / "domains" / "a.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# a.py\nVERSION = 1\n", encoding="utf-8")

    # Single-file pipeline; patch tiny timeout onto its one patch step.
    program_dict = build_program_sprint(1)
    for step in program_dict["steps"]:
        if step["id"] == "patch_0":
            step["timeout_seconds"] = 0.05
            step["on_timeout"] = "fail"
            step.pop("on_error", None)  # disable retry so test is fast

    vm = ExecutionVM(
        llm=SlowLLMAdapter(),
        tools={
            "read_repo_files":            read_repo_files,
            "read_staged_files":          read_staged_files,
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
        "target_files":  json.dumps([str(target)]),
        "test_file":     str(tmp_path / "test_sprint.py"),
        "repo_path":     str(tmp_path),
        "file_0_paths":  json.dumps([str(target)]),
        "file_0_file":   str(target),
    })

    assert trace.status == TraceStatus.FAILED
    assert trace.error is not None
    assert "timed out" in trace.error


# ---------------------------------------------------------------------------
# Genericity tests (post-refactor) — DA-17, DA-18
# ---------------------------------------------------------------------------

def test_da_17_build_program_sprint_generic_n(tmp_path: Path) -> None:
    """build_program_sprint(N) for N != 3 produces a valid N-file pipeline
    that runs end-to-end. Proves the agent is no longer hardcoded to the
    legacy store/handlers/tools (nano-vm-mcp) 3-file shape — e.g. it can
    target Sieshka's 4 FSM domain files."""
    import asyncio as _asyncio

    from nano_vm.models import Program, TraceStatus
    from nano_vm.vm import ExecutionVM

    target_paths = [
        tmp_path / "app" / "domains" / "inventory" / "fsm.py",
        tmp_path / "app" / "domains" / "promotions" / "fsm.py",
        tmp_path / "app" / "domains" / "schedule" / "fsm.py",
        tmp_path / "app" / "domains" / "privacy" / "fsm.py",
    ]
    for f in target_paths:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(f"# {f.name}\nVERSION = 1\n", encoding="utf-8")

    test_f = tmp_path / "tests" / "unit" / "fsm" / "test_inventory_fsm.py"
    test_f.parent.mkdir(parents=True, exist_ok=True)
    test_f.write_text("def test_ok():\n    assert True\n", encoding="utf-8")

    test_json = json.dumps({str(test_f): "def test_ok():\n    assert True\n"})
    llm_responses = [_make_sr_patch(str(p)) for p in target_paths] + [test_json]

    vm = ExecutionVM(
        llm=MockLLMAdapter(llm_responses),
        tools={
            "read_repo_files":            read_repo_files,
            "read_staged_files":          read_staged_files,
            "apply_search_replace_patch": apply_search_replace_patch,
            "stage_patch":                stage_patch,
            "validate_staged_mypy":       lambda paths, **kw: "OK",
            "commit_patches":             commit_patches,
            "rollback_patches":           rollback_patches,
            "git_checkout_files":         lambda paths, **kw: f"RESTORED: {paths}",
            "run_mypy":                   lambda paths, **kw: "OK",
            "run_pytest":                 lambda test_file, **kw: "PASS",
            "write_repo_files":           write_repo_files,
            "notify_rejected_mypy":       lambda **kw: "REJECTED_MYPY",
            "notify_rejected_pytest":     lambda **kw: "REJECTED_PYTEST",
            "notify_done":                lambda **kw: "DONE",
        },
    )

    program_dict = build_program_sprint(len(target_paths))
    # Sanity: exactly 4 read/patch/stage triples generated, ids 0..3, no
    # off-by-one or leftover chain link into a nonexistent 5th file.
    all_ids = {s["id"] for s in program_dict["steps"]}
    for i in range(4):
        assert f"read_{i}" in all_ids
        assert f"patch_{i}" in all_ids
        assert f"stage_{i}" in all_ids
    assert "read_4" not in all_ids
    assert "patch_4" not in all_ids
    assert "stage_4" not in all_ids

    program = Program.from_dict(program_dict)
    resolved = [str(p) for p in target_paths]
    context: dict[str, str] = {
        "sprint_spec":  "rename FSM field across 4 domain modules",
        "target_files": json.dumps(resolved),
        "test_file":    str(test_f),
        "repo_path":    str(tmp_path),
    }
    for i, path in enumerate(resolved):
        context[f"file_{i}_paths"] = json.dumps([path])
        context[f"file_{i}_file"] = path

    trace = _asyncio.run(vm.run(program, context=context))

    assert trace.status == TraceStatus.SUCCESS
    outputs = [s.output for s in trace.steps]
    assert "DONE" in outputs


def test_da_18_build_program_sprint_rejects_zero_files() -> None:
    """build_program_sprint(0) raises ValueError — a sprint needs >= 1 target file."""
    with pytest.raises(ValueError, match="num_files"):
        build_program_sprint(0)


# ---------------------------------------------------------------------------
# New-file creation tests (post-2026-06-20 refactor) — DA-19, DA-20, DA-21
# ---------------------------------------------------------------------------

def test_da_19_build_program_sprint_new_file_has_no_read_step() -> None:
    """A file flagged is_new=True must not get a read_{i} step — there is
    nothing on disk to read yet. Its chain is patch_{i} -> stage_{i} only,
    and stage_{i} must call stage_new_file, not stage_patch."""
    program_dict = build_program_sprint([True])

    ids = [s["id"] for s in program_dict["steps"]]
    assert "read_0" not in ids
    assert "patch_0" in ids
    assert "stage_0" in ids

    stage_step = next(s for s in program_dict["steps"] if s["id"] == "stage_0")
    assert stage_step["tool"] == "stage_new_file"
    assert stage_step["args"] == {
        "file_path": "$file_0_file",
        "content": "$file_0_patch",
    }

    patch_step = next(s for s in program_dict["steps"] if s["id"] == "patch_0")
    assert "$read_0.output" not in patch_step["prompt"]
    assert "$file_0_file" in patch_step["prompt"]


def test_da_20_new_file_sprint_e2e(tmp_path: Path) -> None:
    """Single brand-new target file (no S&R, full-content patch) runs the
    full DA-4 pipeline end to end: patch -> stage_new_file -> generate_test
    -> mypy -> commit -> pytest -> notify_done. Mirrors
    sprint_m1_inventory_promotions's InventoryFSM module creation."""
    from nano_vm.models import Program, TraceStatus
    from nano_vm.vm import ExecutionVM

    target = tmp_path / "app" / "domains" / "inventory" / "fsm.py"
    assert not target.exists()  # genuinely new — nothing pre-staged on disk

    test_f = tmp_path / "tests" / "unit" / "fsm" / "test_inventory_fsm.py"
    test_f.parent.mkdir(parents=True, exist_ok=True)
    test_f.write_text("def test_ok():\n    assert True\n", encoding="utf-8")

    new_file_content = (
        "from __future__ import annotations\n\n"
        "class InventoryFSM:\n"
        "    pass\n"
    )
    test_json = json.dumps({str(test_f): "def test_ok():\n    assert True\n"})
    llm_responses = [new_file_content, test_json]

    vm = ExecutionVM(
        llm=MockLLMAdapter(llm_responses),
        tools={
            "read_repo_files":            read_repo_files,
            "read_staged_files":          read_staged_files,
            "apply_search_replace_patch": apply_search_replace_patch,
            "stage_patch":                stage_patch,
            "stage_new_file":             stage_new_file,
            "validate_staged_mypy":       lambda paths, **kw: "OK",
            "commit_patches":             commit_patches,
            "rollback_patches":           rollback_patches,
            "git_checkout_files":         lambda paths, **kw: f"RESTORED: {paths}",
            "run_mypy":                   lambda paths, **kw: "OK",
            "run_pytest":                 lambda test_file, **kw: "PASS",
            "write_repo_files":           write_repo_files,
            "notify_rejected_mypy":       lambda **kw: "REJECTED_MYPY",
            "notify_rejected_pytest":     lambda **kw: "REJECTED_PYTEST",
            "notify_done":                lambda **kw: "DONE",
        },
    )

    program_dict = build_program_sprint([True])
    program = Program.from_dict(program_dict)

    context: dict[str, str] = {
        "sprint_spec":  "create InventoryFSM (AVAILABLE→LOW_STOCK→CRITICAL→OUT_OF_STOCK)",
        "target_files": json.dumps([str(target)]),
        "test_file":    str(test_f),
        "repo_path":    str(tmp_path),
        "file_0_file":  str(target),
        # deliberately NO file_0_paths — new files never read from disk
    }

    trace = asyncio.run(vm.run(program, context=context))

    assert trace.status == TraceStatus.SUCCESS
    outputs = [s.output for s in trace.steps]
    assert "DONE" in outputs
    step_ids = [s.step_id for s in trace.steps]
    assert "read_0" not in step_ids


def test_da_21_mixed_existing_and_new_file_sprint_e2e(tmp_path: Path) -> None:
    """One existing file (patched via S&R) + one brand-new file (full content)
    in the same sprint — both chains coexist and both feed into the same
    mypy/commit/pytest tail."""
    from nano_vm.models import Program, TraceStatus
    from nano_vm.vm import ExecutionVM

    existing = tmp_path / "app" / "domains" / "orders" / "fsm.py"
    existing.parent.mkdir(parents=True, exist_ok=True)
    existing.write_text("# orders fsm\nVERSION = 1\n", encoding="utf-8")

    new_file = tmp_path / "app" / "domains" / "inventory" / "fsm.py"
    assert not new_file.exists()

    test_f = tmp_path / "tests" / "test_mixed.py"
    test_f.parent.mkdir(parents=True, exist_ok=True)
    test_f.write_text("def test_ok():\n    assert True\n", encoding="utf-8")

    sr_patch_for_existing = _make_sr_patch(str(existing))
    new_file_content = "class InventoryFSM:\n    pass\n"
    test_json = json.dumps({str(test_f): "def test_ok():\n    assert True\n"})
    llm_responses = [sr_patch_for_existing, new_file_content, test_json]

    vm = ExecutionVM(
        llm=MockLLMAdapter(llm_responses),
        tools={
            "read_repo_files":            read_repo_files,
            "read_staged_files":          read_staged_files,
            "apply_search_replace_patch": apply_search_replace_patch,
            "stage_patch":                stage_patch,
            "stage_new_file":             stage_new_file,
            "validate_staged_mypy":       lambda paths, **kw: "OK",
            "commit_patches":             commit_patches,
            "rollback_patches":           rollback_patches,
            "git_checkout_files":         lambda paths, **kw: f"RESTORED: {paths}",
            "run_mypy":                   lambda paths, **kw: "OK",
            "run_pytest":                 lambda test_file, **kw: "PASS",
            "write_repo_files":           write_repo_files,
            "notify_rejected_mypy":       lambda **kw: "REJECTED_MYPY",
            "notify_rejected_pytest":     lambda **kw: "REJECTED_PYTEST",
            "notify_done":                lambda **kw: "DONE",
        },
    )

    is_new_flags = [False, True]  # file_0 = existing, file_1 = new
    program = Program.from_dict(build_program_sprint(is_new_flags))

    resolved = [str(existing), str(new_file)]
    context: dict[str, str] = {
        "sprint_spec":  "patch orders FSM + create inventory FSM",
        "target_files": json.dumps(resolved),
        "test_file":    str(test_f),
        "repo_path":    str(tmp_path),
        "file_0_file":  str(existing),
        "file_0_paths": json.dumps([str(existing)]),
        "file_1_file":  str(new_file),
        # no file_1_paths — file_1 is new, never read from disk
    }

    trace = asyncio.run(vm.run(program, context=context))

    assert trace.status == TraceStatus.SUCCESS
    step_ids = [s.step_id for s in trace.steps]
    assert "read_0" in step_ids   # existing file was read
    assert "read_1" not in step_ids  # new file was never read
    assert "patch_0" in step_ids
    assert "patch_1" in step_ids


# ---------------------------------------------------------------------------
# reference_files tests (post-2026-06-21 refactor) — DA-28..31
# ---------------------------------------------------------------------------

def test_da_28_build_program_sprint_no_references_unchanged() -> None:
    """Without reference_files, no read_references step appears and prompts
    contain no 'Reference files' section — exact pre-existing behavior."""
    program_dict = build_program_sprint(2)
    ids = [s["id"] for s in program_dict["steps"]]
    assert "read_references" not in ids

    patch_0 = next(s for s in program_dict["steps"] if s["id"] == "patch_0")
    assert "Reference files" not in patch_0["prompt"]
    assert "$read_references.output" not in patch_0["prompt"]


def test_da_29_build_program_sprint_with_references_adds_read_step() -> None:
    """reference_files non-empty → read_references step inserted before the
    first file's chain, and every patch_{i} prompt includes the reference
    section referencing $read_references.output."""
    program_dict = build_program_sprint([False, True], reference_files=["app/fsm/core/base.py"])
    steps_by_id = {s["id"]: s for s in program_dict["steps"]}

    assert "read_references" in steps_by_id
    ref_step = steps_by_id["read_references"]
    assert ref_step["tool"] == "read_repo_files"
    assert ref_step["args"] == {"paths": "$reference_paths"}
    # read_references must chain into the first file's first step (read_0,
    # since file 0 is existing) — not directly into generate_test.
    assert ref_step["next_step"] == "read_0"

    patch_0_prompt = steps_by_id["patch_0"]["prompt"]
    patch_1_prompt = steps_by_id["patch_1"]["prompt"]
    assert "$read_references.output" in patch_0_prompt
    assert "Reference files" in patch_0_prompt
    # is_new file's create-prompt also gets the reference section
    assert "$read_references.output" in patch_1_prompt
    assert "do NOT modify" in patch_1_prompt or "do not invent alternative" in patch_1_prompt


def test_da_30_build_program_sprint_references_chain_to_first_new_file() -> None:
    """If file 0 is new (no read_0 step exists), read_references must chain
    directly into patch_0, not into a nonexistent read_0."""
    program_dict = build_program_sprint([True], reference_files=["app/domains/orders/fsm.py"])
    steps_by_id = {s["id"]: s for s in program_dict["steps"]}
    assert steps_by_id["read_references"]["next_step"] == "patch_0"
    assert "read_0" not in steps_by_id


def test_da_31_reference_files_e2e_inventory_fsm_sprint(tmp_path: Path) -> None:
    """E2E repro of sprint_m1_inventory_promotions's actual failure mode:
    a new file is created WITH reference context (canonical BaseFSM shown
    to the LLM) and the pipeline runs through to SUCCESS. This doesn't
    assert the LLM "got it right" (that's a MockLLMAdapter, not a real
    model) — it asserts the wiring: read_references executes once, its
    output reaches the patch_0 prompt via context substitution, and the
    rest of the DA-4 tail (mypy/commit/pytest) is unaffected."""
    from nano_vm.models import Program, TraceStatus
    from nano_vm.vm import ExecutionVM

    base_fsm = tmp_path / "app" / "fsm" / "core" / "base.py"
    base_fsm.parent.mkdir(parents=True, exist_ok=True)
    base_fsm.write_text(
        "from __future__ import annotations\n\n"
        "class BaseFSM:\n"
        "    def __init__(self, initial_state):\n"
        "        self._state = initial_state\n",
        encoding="utf-8",
    )

    new_file = tmp_path / "app" / "domains" / "inventory" / "fsm.py"
    test_f = tmp_path / "tests" / "test_inventory_fsm.py"
    test_f.parent.mkdir(parents=True, exist_ok=True)
    test_f.write_text("def test_ok():\n    assert True\n", encoding="utf-8")

    new_file_content = (
        "from app.fsm.core.base import BaseFSM\n\n"
        "class InventoryFSM(BaseFSM):\n    pass\n"
    )
    test_json = json.dumps({str(test_f): "def test_ok():\n    assert True\n"})
    llm_responses = [new_file_content, test_json]

    vm = ExecutionVM(
        llm=MockLLMAdapter(llm_responses),
        tools={
            "read_repo_files":            read_repo_files,
            "read_staged_files":          read_staged_files,
            "apply_search_replace_patch": apply_search_replace_patch,
            "stage_patch":                stage_patch,
            "stage_new_file":             stage_new_file,
            "validate_staged_mypy":       lambda paths, **kw: "OK",
            "commit_patches":             commit_patches,
            "rollback_patches":           rollback_patches,
            "git_checkout_files":         lambda paths, **kw: f"RESTORED: {paths}",
            "run_mypy":                   lambda paths, **kw: "OK",
            "run_pytest":                 lambda test_file, **kw: "PASS",
            "write_repo_files":           write_repo_files,
            "notify_rejected_mypy":       lambda **kw: "REJECTED_MYPY",
            "notify_rejected_pytest":     lambda **kw: "REJECTED_PYTEST",
            "notify_done":                lambda **kw: "DONE",
        },
    )

    program = Program.from_dict(
        build_program_sprint([True], reference_files=[str(base_fsm)])
    )
    context: dict[str, str] = {
        "sprint_spec":     "create InventoryFSM following BaseFSM pattern",
        "target_files":    json.dumps([str(new_file)]),
        "test_file":       str(test_f),
        "repo_path":       str(tmp_path),
        "file_0_file":     str(new_file),
        "reference_paths": json.dumps([str(base_fsm)]),
    }

    trace = asyncio.run(vm.run(program, context=context))

    assert trace.status == TraceStatus.SUCCESS
    step_ids = [s.step_id for s in trace.steps]
    assert "read_references" in step_ids
    assert step_ids.index("read_references") < step_ids.index("patch_0")
    ref_step_result = next(s for s in trace.steps if s.step_id == "read_references")
    assert "class BaseFSM" in ref_step_result.output


# ---------------------------------------------------------------------------
# read_staged_files / generate_test context tests — DA-32..34
# ---------------------------------------------------------------------------

def test_da_32_build_program_sprint_inserts_read_staged_before_test() -> None:
    """read_staged must run after the last file's stage step and before
    generate_test, regardless of file count or is_new mix."""
    program_dict = build_program_sprint([False, True])
    steps_by_id = {s["id"]: s for s in program_dict["steps"]}

    assert "read_staged" in steps_by_id
    assert steps_by_id["read_staged"]["tool"] == "read_staged_files"
    assert steps_by_id["read_staged"]["args"] == {"paths": "$target_files"}
    assert steps_by_id["read_staged"]["next_step"] == "generate_test"
    # the last file's stage step chains into read_staged, not generate_test directly
    assert steps_by_id["stage_1"]["next_step"] == "read_staged"

    generate_test_prompt = steps_by_id["generate_test"]["prompt"]
    assert "$read_staged.output" in generate_test_prompt
    assert "do not invent methods" in generate_test_prompt


def test_da_33_read_staged_files_prefers_buffer_over_disk(tmp_path: Path) -> None:
    """read_staged_files must return the STAGED (buffered) content, not the
    on-disk content, when a file has been staged this sprint but not yet
    committed — this is the entire point of the tool."""
    import agent.tools as tools_module

    target = tmp_path / "fsm.py"
    target.write_text("OLD_DISK_CONTENT = True\n", encoding="utf-8")

    tools_module._patch_buffer[str(target)] = "NEW_STAGED_CONTENT = True\n"
    try:
        result = read_staged_files(json.dumps([str(target)]), repo_path=str(tmp_path))
    finally:
        tools_module._patch_buffer.clear()

    assert "NEW_STAGED_CONTENT" in result
    assert "OLD_DISK_CONTENT" not in result


def test_da_34_read_staged_files_falls_back_to_disk_when_unstaged(tmp_path: Path) -> None:
    """A target file not touched by any patch/stage step this sprint (e.g.
    out of scope for this particular sprint) still reads correctly from disk."""
    target = tmp_path / "untouched.py"
    target.write_text("UNTOUCHED = True\n", encoding="utf-8")

    result = read_staged_files(json.dumps([str(target)]), repo_path=str(tmp_path))

    assert "UNTOUCHED" in result


def test_da_35_generate_test_e2e_sees_real_staged_signature(tmp_path: Path) -> None:
    """E2E repro of the actual failure mode (DECISIONS.md 2026-06-22):
    generate_test must receive the REAL staged FSM content (sync,
    state_reader/state_writer constructor) so the test it writes matches
    the real API instead of a hallucinated async/db_path shape."""
    from nano_vm.models import Program, TraceStatus
    from nano_vm.vm import ExecutionVM

    new_file = tmp_path / "app" / "domains" / "inventory" / "fsm.py"
    test_f = tmp_path / "tests" / "test_inventory_fsm.py"
    test_f.parent.mkdir(parents=True, exist_ok=True)
    test_f.write_text("def test_ok():\n    assert True\n", encoding="utf-8")

    real_fsm_content = (
        "class InventoryFSM:\n"
        "    def __init__(self, state_reader, state_writer):\n"
        "        self._read = state_reader\n"
        "        self._write = state_writer\n"
    )

    captured_prompts: list[str] = []

    class _CapturingAdapter:
        def __init__(self, responses: list[str]) -> None:
            self._responses = list(responses)

        async def complete(self, messages: list[dict[str, str]]) -> str:
            captured_prompts.append(messages[-1]["content"])
            return self._responses.pop(0)

    test_json = json.dumps({str(test_f): "def test_ok():\n    assert True\n"})
    adapter = _CapturingAdapter([real_fsm_content, test_json])

    vm = ExecutionVM(
        llm=adapter,
        tools={
            "read_repo_files":            read_repo_files,
            "read_staged_files":          read_staged_files,
            "apply_search_replace_patch": apply_search_replace_patch,
            "stage_patch":                stage_patch,
            "stage_new_file":             stage_new_file,
            "validate_staged_mypy":       lambda paths, **kw: "OK",
            "commit_patches":             commit_patches,
            "rollback_patches":           rollback_patches,
            "git_checkout_files":         lambda paths, **kw: f"RESTORED: {paths}",
            "run_mypy":                   lambda paths, **kw: "OK",
            "run_pytest":                 lambda test_file, **kw: "PASS",
            "write_repo_files":           write_repo_files,
            "notify_rejected_mypy":       lambda **kw: "REJECTED_MYPY",
            "notify_rejected_pytest":     lambda **kw: "REJECTED_PYTEST",
            "notify_done":                lambda **kw: "DONE",
        },
    )

    program = Program.from_dict(build_program_sprint([True]))
    context: dict[str, str] = {
        "sprint_spec":  "create InventoryFSM",
        "target_files": json.dumps([str(new_file)]),
        "test_file":    str(test_f),
        "repo_path":    str(tmp_path),
        "file_0_file":  str(new_file),
    }

    trace = asyncio.run(vm.run(program, context=context))

    assert trace.status == TraceStatus.SUCCESS
    # the generate_test prompt (2nd LLM call) must contain the REAL staged
    # constructor shape, not just the sprint_spec text
    generate_test_prompt = captured_prompts[1]
    assert "state_reader" in generate_test_prompt
    assert "state_writer" in generate_test_prompt
