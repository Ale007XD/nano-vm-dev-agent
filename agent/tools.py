"""nano_vm_mcp.tools — MCP tool implementations."""

from __future__ import annotations

import logging
import os
import uuid
from typing import Any

try:
    import httpx

    _HTTPX_AVAILABLE = True
except ImportError:
    _HTTPX_AVAILABLE = False

from nano_vm import ExecutionVM, Program
from nano_vm.adapters import MockLLMAdapter
from pydantic import ValidationError

from .store import ProgramStore

logger = logging.getLogger(__name__)

AGENT_DEBUGGER_URL = os.getenv(
    "AGENT_DEBUGGER_URL",
    "https://agent-debugger-production.up.railway.app",
)
AGENT_DEBUGGER_TOKEN = os.getenv("AGENT_DEBUGGER_TOKEN", "")


def _has_llm_steps(program_data: dict[str, Any]) -> bool:
    """Return True if any step (including parallel sub-steps) requires an LLM."""

    def _scan(steps: list[dict[str, Any]]) -> bool:
        for step in steps:
            if step.get("type") == "llm":
                return True
            if step.get("type") == "parallel":
                if _scan(step.get("parallel_steps", [])):
                    return True
        return False

    return _scan(program_data.get("steps", []))


def _build_vm(program_data: dict[str, Any]) -> ExecutionVM | str:
    """
    Build ExecutionVM with the appropriate LLM adapter.

    - tool/condition/parallel-only programs: MockLLMAdapter("noop") — no API key needed.
    - programs with llm steps: LiteLLMAdapter from NANO_VM_MCP_LLM_MODEL env var.

    Returns ExecutionVM on success, or a str error message if llm steps are present
    but NANO_VM_MCP_LLM_MODEL is not configured.
    """
    if not _has_llm_steps(program_data):
        return ExecutionVM(llm=MockLLMAdapter("noop"))

    model = os.getenv("NANO_VM_MCP_LLM_MODEL", "")
    if not model:
        return (
            "Program contains llm steps but NANO_VM_MCP_LLM_MODEL is not set. "
            "Set NANO_VM_MCP_LLM_MODEL (e.g. 'openrouter/llama-3.3-70b-instruct:free') "
            "and the corresponding API key in your environment."
        )
    try:
        from nano_vm.adapters import LiteLLMAdapter
    except ImportError:
        return (
            "LiteLLMAdapter is not available. Install it with: pip install 'nano-vm-mcp[litellm]'"
        )
    return ExecutionVM(llm=LiteLLMAdapter(model))


def _extract_cost(trace: Any) -> float:
    """
    Извлекает стоимость из Trace совместимым способом.

    total_cost_usd — метод (callable), не property. Нужно вызывать.
    Fallback на total_cost (атрибут, старые версии).
    """
    if hasattr(trace, "total_cost_usd"):
        val = trace.total_cost_usd
        # Может быть методом или property в зависимости от версии nano_vm
        if callable(val):
            val = val()
        return float(val or 0.0)
    if hasattr(trace, "total_cost"):
        return float(trace.total_cost or 0.0)
    return 0.0


def _build_debugger_payload(trace_dict: dict[str, Any]) -> dict[str, Any]:
    """
    Build Agent Debugger /analyze payload from stored trace dict.

    Maps nano-vm Trace fields to the agreed request schema.
    FAIL:<reason> sentinels are intentional FSM outputs — not tool errors.
    """
    steps = trace_dict.get("steps", [])
    mapped_steps = []
    for i, s in enumerate(steps):
        mapped_steps.append(
            {
                "step_id": s.get("step_id", f"step_{i}"),
                "type": s.get("type", "tool"),
                "status": str(s.get("status", "UNKNOWN")).split(".")[-1],
                "output": str(s.get("output", "")),
                "retries": s.get("retry_count", 0),
                "duration_ms": s.get("duration_ms", 0),
            }
        )

    trace_id = trace_dict.get("trace_id", "")
    raw_status = str(trace_dict.get("status", "FAILED"))
    # Normalize "TraceStatus.FAILED" → "FAILED"
    status = raw_status.split(".")[-1] if "." in raw_status else raw_status
    return {
        "trace": {
            "trace_id": trace_id,
            "program_name": trace_dict.get("program_name", ""),
            "status": status,
            "steps": mapped_steps,
            "final_step": mapped_steps[-1]["step_id"] if mapped_steps else "",
            "escalations": 0,
            "blocked_actions": 0,
            "transition_entropy": trace_dict.get("transition_entropy", 0.0),
            "rollback_density": trace_dict.get("rollback_density", 0.0),
            "tool_churn_rate": trace_dict.get("tool_churn_rate", 0.0),
        },
    }


