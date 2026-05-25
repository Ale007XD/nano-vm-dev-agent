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
import re
import subprocess
from typing import Any


def _strip_fences(text: str) -> str:
    """Strip markdown code fences (```json ... ``` or ``` ... ```) from LLM output."""
    text = text.strip()
    # Remove opening fence: ```json or ```
    text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
    # Remove closing fence
    text = re.sub(r"\n?```$", "", text)
    return text.strip()


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
        test_file: absolute path to the test file.

    Returns:
        'PASS' if pytest exits 0, otherwise pytest stdout (failure details).
    """
    import os
    # Run from the directory containing the test file so imports resolve correctly
    repo_path: str = kwargs.get("repo_path", "") or str(os.path.dirname(test_file))
    cwd = repo_path if os.path.isdir(repo_path) else os.path.dirname(test_file)

    result = subprocess.run(
        ["python3", "-m", "pytest", test_file, "-v", "--tb=short"],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=cwd,
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
        files_json: JSON string of dict[str, str] — {relative_path: content}.
                    LLM markdown fences and double-encoding are handled automatically.

    Returns:
        'WRITTEN: path1, path2, ...'

    Raises:
        ValueError: if files_json cannot be parsed as JSON dict.
    """
    import os

    repo_path: str = kwargs.get("repo_path", "") or ""

    import sys
    print(f"[DEBUG] type={type(files_json)} len={len(files_json)}", file=sys.stderr)
    print(f"[DEBUG] first_200={files_json[:200]!r}", file=sys.stderr)
    print(f"[DEBUG] last_50={files_json[-50:]!r}", file=sys.stderr)

    # Strip markdown fences if LLM wrapped output
    clean = _strip_fences(files_json)

    # First parse
    try:
        parsed: Any = json.loads(clean)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"write_repo_files: files_json must be JSON dict, got: {clean[:120]!r}"
        ) from exc

    # If LLM double-encoded (returned a JSON string containing JSON), unwrap once more
    if isinstance(parsed, str):
        try:
            parsed = json.loads(parsed)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"write_repo_files: double-encoded JSON unwrap failed, got: {parsed[:120]!r}"
            ) from exc

    if not isinstance(parsed, dict):
        raise ValueError(f"write_repo_files: expected dict, got {type(parsed).__name__}")

    files: dict[str, str] = parsed

    written: list[str] = []
    for rel_path, content in files.items():
        # Resolve path relative to repo_path if not absolute
        full_path = rel_path if os.path.isabs(rel_path) else os.path.join(repo_path, rel_path)
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        with open(full_path, "w", encoding="utf-8") as fh:
            fh.write(content)
        written.append(full_path)

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
