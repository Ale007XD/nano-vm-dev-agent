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
    text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
    text = re.sub(r"\n?```$", "", text)
    return text.strip()


def _extract_json_object(text: str) -> str:
    """Extract the outermost JSON object {...} from text that may contain prose/thinking.

    Handles models that prefix output with reasoning, <tool_call> blocks, or other text
    before the actual JSON payload.

    Returns the extracted JSON substring, or the original text if no braces found.
    """
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return text
    return text[start : end + 1]


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
# apply_search_replace_patch
# ---------------------------------------------------------------------------

_SR_PATTERN = re.compile(
    r"<<<SEARCH\n(.*?)\n=======\n(.*?)\n>>>REPLACE",
    re.DOTALL,
)


def apply_search_replace_patch(file_path: str, patch_text: str, **kwargs: Any) -> str:
    """Apply a Search&Replace patch to a single file.

    Patch format (one or more blocks):

        <<<SEARCH
        <exact lines to find>
        =======
        <replacement lines>
        >>>REPLACE

    Rules:
    - Each SEARCH block must match exactly once in the file.
      If a block matches 0 or >1 times, ValueError is raised
      so FSM can retry the LLM step.
    - Blocks are applied sequentially; each block operates on the
      already-patched content from the previous block.
    - Leading/trailing whitespace on the patch_text is stripped
      before parsing, but interior whitespace is preserved exactly.

    Args:
        file_path: absolute or repo-relative path to the file to patch.
        patch_text: raw LLM output containing one or more S&R blocks.
                    Markdown fences are stripped automatically.

    Returns:
        'PATCHED: {file_path}' on success.

    Raises:
        FileNotFoundError: if file_path does not exist.
        ValueError: if patch_text contains no S&R blocks, or if any
                    SEARCH block matches 0 or >1 times in the file.
    """
    import os
    import sys

    repo_path: str = kwargs.get("repo_path", "") or ""
    full_path = file_path if os.path.isabs(file_path) else os.path.join(repo_path, file_path)

    try:
        with open(full_path, encoding="utf-8") as fh:
            content = fh.read()
    except FileNotFoundError:
        raise FileNotFoundError(f"apply_search_replace_patch: file not found: {full_path!r}")

    # Strip markdown fences if LLM wrapped output
    clean_patch = _strip_fences(patch_text)

    blocks = _SR_PATTERN.findall(clean_patch)
    if not blocks:
        raise ValueError(
            f"apply_search_replace_patch: no S&R blocks found in patch_text. "
            f"Expected format: <<<SEARCH ... ======= ... >>>REPLACE\n"
            f"Got: {clean_patch[:300]!r}"
        )

    print(f"[S&R] {full_path}: applying {len(blocks)} block(s)", file=sys.stderr)

    for idx, (search, replace) in enumerate(blocks):
        count = content.count(search)
        if count == 0:
            raise ValueError(
                f"apply_search_replace_patch: block {idx + 1} — SEARCH not found in file.\n"
                f"File: {full_path!r}\n"
                f"SEARCH ({len(search)} chars):\n{search[:200]!r}"
            )
        if count > 1:
            raise ValueError(
                f"apply_search_replace_patch: block {idx + 1} — SEARCH matches {count} times "
                f"(must match exactly once).\n"
                f"File: {full_path!r}\n"
                f"SEARCH ({len(search)} chars):\n{search[:200]!r}"
            )
        content = content.replace(search, replace, 1)
        print(f"[S&R]   block {idx + 1}: OK", file=sys.stderr)

    with open(full_path, "w", encoding="utf-8") as fh:
        fh.write(content)

    return f"PATCHED: {full_path}"


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
    import sys

    repo_path: str = kwargs.get("repo_path", "") or ""

    print(f"[DEBUG] type={type(files_json)} len={len(files_json)}", file=sys.stderr)
    print(f"[DEBUG] first_200={files_json[:200]!r}", file=sys.stderr)
    print(f"[DEBUG] last_50={files_json[-50:]!r}", file=sys.stderr)

    clean = _strip_fences(files_json)
    clean = _extract_json_object(clean)

    try:
        parsed: Any = json.loads(clean)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"write_repo_files: files_json must be JSON dict, got: {clean[:120]!r}"
        ) from exc

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
