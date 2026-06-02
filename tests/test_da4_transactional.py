"""
tests/test_da4_transactional.py
================================
DA-4: unit tests for transactional patching in agent/tools.py

DA4-01  stage_patch: reads from disk, stores in buffer, disk unchanged
DA4-02  stage_patch: accumulates — second call reads from buffer not disk
DA4-03  stage_patch: ValueError on no S&R blocks
DA4-04  stage_patch: ValueError on SEARCH not found
DA4-05  stage_patch: ValueError on SEARCH matches > 1
DA4-06  commit_patches: flushes buffer to disk, clears buffer
DA4-07  commit_patches: empty buffer returns sentinel string
DA4-08  rollback_patches: clears buffer without touching disk
DA4-09  rollback_patches: empty buffer returns sentinel string
DA4-10  validate_staged_mypy: OK path (trivially valid Python)
DA4-11  validate_staged_mypy: returns error output on bad Python
DA4-12  git_checkout_files: ValueError on non-JSON paths
DA4-13  full happy-path: stage → validate_staged_mypy → commit → disk has new content
DA4-14  full fail-path: stage → validate_staged_mypy fail → rollback → disk unchanged
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import types

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _reload_tools() -> types.ModuleType:
    """Reload agent.tools to reset _patch_buffer between tests."""
    if "agent.tools" in sys.modules:
        del sys.modules["agent.tools"]
    if "agent" in sys.modules:
        del sys.modules["agent"]
    # ensure agent/ is on path
    agent_dir = os.path.join(os.path.dirname(__file__), "..")
    if agent_dir not in sys.path:
        sys.path.insert(0, agent_dir)
    return importlib.import_module("agent.tools")


# ---------------------------------------------------------------------------
# DA4-01  stage reads from disk, buffer holds result, disk unchanged
# ---------------------------------------------------------------------------

def test_da4_01_stage_reads_disk_buffer_populated(tmp_path: os.PathLike[str]) -> None:
    tools = _reload_tools()
    f = tmp_path / "sample.py"
    f.write_text("x = 1\ny = 2\n", encoding="utf-8")

    patch = "<<<SEARCH\nx = 1\n=======\nx = 99\n>>>REPLACE"
    result = tools.stage_patch(str(f), patch)

    assert result.startswith("STAGED:")
    assert str(f) in tools._patch_buffer
    assert "x = 99" in tools._patch_buffer[str(f)]
    # disk untouched
    assert f.read_text(encoding="utf-8") == "x = 1\ny = 2\n"


# ---------------------------------------------------------------------------
# DA4-02  second stage_patch reads from buffer, not disk
# ---------------------------------------------------------------------------

def test_da4_02_stage_accumulates_from_buffer(tmp_path: os.PathLike[str]) -> None:
    tools = _reload_tools()
    f = tmp_path / "sample.py"
    f.write_text("x = 1\ny = 2\n", encoding="utf-8")

    patch1 = "<<<SEARCH\nx = 1\n=======\nx = 99\n>>>REPLACE"
    patch2 = "<<<SEARCH\ny = 2\n=======\ny = 88\n>>>REPLACE"

    tools.stage_patch(str(f), patch1)
    # disk still has original — buffer has patch1 result
    tools.stage_patch(str(f), patch2)

    buffered = tools._patch_buffer[str(f)]
    assert "x = 99" in buffered
    assert "y = 88" in buffered
    assert f.read_text(encoding="utf-8") == "x = 1\ny = 2\n"


# ---------------------------------------------------------------------------
# DA4-03  stage_patch: no S&R blocks → ValueError
# ---------------------------------------------------------------------------

def test_da4_03_stage_no_blocks(tmp_path: os.PathLike[str]) -> None:
    import pytest
    tools = _reload_tools()
    f = tmp_path / "sample.py"
    f.write_text("x = 1\n", encoding="utf-8")

    with pytest.raises(ValueError, match="no S&R blocks"):
        tools.stage_patch(str(f), "some random text")


# ---------------------------------------------------------------------------
# DA4-04  stage_patch: SEARCH not found → ValueError
# ---------------------------------------------------------------------------

def test_da4_04_stage_search_not_found(tmp_path: os.PathLike[str]) -> None:
    import pytest
    tools = _reload_tools()
    f = tmp_path / "sample.py"
    f.write_text("x = 1\n", encoding="utf-8")

    patch = "<<<SEARCH\nz = 999\n=======\nz = 0\n>>>REPLACE"
    with pytest.raises(ValueError, match="SEARCH not found"):
        tools.stage_patch(str(f), patch)


# ---------------------------------------------------------------------------
# DA4-05  stage_patch: SEARCH matches > 1 → ValueError
# ---------------------------------------------------------------------------

def test_da4_05_stage_search_ambiguous(tmp_path: os.PathLike[str]) -> None:
    import pytest
    tools = _reload_tools()
    f = tmp_path / "sample.py"
    f.write_text("x = 1\nx = 1\n", encoding="utf-8")

    patch = "<<<SEARCH\nx = 1\n=======\nx = 0\n>>>REPLACE"
    with pytest.raises(ValueError, match="matches 2 times"):
        tools.stage_patch(str(f), patch)


# ---------------------------------------------------------------------------
# DA4-06  commit_patches: writes to disk, clears buffer
# ---------------------------------------------------------------------------

def test_da4_06_commit_writes_and_clears(tmp_path: os.PathLike[str]) -> None:
    tools = _reload_tools()
    f = tmp_path / "sample.py"
    f.write_text("x = 1\n", encoding="utf-8")

    patch = "<<<SEARCH\nx = 1\n=======\nx = 42\n>>>REPLACE"
    tools.stage_patch(str(f), patch)
    result = tools.commit_patches()

    assert "COMMITTED:" in result
    assert f.read_text(encoding="utf-8") == "x = 42\n"
    assert tools._patch_buffer == {}


# ---------------------------------------------------------------------------
# DA4-07  commit_patches: empty buffer → sentinel
# ---------------------------------------------------------------------------

def test_da4_07_commit_empty_buffer() -> None:
    tools = _reload_tools()
    result = tools.commit_patches()
    assert result == "COMMITTED: (nothing staged)"


# ---------------------------------------------------------------------------
# DA4-08  rollback_patches: clears buffer, disk unchanged
# ---------------------------------------------------------------------------

def test_da4_08_rollback_clears_buffer_disk_safe(tmp_path: os.PathLike[str]) -> None:
    tools = _reload_tools()
    f = tmp_path / "sample.py"
    f.write_text("x = 1\n", encoding="utf-8")

    patch = "<<<SEARCH\nx = 1\n=======\nx = 99\n>>>REPLACE"
    tools.stage_patch(str(f), patch)
    result = tools.rollback_patches()

    assert "ROLLED_BACK:" in result
    assert tools._patch_buffer == {}
    assert f.read_text(encoding="utf-8") == "x = 1\n"


# ---------------------------------------------------------------------------
# DA4-09  rollback_patches: empty buffer → sentinel
# ---------------------------------------------------------------------------

def test_da4_09_rollback_empty_buffer() -> None:
    tools = _reload_tools()
    result = tools.rollback_patches()
    assert result == "ROLLED_BACK: (nothing staged)"


# ---------------------------------------------------------------------------
# DA4-10  validate_staged_mypy: OK on valid Python
# ---------------------------------------------------------------------------

def test_da4_10_validate_staged_mypy_ok(tmp_path: os.PathLike[str]) -> None:
    tools = _reload_tools()
    # Stage a trivially valid typed Python file
    f = tmp_path / "mod.py"
    f.write_text("x: int = 1\n", encoding="utf-8")

    patch = "<<<SEARCH\nx: int = 1\n=======\nx: int = 2\n>>>REPLACE"
    tools.stage_patch(str(f), patch)

    result = tools.validate_staged_mypy(
        json.dumps([str(f)]),
        repo_path=str(tmp_path),
    )
    assert result == "OK"
    # disk still original
    assert f.read_text(encoding="utf-8") == "x: int = 1\n"
    # buffer still populated (validate does not clear)
    assert str(f) in tools._patch_buffer


# ---------------------------------------------------------------------------
# DA4-11  validate_staged_mypy: returns errors on type-invalid Python
# ---------------------------------------------------------------------------

def test_da4_11_validate_staged_mypy_fail(tmp_path: os.PathLike[str]) -> None:
    tools = _reload_tools()
    f = tmp_path / "bad.py"
    f.write_text("x: int = 1\n", encoding="utf-8")

    # Stage content that will fail mypy --strict (missing return type annotation)
    bad_content = "def foo(x):\n    return x\n"
    tools._patch_buffer[str(f)] = bad_content

    result = tools.validate_staged_mypy(
        json.dumps([str(f)]),
        repo_path=str(tmp_path),
    )
    assert result != "OK"


# ---------------------------------------------------------------------------
# DA4-12  git_checkout_files: ValueError on non-JSON paths
# ---------------------------------------------------------------------------

def test_da4_12_git_checkout_invalid_paths() -> None:
    import pytest
    tools = _reload_tools()
    with pytest.raises(ValueError, match="paths must be JSON list"):
        tools.git_checkout_files("not-json")


# ---------------------------------------------------------------------------
# DA4-13  happy path: stage → validate OK → commit → disk updated
# ---------------------------------------------------------------------------

def test_da4_13_happy_path(tmp_path: os.PathLike[str]) -> None:
    tools = _reload_tools()
    f = tmp_path / "mod.py"
    f.write_text("x: int = 1\n", encoding="utf-8")

    patch = "<<<SEARCH\nx: int = 1\n=======\nx: int = 99\n>>>REPLACE"
    tools.stage_patch(str(f), patch)

    mypy_result = tools.validate_staged_mypy(
        json.dumps([str(f)]),
        repo_path=str(tmp_path),
    )
    assert mypy_result == "OK"
    assert f.read_text(encoding="utf-8") == "x: int = 1\n"  # still original

    tools.commit_patches()
    assert f.read_text(encoding="utf-8") == "x: int = 99\n"
    assert tools._patch_buffer == {}


# ---------------------------------------------------------------------------
# DA4-14  fail path: stage → validate fail → rollback → disk clean
# ---------------------------------------------------------------------------

def test_da4_14_fail_path_rollback(tmp_path: os.PathLike[str]) -> None:
    tools = _reload_tools()
    f = tmp_path / "mod.py"
    f.write_text("x: int = 1\n", encoding="utf-8")

    # inject bad content directly into buffer
    bad = "def foo(x):\n    return x\n"
    tools._patch_buffer[str(f)] = bad

    mypy_result = tools.validate_staged_mypy(
        json.dumps([str(f)]),
        repo_path=str(tmp_path),
    )
    assert mypy_result != "OK"

    tools.rollback_patches()
    assert tools._patch_buffer == {}
    assert f.read_text(encoding="utf-8") == "x: int = 1\n"