async def call_agent_debugger(trace_dict: dict[str, Any]) -> dict[str, Any]:
    """
    POST trace to Agent Debugger /analyze endpoint.

    Returns diagnostic dict or {"error": reason} if unavailable.
    Requires AGENT_DEBUGGER_TOKEN env var.
    """
    if not _HTTPX_AVAILABLE:
        return {"error": "httpx not installed — pip install httpx"}
    if not AGENT_DEBUGGER_TOKEN:
        return {"error": "AGENT_DEBUGGER_TOKEN not set"}

    payload = _build_debugger_payload(trace_dict)
    url = f"{AGENT_DEBUGGER_URL.rstrip('/')}/analyze"

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                url,
                json=payload,
                headers={"Authorization": f"Bearer {AGENT_DEBUGGER_TOKEN}"},
            )
            resp.raise_for_status()
            return resp.json()  # type: ignore[no-any-return]
    except httpx.HTTPStatusError as exc:
        return {"error": f"Agent Debugger HTTP {exc.response.status_code}: {exc.response.text}"}
    except Exception as exc:
        return {"error": f"Agent Debugger unreachable: {exc}"}


async def debug_trace(store: ProgramStore, trace_id: str) -> dict[str, Any]:
    """
    MCP tool: retrieve trace by ID and run Agent Debugger diagnostics.

    Returns combined result: trace metadata + diagnostic from Agent Debugger.
    """
    trace_dict = store.get_trace(trace_id)
    if trace_dict is None:
        return {"error": f"Trace '{trace_id}' not found"}

    diagnostic = await call_agent_debugger(trace_dict)
    return {
        "trace_id": trace_id,
        "status": trace_dict.get("status"),
        "diagnostic": diagnostic,
    }


async def run_program(
    store: ProgramStore,
    program_data: dict[str, Any],
    save_as: str = "",
    idempotency_key: str = "",
) -> dict[str, Any]:
    """
    Validate and execute a Program dict.

    Args:
        store: ProgramStore instance.
        program_data: Raw dict conforming to nano_vm.Program schema.
        save_as: Optional name to persist the program in the store.
        idempotency_key: Optional key for exactly-once execution guarantee (v0.4.0).

    Returns:
        {"trace_id": str, "program_id": str, "status": str,
         "steps": int, "cost": float, "error": str | None}
    """
    try:
        program = Program.model_validate(program_data)
    except ValidationError as exc:
        return {
            "error": f"Invalid program: {exc.error_count()} validation error(s)",
            "detail": str(exc),
        }

    program_id = str(uuid.uuid4())
    if save_as:
        store.save_program(program_id, save_as, program_data)

    vm_or_err = _build_vm(program_data)
    if isinstance(vm_or_err, str):
        return {"error": vm_or_err}

    try:
        trace = await vm_or_err.run(program)
    except Exception as exc:
        logger.exception("vm_run_failed program_id=%s", program_id)
        return {"error": f"Execution failed: {exc}", "program_id": program_id}

    # Use trace.trace_id (UUID4 assigned by ExecutionVM, OTel-ready)
    # Do NOT generate a new uuid4 — get_trace by trace_id would never match.
    trace_id = str(trace.trace_id) if hasattr(trace, "trace_id") else str(uuid.uuid4())
    trace_dict = trace.model_dump(mode="json") if hasattr(trace, "model_dump") else vars(trace)
    cost = _extract_cost(trace)

    if not save_as:
        store.save_program(program_id, "", program_data)

    store.save_trace(
        trace_id=trace_id,
        program_id=program_id,
        status=str(trace.status),
        steps_count=len(trace.steps) if hasattr(trace, "steps") else 0,
        total_cost=cost,
        trace=trace_dict,
    )

    # Record per-step transitions for transition_stats (TE-02)
    model_id = os.getenv("NANO_VM_MCP_LLM_MODEL", "__none__") or "__none__"
    steps = trace.steps if hasattr(trace, "steps") else []
    if len(steps) >= 2:
        step_ids = [s.step_id for s in steps]
        for from_s, to_s in zip(step_ids, step_ids[1:]):
            store.upsert_transition(
                program_name=program.name,
                from_step=from_s,
                to_step=to_s,
                model_id=model_id,
            )

    return {
        "trace_id": trace_id,
        "program_id": program_id,
        "status": str(trace.status),
        "steps": len(trace.steps) if hasattr(trace, "steps") else 0,
        "cost": cost,
        "error": None,
    }


async def get_trace(store: ProgramStore, trace_id: str) -> dict[str, Any]:
    """
    Retrieve a full Trace by ID.

    Returns the stored Trace JSON or {"error": "not found"}.
    """
    result = store.get_trace(trace_id)
    if result is None:
        return {"error": f"Trace '{trace_id}' not found"}
    return result


async def list_programs(store: ProgramStore) -> list[dict[str, Any]]:
    """
    List all saved programs (id, name, created_at).
    """
    return store.list_programs()


async def get_program(store: ProgramStore, program_id: str) -> dict[str, Any]:
    """
    Retrieve a saved Program JSON by ID.
    """
    result = store.get_program(program_id)
    if result is None:
        return {"error": f"Program '{program_id}' not found"}
    return result


async def delete_program(store: ProgramStore, program_id: str) -> dict[str, Any]:
    """
    Delete a program and its associated traces.

    Returns {"deleted": true} or {"error": "not found"}.
    """
    ok = store.delete_program(program_id)
    if not ok:
        return {"error": f"Program '{program_id}' not found"}
    return {"deleted": True, "program_id": program_id}
