"""
agent/tools.py
==============
Synchronous tool functions for the FSM dev-agent pipeline.

All functions accept **kwargs because ExecutionVM passes the full context
to every tool call. Arguments that carry data are passed as JSON strings
to stay compatible with DSL $var resolution (which always produces str).

subprocess timeout: 120s per call — guards against hanging linters/tests.

Transactional patching (DA-4):
  stage_patch()          — S&R in-memory → _patch_buffer[path] = patched content
  validate_staged_mypy() — copy buffer to tmpdir, run mypy there (disk untouched)
  commit_patches()       — flush _patch_buffer to disk atomically, clear buffer
  rollback_patches()     — clear _patch_buffer without writing to disk
  git_checkout_files()   — git checkout safety net after pytest-fail post-commit
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from typing import Any


def _strip_fences(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
    text = re.sub(r"\n?```$", "", text)
    return text.strip()


def _extract_json_object(text: str) -> str:
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return text
    return text[start : end + 1]


# ---------------------------------------------------------------------------
# Transactional patch buffer
# ---------------------------------------------------------------------------

_patch_buffer: dict[str, str] = {}


# ---------------------------------------------------------------------------
# FailureFingerprint
# ---------------------------------------------------------------------------



@dataclass(frozen=True)
class FailureFingerprint:
    """Canonical fingerprint for a repeating failure pattern.

    Fields:
        tool:        Tool name that failed (e.g. 'run_mypy', 'write_repo_files').
        error_class: Error category (e.g. 'arg-type', 'expected_dict', 'no_choices').
        pattern:     Optional sub-pattern wildcard; '*' means any value matches.

    Key format: '{tool}:{error_class}:{pattern}'
    """

    tool: str
    error_class: str
    pattern: str = "*"

    def key(self) -> str:
        return f"{self.tool}:{self.error_class}:{self.pattern}"


# Module-level seen-fingerprints state (reset per process / test via clear_fingerprints)
_seen_fingerprints: set[str] = set()

# Known fingerprints that indicate non-convergent failure (escalate, not retry)
KNOWN_FINGERPRINTS: frozenset[str] = frozenset(
    [
        "mypy:arg-type:*",
        "write_repo_files:expected_dict:*",
        "CustomStreamWrapper:no_choices:*",
    ]
)


def record_fingerprint(fp: FailureFingerprint) -> None:
    """Record a fingerprint as seen in the current agent run."""
    _seen_fingerprints.add(fp.key())


def check_fingerprint(fp: FailureFingerprint) -> bool:
    """Return True if this fingerprint has been seen before → caller should ESCALATE."""
    return fp.key() in _seen_fingerprints


def clear_fingerprints() -> None:
    """Reset seen-fingerprints state (call at sprint start or in tests)."""
    _seen_fingerprints.clear()


def get_seen_fingerprints() -> frozenset[str]:
    """Return immutable snapshot of currently seen fingerprints."""
    return frozenset(_seen_fingerprints)


# ---------------------------------------------------------------------------
# S&R core
# ---------------------------------------------------------------------------

_SR_PATTERN = re.compile(
    r"<<<SEARCH\n(.*?)\n=======\n(.*?)\n>>>REPLACE",
    re.DOTALL,
)


def _apply_sr_blocks(content: str, clean_patch: str, label: str) -> str:
    """Apply all S&R blocks to content string; raise ValueError on mismatch."""
    import sys

    blocks = _SR_PATTERN.findall(clean_patch)
    if not blocks:
        raise ValueError(
            f"no S&R blocks found in patch_text. "
            f"Expected: <<<SEARCH ... ======= ... >>>REPLACE\n"
            f"Got: {clean_patch[:300]!r}"
        )

    print(f"[S&R] {label}: applying {len(blocks)} block(s)", file=sys.stderr)

    for idx, (search, replace) in enumerate(blocks):
        count = content.count(search)
        if count == 0:
            raise ValueError(
                f"block {idx + 1} — SEARCH not found.\n"
                f"File: {label!r}\n"
                f"SEARCH ({len(search)} chars):\n{search[:200]!r}"
            )
        if count > 1:
            raise ValueError(
                f"block {idx + 1} — SEARCH matches {count} times (must be exactly 1).\n"
                f"File: {label!r}\n"
                f"SEARCH ({len(search)} chars):\n{search[:200]!r}"
            )
        content = content.replace(search, replace, 1)
        print(f"[S&R]   block {idx + 1}: OK", file=sys.stderr)

    return content


# ---------------------------------------------------------------------------
# read_repo_files
# ---------------------------------------------------------------------------

def read_repo_files(paths: str, **kwargs: Any) -> str:
    """Read source files from the repository.

    Args:
        paths: JSON string of list[str] — file paths to read.

    Returns:
        Concatenated file contents separated by '### FILE: {path}' headers.
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
# apply_search_replace_patch  (direct write — kept for backward compat)
# ---------------------------------------------------------------------------

