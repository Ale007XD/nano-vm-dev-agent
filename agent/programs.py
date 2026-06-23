"""
agent/programs.py
=================
build_program_sprint(n) — generates a deterministic FSM pipeline for sprint execution
with dynamic per-file read/patch/stage steps for N target files.

Flow (DA-4 transactional patching, generalized for N files):

  For each file i in range(n):
    read_file_i → patch_file_i(llm) → stage_file_i  ← in-memory, disk untouched

  generate_test(llm) → write_test                   ← test written to disk (disposable)

  validate_staged_mypy                              ← mypy in tmpdir, disk untouched
  mypy_guard
    ↓ fail → rollback_patches → reject_mypy         ← buffer cleared, disk clean
    ↓ pass → commit_patches                         ← flush buffer to disk

  run_tests                                         ← pytest against committed files
  pytest_guard
    ↓ fail → git_checkout_files → reject_pytest     ← git restores HEAD
    ↓ pass → notify_done

Guarantees:
- Disk is never touched by source patches until mypy passes.
- If mypy fails: rollback_patches() — disk remains at HEAD.
- If pytest fails: git_checkout_files() restores committed files to HEAD.
- Test file is written directly (disposable — safe to overwrite on retry).
"""

from __future__ import annotations

from typing import Any

_REFERENCE_SECTION_TEMPLATE = (
    "## Reference files (existing project code — for context ONLY, "
    "do NOT modify these, they are not your target)\n"
    "$read_references.output\n\n"
)


def _patch_prompt(index: int, has_references: bool) -> str:
    """S&R patch prompt for an EXISTING target file at `index`."""
    ref_section = _REFERENCE_SECTION_TEMPLATE if has_references else ""
    return (
        "You are an expert Python developer.\n\n"
        "## Sprint specification\n$sprint_spec\n\n"
        f"{ref_section}"
        f"## Current file: $file_{index}_file\n$read_{index}.output\n\n"
        "## Task\n"
        "Produce a Search&Replace patch for ONLY the changes to this file "
        "described in the specification above.\n\n"
        "Rules:\n"
        "- Each SEARCH block must match exactly once in the file shown above.\n"
        "- Preserve indentation and code style exactly.\n"
        "- mypy --strict must pass after the patch (0 errors).\n"
        "- Do not copy '# type: ignore[...]' comments from the reference "
        "files unless the same mypy error genuinely applies here — an "
        "unused ignore is itself a mypy --strict error.\n"
        "- Every name you use (Enum, auto, etc.) must have a matching "
        "import in the new content — re-check imports after rewriting "
        "the header, do not drop ones still used below.\n"
        "- Output ONLY patch blocks — no explanation, no markdown prose.\n\n"
        "## Output format — CRITICAL\n"
        "Return one or more blocks in this exact format:\n\n"
        "<<<SEARCH\n"
        "<exact lines from current file>\n"
        "=======\n"
        "<replacement lines>\n"
        ">>>REPLACE\n\n"
        "Multiple blocks are allowed. No other text."
    )


def _create_prompt(index: int, has_references: bool) -> str:
    """Full-content prompt for a NEW target file at `index` (no S&R — there
    is nothing on disk yet to diff against)."""
    ref_section = _REFERENCE_SECTION_TEMPLATE if has_references else ""
    ref_rule = (
        "- Match the patterns/imports shown in the reference files above "
        "exactly — do not invent alternative import paths or redefine "
        "classes that already exist there.\n"
        if has_references
        else ""
    )
    return (
        "You are an expert Python developer.\n\n"
        "## Sprint specification\n$sprint_spec\n\n"
        f"{ref_section}"
        f"## New file to create: $file_{index}_file\n\n"
        "This file does not exist yet — there is nothing to diff against.\n\n"
        "## Task\n"
        "Write the COMPLETE content of this new file from scratch, satisfying "
        "the specification above.\n\n"
        "Rules:\n"
        f"{ref_rule}"
        "- Output ONLY the raw file content — no explanation, no markdown fences.\n"
        "- File must be syntactically complete and importable on its own.\n"
        "- Every name you use (Enum, auto, etc.) must have a matching import.\n"
        "- Do not copy '# type: ignore[...]' comments from the reference "
        "files unless the same mypy error genuinely applies here — an "
        "unused ignore is itself a mypy --strict error.\n"
        "- mypy --strict must pass (0 errors).\n"
        "- Include 'from __future__ import annotations' if the spec implies "
        "modern type hints.\n\n"
        "Output the file content now — nothing else."
    )

