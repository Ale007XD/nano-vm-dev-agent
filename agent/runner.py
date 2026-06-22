"""
agent/runner.py
===============
Public API for the nano-vm dev-agent.

Provider resolution order (first configured wins):
  1. Vibecode proxy  — VIBECODE_API_KEY  (openai/claude-sonnet-4.6)
  2. OpenRouter      — OPENROUTER_API_KEY (configurable model, free tier supported)
  3. Anthropic API   — ANTHROPIC_API_KEY  (claude-sonnet-4-20250514)

Override provider explicitly via NANO_VM_AGENT_PROVIDER=vibecode|openrouter|anthropic.
Override model via NANO_VM_AGENT_MODEL=<litellm model string>.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from typing import Any, cast

from nano_vm.adapters.base import LLMAdapter
from nano_vm.adapters.litellm_adapter import LiteLLMAdapter
from nano_vm.models import Program, Trace
from nano_vm.vm import ExecutionVM

from .programs import build_program_sprint
from .streaming_adapter import StreamingLiteLLMAdapter
from .tools import (
    apply_search_replace_patch,
    clear_fingerprints,
    commit_patches,
    git_checkout_files,
    notify_done,
    notify_rejected_mypy,
    notify_rejected_pytest,
    read_repo_files,
    rollback_patches,
    run_mypy,
    run_pytest,
    stage_new_file,
    stage_patch,
    validate_staged_mypy,
    write_repo_files,
)

# ---------------------------------------------------------------------------
# Provider defaults
# ---------------------------------------------------------------------------

_PROVIDER_DEFAULTS: dict[str, dict[str, Any]] = {
    "vibecode": {
        "model": "openai/claude-sonnet-4.6",
        "api_base": "https://api.vibecode-claude.online/v1",
        "api_key_env": "VIBECODE_API_KEY",
        "kwargs": {"stream": True, "timeout": 300},
    },
    "openrouter": {
        "model": "openrouter/meta-llama/llama-3.3-70b-instruct:free",
        "api_base": "https://openrouter.ai/api/v1",
        "api_key_env": "OPENROUTER_API_KEY",
        "kwargs": {"timeout": 120},
    },
    "anthropic": {
        "model": "claude-sonnet-4-20250514",
        "api_base": None,
        "api_key_env": "ANTHROPIC_API_KEY",
        "kwargs": {"timeout": 300},
    },
}

# Free-tier OpenRouter models suitable for code tasks
OPENROUTER_FREE_MODELS = [
    "openrouter/meta-llama/llama-3.3-70b-instruct:free",
    "openrouter/google/gemma-3-27b-it:free",
    "openrouter/mistralai/mistral-7b-instruct:free",
    "openrouter/qwen/qwen-2.5-72b-instruct:free",
]


def _resolve_provider() -> tuple[str, dict[str, Any]]:
    """Resolve active provider from environment.

    Returns (provider_name, config_dict).
    Raises RuntimeError if no provider is configured.
    """
    explicit = os.environ.get("NANO_VM_AGENT_PROVIDER", "").lower().strip()

    # If explicit provider requested — use it or fail fast
    if explicit:
        if explicit not in _PROVIDER_DEFAULTS:
            raise RuntimeError(
                f"Unknown NANO_VM_AGENT_PROVIDER={explicit!r}. "
                f"Valid: {list(_PROVIDER_DEFAULTS)}"
            )
        cfg = _PROVIDER_DEFAULTS[explicit]
        key = os.environ.get(cfg["api_key_env"], "")
        if not key:
            raise RuntimeError(
                f"NANO_VM_AGENT_PROVIDER={explicit} but {cfg['api_key_env']} is not set."
            )
        return explicit, cfg

    # Auto-detect: first configured provider wins
    for name, cfg in _PROVIDER_DEFAULTS.items():
        key = os.environ.get(cfg["api_key_env"], "")
        if key:
            return name, cfg

    raise RuntimeError(
        "No LLM provider configured. Set one of: "
        "VIBECODE_API_KEY, OPENROUTER_API_KEY, ANTHROPIC_API_KEY"
    )


def build_adapter(
    llm_model: str | None = None,
    adapter_kwargs: dict[str, Any] | None = None,
) -> tuple[LLMAdapter, str]:
    """Build LiteLLMAdapter from environment configuration.

    Args:
        llm_model: override model string (litellm format). If None, uses provider default
                   or NANO_VM_AGENT_MODEL env var.
        adapter_kwargs: extra kwargs merged into provider defaults.

    Returns:
        (adapter, provider_name) — adapter ready to use, provider name for logging.
    """
    provider_name, cfg = _resolve_provider()

    # Model resolution: explicit arg > env var > provider default
    model = (
        llm_model
        or os.environ.get("NANO_VM_AGENT_MODEL", "")
        or cfg["model"]
    )

    api_key = os.environ.get(cfg["api_key_env"], "")
    api_base: str | None = cfg["api_base"]

    # Set litellm env vars before adapter init (litellm reads them at call time)
    if provider_name == "vibecode":
        os.environ["OPENAI_API_KEY"] = api_key
        os.environ["OPENAI_API_BASE"] = api_base or ""
    elif provider_name == "openrouter":
        os.environ["OPENROUTER_API_KEY"] = api_key
    elif provider_name == "anthropic":
        os.environ["ANTHROPIC_API_KEY"] = api_key

    # Build kwargs: provider defaults + caller overrides
    kwargs: dict[str, Any] = {**cfg["kwargs"], **(adapter_kwargs or {})}
    if api_base and provider_name not in ("anthropic",):
        kwargs["api_base"] = api_base

    # LiteLLMAdapter.complete() (nano_vm core) does response.choices[0]...
    # unconditionally — correct for stream=False, but litellm.acompletion()
    # returns a CustomStreamWrapper (no .choices) when stream=True, raising
    # AttributeError. Vibecode's stream=True is not optional (proxy enforces
    # a ~100s non-streaming timeout — see streaming_adapter.py docstring),
    # so route through StreamingLiteLLMAdapter, which consumes the stream
    # correctly, instead of disabling streaming.
    wants_stream = bool(kwargs.pop("stream", False))
    if wants_stream:
        adapter: LLMAdapter = cast(
            LLMAdapter, StreamingLiteLLMAdapter(model, **kwargs)
        )
    else:
        adapter = cast(LLMAdapter, LiteLLMAdapter(model, **kwargs))

    return adapter, provider_name


async def run_sprint(
    sprint_spec: str,
    target_files: list[str],
    test_file: str,
    llm_model: str | None = None,
    repo_path: str = ".",
    adapter_kwargs: dict[str, Any] | None = None,
    reference_files: list[str] | None = None,
) -> Trace:
    """Run a dev-agent sprint using the configured LLM provider.

    Provider is resolved automatically from environment variables.
    See module docstring for resolution order.

    Args:
        sprint_spec:    Natural language description of the sprint task.
        target_files:   List of source files to patch (repo-relative or absolute).
        test_file:      Path to the test file to generate/run.
        llm_model:      Override LLM model string. If None, uses provider default
                        or NANO_VM_AGENT_MODEL env var.
        repo_path:      Root of the repository being patched.
        adapter_kwargs: Extra kwargs forwarded to LiteLLMAdapter.
        reference_files: Optional existing repo files (e.g. a canonical base
                        class + one reference implementation) shown to every
                        per-file prompt as read-only context. Without this,
                        new-file prompts have zero visibility into existing
                        project conventions and the LLM fabricates its own
                        import paths / reimplements shared base classes
                        (DECISIONS.md 2026-06-21, sprint_m1_inventory_promotions).

    Returns:
        Trace from ExecutionVM.run().
    """
    clear_fingerprints()
    adapter, provider_name = build_adapter(llm_model, adapter_kwargs)
    print(f"[runner] provider={provider_name}")

    tools: dict[str, Callable[..., Any]] = {
        "read_repo_files":            read_repo_files,
        "apply_search_replace_patch": apply_search_replace_patch,
        "stage_patch":                stage_patch,
        "stage_new_file":             stage_new_file,
        "validate_staged_mypy":       validate_staged_mypy,
        "commit_patches":             commit_patches,
        "rollback_patches":           rollback_patches,
        "git_checkout_files":         git_checkout_files,
        "run_mypy":                   run_mypy,
        "run_pytest":                 run_pytest,
        "write_repo_files":           write_repo_files,
        "notify_rejected_mypy":       notify_rejected_mypy,
        "notify_rejected_pytest":     notify_rejected_pytest,
        "notify_done":                notify_done,
    }

    vm = ExecutionVM(llm=adapter, tools=tools)

    abs_repo = os.path.abspath(repo_path)
    resolved = [
        p if os.path.isabs(p) else os.path.join(abs_repo, p)
        for p in target_files
    ]
    test_file_resolved = (
        test_file if os.path.isabs(test_file) else os.path.join(abs_repo, test_file)
    )

    # A target file that doesn't exist on disk yet is being created from
    # scratch this sprint, not patched — see build_program_sprint() docstring
    # and DECISIONS.md 2026-06-20 (sprint_m1_inventory_promotions: new FSM
    # modules had no prior content for stage_patch's S&R diff to apply to).
    is_new_flags = [not os.path.isfile(p) for p in resolved]
    if any(is_new_flags):
        print(
            f"[runner] new files (no S&R, full-content generation): "
            f"{[p for p, n in zip(resolved, is_new_flags) if n]}"
        )

    resolved_refs = [
        p if os.path.isabs(p) else os.path.join(abs_repo, p)
        for p in (reference_files or [])
    ]
    if resolved_refs:
        print(f"[runner] reference files (read-only context): {resolved_refs}")

    program = Program.from_dict(build_program_sprint(is_new_flags, resolved_refs))

    context: dict[str, str] = {
        "sprint_spec":  sprint_spec,
        "target_files": json.dumps(resolved),
        "test_file":    test_file_resolved,
        "repo_path":    abs_repo,
    }
    if resolved_refs:
        context["reference_paths"] = json.dumps(resolved_refs)
    for i, (path, is_new) in enumerate(zip(resolved, is_new_flags)):
        context[f"file_{i}_file"] = path
        if not is_new:
            context[f"file_{i}_paths"] = json.dumps([path])

    return await vm.run(program, context=context)