def apply_search_replace_patch(file_path: str, patch_text: str, **kwargs: Any) -> str:
    """Apply a Search&Replace patch directly to a file on disk (immediate write)."""
    import os

    repo_path: str = kwargs.get("repo_path", "") or ""
    full_path = file_path if os.path.isabs(file_path) else os.path.join(repo_path, file_path)

    try:
        with open(full_path, encoding="utf-8") as fh:
            content = fh.read()
    except FileNotFoundError:
        raise FileNotFoundError(f"apply_search_replace_patch: file not found: {full_path!r}")

    clean_patch = _strip_fences(patch_text)
    content = _apply_sr_blocks(content, clean_patch, full_path)

    with open(full_path, "w", encoding="utf-8") as fh:
        fh.write(content)

    return f"PATCHED: {full_path}"


# ---------------------------------------------------------------------------
# stage_patch
# ---------------------------------------------------------------------------

def stage_patch(file_path: str, patch_text: str, **kwargs: Any) -> str:
    """Apply S&R patch in-memory; buffer result without writing to disk.

    Reads from _patch_buffer if the file was already staged in this sprint,
    otherwise reads from disk. Multiple stage_patch calls on the same file
    accumulate correctly.

    Returns:
        'STAGED: {full_path}'
    """
    import os
    import sys

    repo_path: str = kwargs.get("repo_path", "") or ""
    full_path = file_path if os.path.isabs(file_path) else os.path.join(repo_path, file_path)

    if full_path in _patch_buffer:
        content = _patch_buffer[full_path]
        print(f"[STAGE] {full_path}: reading from buffer", file=sys.stderr)
    else:
        try:
            with open(full_path, encoding="utf-8") as fh:
                content = fh.read()
        except FileNotFoundError:
            raise FileNotFoundError(f"stage_patch: file not found: {full_path!r}")

    clean_patch = _strip_fences(patch_text)
    patched = _apply_sr_blocks(content, clean_patch, full_path)

    _patch_buffer[full_path] = patched
    print(
        f"[STAGE] {full_path}: staged ({len(_patch_buffer)} file(s) in buffer)",
        file=sys.stderr,
    )
    return f"STAGED: {full_path}"


# ---------------------------------------------------------------------------
# validate_staged_mypy
# ---------------------------------------------------------------------------

def validate_staged_mypy(paths: str, **kwargs: Any) -> str:
    """Run mypy --strict against staged buffer content without touching disk.

    Copies _patch_buffer files into a tmpdir overlaid on the repo, then runs
    mypy from there. Disk is never modified. Buffer is never cleared.

    Args:
        paths: JSON string of list[str] — repo-relative paths to type-check.
               These are the paths mypy will be invoked on (inside tmpdir).

    Returns:
        'OK' if mypy exits 0, otherwise mypy stdout (error lines).
    """
    import os
    import shutil
    import sys
    import tempfile

    repo_path: str = kwargs.get("repo_path", "") or ""

    try:
        path_list: list[str] = json.loads(paths)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"validate_staged_mypy: paths must be JSON list, got: {paths!r}"
        ) from exc

    with tempfile.TemporaryDirectory(prefix="nano_vm_mypy_") as tmpdir:
        # 1. Copy entire repo into tmpdir so imports resolve correctly
        if repo_path and os.path.isdir(repo_path):
            shutil.copytree(
                repo_path,
                tmpdir,
                dirs_exist_ok=True,
                ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"),
            )

        # 2. Overlay staged buffer — overwrite copied files with in-memory content
        for buf_path, buf_content in _patch_buffer.items():
            if repo_path and buf_path.startswith(repo_path):
                rel = os.path.relpath(buf_path, repo_path)
            else:
                rel = buf_path
            dest = os.path.join(tmpdir, rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "w", encoding="utf-8") as fh:
                fh.write(buf_content)
            print(f"[MYPY-STAGE] overlaid {rel}", file=sys.stderr)

        # 3. Resolve check paths relative to tmpdir
        check_paths: list[str] = []
        for p in path_list:
            if os.path.isabs(p) and repo_path and p.startswith(repo_path):
                rel = os.path.relpath(p, repo_path)
                check_paths.append(os.path.join(tmpdir, rel))
            elif not os.path.isabs(p):
                check_paths.append(os.path.join(tmpdir, p))
            else:
                check_paths.append(p)

        result = subprocess.run(
            ["mypy", "--strict", "--ignore-missing-imports", *check_paths],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=tmpdir,
        )

    if result.returncode == 0:
        return "OK"
    return result.stdout or result.stderr