def _build_tail_steps(has_references: bool) -> list[dict[str, Any]]:
    """generate_test -> write_test -> validate_mypy -> commit/rollback ->
    run_tests -> pytest_guard tail (DA-4), parameterized by has_references
    so generate_test's prompt can show the same reference-files context
    the per-file patch/create steps already saw.

    generate_test fixes (DECISIONS.md 2026-06-22): without reference_files
    context and an explicit no-tool-access rule, claude-sonnet-4.6 — given
    a large, agentic-shaped prompt (spec + generated code + "Task" framing)
    — hallucinated a <tool_call>{"name": "read_file", ...} block instead of
    the requested JSON test file, because it felt it needed to inspect
    models.py and the prompt's structure made it think a file-read tool was
    available. It wasn't — StreamingLiteLLMAdapter never declares tools.
    Fix: (1) show $read_references.output here too, removing the reason to
    "go look"; (2) explicit rule forbidding tool-call syntax; (3) on_error
    retry on generate_test itself (not write_test — write_test's input is
    fixed once generate_test returns, retrying it alone re-feeds the same
    bad string every time; only re-running the LLM call can produce a
    different, hopefully well-formed, response).
    """
    ref_section = _REFERENCE_SECTION_TEMPLATE if has_references else ""
    return [
        {
            "id": "read_staged",
            "type": "tool",
            "tool": "read_staged_files",
            "args": {"paths": "$target_files"},
            "next_step": "generate_test",
        },
        {
            "id": "generate_test",
            "type": "llm",
            "prompt": (
                "You are an expert Python developer.\n\n"
                "## Sprint specification\n$sprint_spec\n\n"
                f"{ref_section}"
                "## Actual generated/patched file contents — THIS is the real "
                "API you must test, not what the spec implies\n"
                "$read_staged.output\n\n"
                "## Task\n"
                "Write the complete test file '$test_file' for the code shown above.\n\n"
                "Rules:\n"
                "- Use pytest (no unittest).\n"
                "- Use 'from __future__ import annotations' (double underscores).\n"
                "- Use ONLY the classes, constructors, and method signatures shown "
                "in the file contents above — do not invent methods, do not assume "
                "async/await unless the code above actually uses it, do not assume "
                "a different constructor shape (e.g. db_path=) than what is shown.\n"
                "- Cover all test cases listed in spec.\n"
                "- You have NO tools and NO file-reading access beyond what is shown "
                "in this prompt. Do not attempt tool_call, function_call, or any "
                "similar syntax — there is nothing on the other end to execute it. "
                "If something seems missing, use your best judgement from what is "
                "shown above and write the test anyway.\n\n"
                "## Output format — CRITICAL\n"
                "Return ONLY a valid JSON object with ONE key:\n"
                '{"$test_file": "...complete test file content..."}\n'
                "No markdown fences. No explanation. No tool calls. Pure JSON only."
            ),
            "output_key": "test_patch",
            "on_error": "retry",
            "max_retries": 2,
            "timeout_seconds": 120,
            "on_timeout": "fail",
        },
        {
            "id": "write_test",
            "type": "tool",
            "tool": "write_repo_files",
            "args": {"files_json": "$test_patch"},
            "next_step": "validate_mypy",
        },
        {
            "id": "validate_mypy",
            "type": "tool",
            "tool": "validate_staged_mypy",
            "args": {"paths": "$target_files"},
            "next_step": "mypy_guard",
        },
        {
            "id": "mypy_guard",
            "type": "condition",
            "condition": "$validate_mypy.output == 'OK'",
            "then": "commit",
            "otherwise": "do_rollback_mypy",
        },
        {
            "id": "do_rollback_mypy",
            "type": "tool",
            "tool": "rollback_patches",
            "next_step": "reject_mypy",
        },
        {
            "id": "commit",
            "type": "tool",
            "tool": "commit_patches",
            "next_step": "run_tests",
        },
        {
            "id": "run_tests",
            "type": "tool",
            "tool": "run_pytest",
            "args": {"test_file": "$test_file"},
            "next_step": "pytest_guard",
        },
        {
            "id": "pytest_guard",
            "type": "condition",
            "condition": "$run_tests.output == 'PASS'",
            "then": "notify_done",
            "otherwise": "do_rollback_pytest",
        },
        {
            "id": "do_rollback_pytest",
            "type": "tool",
            "tool": "git_checkout_files",
            "args": {"paths": "$target_files"},
            "next_step": "reject_pytest",
        },
        {
            "id": "notify_done",
            "type": "tool",
            "tool": "notify_done",
            "is_terminal": True,
        },
        {
            "id": "reject_mypy",
            "type": "tool",
            "tool": "notify_rejected_mypy",
            "is_terminal": True,
        },
        {
            "id": "reject_pytest",
            "type": "tool",
            "tool": "notify_rejected_pytest",
            "is_terminal": True,
        },
    ]


