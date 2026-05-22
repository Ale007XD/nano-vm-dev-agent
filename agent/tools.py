"""
agent/tools.py
==============
Four synchronous tool functions for the FSM dev-agent pipeline.

All functions accept **kwargs because ExecutionVM passes the full context
to every tool call. Arguments that carry data are passed as JSON strings
to stay compatible with DSL $var resolution (which always produces str).

subprocess timeout: 120s per call — guards against hanging linters/tests.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any


# ---------------------------------------------------------------------------
# read_repo_files
# ---------------------------------------------------------------------------

def read_repo_files(paths: str, **kwargs: Any) -> str:
    """Read source files from the repository.

    Args:
        paths: JSON string of list[str] — file paths to read.

    Returns:
        Concatenated file contents separated by '### FILE: {path}' headers.

    Raises:
        FileNotFoundError: if any path does not exist.
        ValueError: if paths is not valid JSON list.
    """
    try:
        path_list: list[str] = json.loads(paths)
    except json.JSONDecodeError as exc:
        raise ValueError(f"read_repo_files: paths must be JSON list, got: {paths!r}") from exc

    parts: list[str] = []
    for path in path_list:
        try:
            with open(path, encoding="utf-8") as fh:
                content = fh.read()
        except FileNotFoundError:
            raise FileNotFoundError(f"read_repo_files: file not found: {path!r}")
        parts.append(f"### FILE: {path}\n{content}")

    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# run_mypy
# ---------------------------------------------------------------------------

def run_mypy(paths: str, **kwargs: Any) -> str:
    """Run mypy --strict on given paths.

    Args:
        paths: JSON string of list[str] — paths to type-check.

    Returns:
        'OK' if mypy exits 0, otherwise mypy stdout (error lines).

    Raises:
        ValueError: if paths is not valid JSON list.
    """
    try:
        path_list: list[str] = json.loads(paths)
    except json.JSONDecodeError as exc:
        raise ValueError(f"run_mypy: paths must be JSON list, got: {paths!r}") from exc

    result = subprocess.run(
        ["mypy", "--strict", "--ignore-missing-imports", *path_list],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode == 0:
        return "OK"
    return result.stdout or result.stderr


# ---------------------------------------------------------------------------
# run_pytest
# ---------------------------------------------------------------------------

def run_pytest(test_file: str, **kwargs: Any) -> str:
    """Run pytest on a single test file.

    Args:
        test_file: path to the test file.

    Returns:
        'PASS' if pytest exits 0, otherwise pytest stdout (failure details).
    """
    result = subprocess.run(
        ["pytest", test_file, "-v", "--tb=short"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode == 0:
        return "PASS"
    return result.stdout or result.stderr


# ---------------------------------------------------------------------------
# write_repo_files
# ---------------------------------------------------------------------------

def write_repo_files(files_json: str, **kwargs: Any) -> str:
    """Write patched files to disk.

    Args:
        files_json: JSON string of dict[str, str] — {path: content}.

    Returns:
        'WRITTEN: path1, path2, ...'

    Raises:
        ValueError: if files_json is not valid JSON dict.
    """
    try:
        files: dict[str, str] = json.loads(files_json)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"write_repo_files: files_json must be JSON dict, got: {files_json[:120]!r}"
        ) from exc

    if not isinstance(files, dict):
        raise ValueError(f"write_repo_files: expected dict, got {type(files).__name__}")

    written: list[str] = []
    for path, content in files.items():
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
        written.append(path)

    return f"WRITTEN: {', '.join(written)}"


# ---------------------------------------------------------------------------
# notify helpers (terminal leaf steps)
# ---------------------------------------------------------------------------

def notify_rejected_mypy(**kwargs: Any) -> str:
    """Terminal step: log mypy rejection."""
    print("REJECTED: mypy failed — patch not applied")
    return "REJECTED_MYPY"


def notify_rejected_pytest(**kwargs: Any) -> str:
    """Terminal step: log pytest rejection."""
    print("REJECTED: pytest failed — patch not applied")
    return "REJECTED_PYTEST"


def notify_done(**kwargs: Any) -> str:
    """Terminal step: log successful patch application."""
    print("DONE: patch applied successfully")
    return "DONE"