# ---------------------------------------------------------------------------
# commit_patches
# ---------------------------------------------------------------------------

def commit_patches(**kwargs: Any) -> str:
    """Flush _patch_buffer to disk atomically, then clear buffer.

    If any write fails, buffer is NOT cleared so the caller can retry.

    Returns:
        'COMMITTED: path1, path2, ...' or 'COMMITTED: (nothing staged)'.
    """
    import os
    import sys

    if not _patch_buffer:
        print("[COMMIT] buffer empty — nothing to write", file=sys.stderr)
        return "COMMITTED: (nothing staged)"

    written: list[str] = []
    for full_path, content in _patch_buffer.items():
        os.makedirs(os.path.dirname(full_path) or ".", exist_ok=True)
        with open(full_path, "w", encoding="utf-8") as fh:
            fh.write(content)
        written.append(full_path)
        print(f"[COMMIT] wrote {full_path}", file=sys.stderr)

    _patch_buffer.clear()
    print(f"[COMMIT] done — {len(written)} file(s) written", file=sys.stderr)
    return f"COMMITTED: {', '.join(written)}"


# ---------------------------------------------------------------------------
# rollback_patches
# ---------------------------------------------------------------------------

def rollback_patches(**kwargs: Any) -> str:
    """Clear _patch_buffer without writing anything to disk.

    Returns:
        'ROLLED_BACK: path1, path2, ...' or 'ROLLED_BACK: (nothing staged)'.
    """
    import sys

    if not _patch_buffer:
        print("[ROLLBACK] buffer empty — nothing to discard", file=sys.stderr)
        return "ROLLED_BACK: (nothing staged)"

    discarded = list(_patch_buffer.keys())
    _patch_buffer.clear()
    print(f"[ROLLBACK] discarded {len(discarded)} file(s): {discarded}", file=sys.stderr)
    return f"ROLLED_BACK: {', '.join(discarded)}"


# ---------------------------------------------------------------------------
# git_checkout_files  (safety net for pytest-fail post-commit)
# ---------------------------------------------------------------------------

def git_checkout_files(paths: str, **kwargs: Any) -> str:
    """Run 'git checkout -- <paths>' to restore files to HEAD.

    Called only after commit_patches() when pytest fails — restores
    committed files back to their pre-patch state from git HEAD.

    Args:
        paths: JSON string of list[str] — repo-relative paths to restore.

    Returns:
        'RESTORED: path1, path2, ...'
    """
    import os
    import sys

    try:
        path_list: list[str] = json.loads(paths)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"git_checkout_files: paths must be JSON list, got: {paths!r}"
        ) from exc

    repo_path: str = kwargs.get("repo_path", "") or ""
    cwd = repo_path if repo_path and os.path.isdir(repo_path) else None

    result = subprocess.run(
        ["git", "checkout", "--", *path_list],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=cwd,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git_checkout_files: exit {result.returncode}:\n"
            f"{result.stderr or result.stdout}"
        )

    print(f"[GIT] restored {path_list}", file=sys.stderr)
    return f"RESTORED: {', '.join(path_list)}"


# ---------------------------------------------------------------------------
# run_mypy  (direct — runs against disk; use validate_staged_mypy pre-commit)
# ---------------------------------------------------------------------------

def run_mypy(paths: str, **kwargs: Any) -> str:
    """Run mypy --strict on given paths (disk).

    Returns:
        'OK' if mypy exits 0, otherwise mypy stdout.
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

    Returns:
        'PASS' if pytest exits 0, otherwise pytest stdout.
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
    """Write files to disk from JSON dict {path: content}.

    Returns:
        'WRITTEN: path1, path2, ...'
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
    print("REJECTED: mypy failed — patch rolled back")
    return "REJECTED_MYPY"


def notify_rejected_pytest(**kwargs: Any) -> str:
    print("REJECTED: pytest failed — patch rolled back via git checkout")
    return "REJECTED_PYTEST"


def notify_done(**kwargs: Any) -> str:
    print("DONE: patch committed successfully")
    return "DONE"