def _file_steps(
    index: int, next_after: str, is_new: bool, has_references: bool
) -> list[dict[str, Any]]:
    """Build the step chain for the target file at `index`.

    Two shapes, chosen by `is_new`:
      existing file: read_{i} -> patch_{i}[S&R, llm] -> stage_{i}[stage_patch]
      new file:                  patch_{i}[full-content, llm] -> stage_{i}[stage_new_file]

    A brand-new file has no prior content to diff against, so there is no
    read step and no Search&Replace — the LLM emits the complete file body
    and stage_new_file() buffers it directly (DECISIONS.md 2026-06-20:
    sprint_m1_inventory_promotions — dev-agent's S&R pipeline assumed every
    target file pre-exists; new-file creation is a distinct mechanism, not
    a variant of patching).

    Args:
        index:          0-based position of this file in target_files.
        next_after:     step id to jump to after staging — the next file's
                        first step (read_{i+1} or patch_{i+1} depending on
                        *its* is_new flag), or 'generate_test' for the last file.
        is_new:         True if this target file does not exist on disk yet.
        has_references: True if a read_references step precedes the file
                        chain — both prompt variants then include the
                        reference-files section (DECISIONS.md 2026-06-21:
                        new-file prompts with zero codebase context produced
                        3 different wrong import paths / 2 duplicate BaseFSM
                        reimplementations across 4 sibling files).
    """
    if is_new:
        prompt = _create_prompt(index, has_references)
        return [
            {
                "id": f"patch_{index}",
                "type": "llm",
                "prompt": prompt,
                "output_key": f"file_{index}_patch",
                "on_error": "retry",
                "max_retries": 2,
                "timeout_seconds": 120,
                "on_timeout": "fail",
            },
            {
                "id": f"stage_{index}",
                "type": "tool",
                "tool": "stage_new_file",
                "args": {
                    "file_path": f"$file_{index}_file",
                    "content": f"$file_{index}_patch",
                },
                "on_error": "retry",
                "max_retries": 2,
                "next_step": next_after,
            },
        ]

    prompt = _patch_prompt(index, has_references)
    return [
        {
            "id": f"read_{index}",
            "type": "tool",
            "tool": "read_repo_files",
            "args": {"paths": f"$file_{index}_paths"},
        },
        {
            "id": f"patch_{index}",
            "type": "llm",
            "prompt": prompt,
            "output_key": f"file_{index}_patch",
            "on_error": "retry",
            "max_retries": 2,
            "timeout_seconds": 120,
            "on_timeout": "fail",
        },
        {
            "id": f"stage_{index}",
            "type": "tool",
            "tool": "stage_patch",
            "args": {
                "file_path": f"$file_{index}_file",
                "patch_text": f"$file_{index}_patch",
            },
            "on_error": "retry",
            "max_retries": 2,
            "next_step": next_after,
        },
    ]


def _first_step_id(index: int, is_new_flags: list[bool]) -> str:
    """First step id for target file `index`, depending on its is_new flag."""
    return f"patch_{index}" if is_new_flags[index] else f"read_{index}"


def build_program_sprint(
    num_files: int | list[bool],
    reference_files: list[str] | None = None,
) -> dict[str, Any]:
    """Build the sprint FSM program for an arbitrary number of target files.

    Generates one (read->patch[S&R]->stage) or (patch[full]->stage_new)
    chain per target file — existing vs new, respectively — then continues
    into the shared generate_test -> validate_mypy -> commit/rollback ->
    run_tests -> pytest_guard tail (DA-4, unchanged).

    If `reference_files` is non-empty, a single 'read_references' step runs
    first (reads all of them via read_repo_files in one call), and every
    per-file prompt (both S&R and full-content) includes a "Reference files"
    section showing their content — read-only context, never a patch target.
    Without this, new-file prompts have zero visibility into existing
    project conventions (canonical base classes, import paths) and the LLM
    fabricates its own — validated failure mode, see DECISIONS.md
    2026-06-21 (sprint_m1_inventory_promotions: 4 new FSM files, 3 different
    wrong import paths for BaseFSM + 2 files reimplementing it locally).

    Required context variables (in addition to the shared ones consumed
    by the tail — sprint_spec, target_files, test_file, repo_path):

        file_{i}_file   : str            — plain path, for stage + prompt label
        file_{i}_paths  : JSON list[str] — for read_repo_files, EXISTING
                          files only (new files skip the read step entirely)
        reference_paths : JSON list[str] — only if reference_files given

    Args:
        num_files: either a plain int (back-compat — all files treated as
                   existing, legacy 3-file shape if called with 3), or a
                   list[bool] of per-file is_new flags (preferred — caller
                   should compute these from os.path.exists() at sprint
                   start, see agent/runner.py).
        reference_files: optional list of existing repo-relative or absolute
                   paths to show every per-file prompt as read-only context
                   (e.g. a canonical base class + one reference implementation
                   the new files are expected to follow).

    Raises:
        ValueError: if num_files (or len(is_new_flags)) < 1.
    """
    if isinstance(num_files, int):
        if num_files < 1:
            raise ValueError("build_program_sprint: num_files must be >= 1")
        is_new_flags: list[bool] = [False] * num_files
    else:
        is_new_flags = list(num_files)
        if len(is_new_flags) < 1:
            raise ValueError("build_program_sprint: num_files must be >= 1")

    has_references = bool(reference_files)
    n = len(is_new_flags)
    steps: list[dict[str, Any]] = []

    first_file_step = _first_step_id(0, is_new_flags)
    if has_references:
        steps.append(
            {
                "id": "read_references",
                "type": "tool",
                "tool": "read_repo_files",
                "args": {"paths": "$reference_paths"},
                "next_step": first_file_step,
            }
        )

    for i in range(n):
        next_after = _first_step_id(i + 1, is_new_flags) if i + 1 < n else "read_staged"
        steps.extend(_file_steps(i, next_after, is_new_flags[i], has_references))
    steps.extend(_build_tail_steps(has_references))

    return {"name": "sprint_execution", "steps": steps}


PROGRAM_SPRINT: dict[str, Any] = build_program_sprint(3)
