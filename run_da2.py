"""
run_da2.py
==========
DA-2: боевой прогон nano-vm-dev-agent на спринт idempotency_store.

Провайдер: Vibecode (https://api.vibecode-claude.online/v1)

Переменные окружения:
  VIBECODE_API_KEY       — ключ Vibecode (sk-...)
  NANO_VM_MCP_REPO_PATH  — путь к репо nano-vm-mcp (default: ~/nano-vm-mcp)

Запуск:
  export VIBECODE_API_KEY=sk-...
  export NANO_VM_MCP_REPO_PATH=~/nano-vm-mcp
  python run_da2.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import textwrap
from pathlib import Path

# ---------------------------------------------------------------------------
# Vibecode provider — ДО любых импортов litellm/nano_vm
# litellm использует OPENAI_API_KEY + OPENAI_API_BASE для openai-compat маршрута
# ---------------------------------------------------------------------------
_VIBECODE_BASE  = "https://api.vibecode-claude.online/v1"
_VIBECODE_MODEL = "openai/claude-sonnet-4.6"

_api_key = os.environ.get("VIBECODE_API_KEY") or os.environ.get("ANTHROPIC_API_KEY", "")
if not _api_key:
    print("ERROR: set VIBECODE_API_KEY (export VIBECODE_API_KEY=sk-...)")
    sys.exit(1)

os.environ["OPENAI_API_KEY"]  = _api_key
os.environ["OPENAI_API_BASE"] = _VIBECODE_BASE

# ---------------------------------------------------------------------------
# Sprint spec
# ---------------------------------------------------------------------------
SPRINT_SPEC = textwrap.dedent("""
{
  "sprint": "idempotency_store",
  "repo": "nano-vm-mcp",
  "version": "v0.4.0",

  "goal": "Межсессионная exactly-once гарантия для run_program через idempotency_key.",

  "schema": {
    "table": "idempotency_keys",
    "columns": {
      "idempotency_key": "TEXT PRIMARY KEY",
      "execution_id":    "TEXT NOT NULL",
      "status":          "TEXT NOT NULL DEFAULT 'pending'",
      "result_json":     "TEXT",
      "created_at":      "TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))",
      "expires_at":      "TEXT"
    },
    "note": "Add CREATE TABLE IF NOT EXISTS to _init_schema in store.py"
  },

  "store_methods": {
    "save_idempotency_key": {
      "args": ["key: str", "execution_id: str", "status: str", "result: dict | None", "expires_at: str | None"],
      "behaviour": "INSERT OR REPLACE — upsert; result stored as JSON string or NULL"
    },
    "get_idempotency_key": {
      "args": ["key: str"],
      "returns": "dict | None",
      "fields": ["idempotency_key", "execution_id", "status", "result_json (parsed)", "created_at", "expires_at"]
    },
    "delete_idempotency_key": {
      "args": ["key: str"],
      "returns": "bool — True if row existed"
    }
  },

  "handler_logic": {
    "where": "GovernedRunProgramHandler._try_handle (handlers.py)",
    "arg": "arguments.get('idempotency_key', '') — optional str from MCP caller",
    "flow": [
      "1. If idempotency_key provided AND store.get_idempotency_key(key) status='success' → return cached result immediately",
      "2. If idempotency_key provided AND row not found → save_idempotency_key(key, 'pending', status='pending', result=None)",
      "3. Run program (existing logic)",
      "4. If idempotency_key provided AND run succeeded → save_idempotency_key(key, trace_id, status='success', result=result)"
    ]
  },

  "tool_signature_change": {
    "run_program": "add optional idempotency_key: str = '' to tools.run_program and pass to handler"
  },

  "constraints": [
    "mypy --strict 0 errors",
    "ruff line-length=100 select=E,F,I,UP",
    "ProgramStore sync (not async)",
    "No eval()/exec()",
    "Use with self._lock for all writes",
    "json.dumps(result) for result_json; json.loads on read (or None if NULL)"
  ],

  "test_file": "tests/test_sprint4_idempotency.py",

  "tests": [
    "IP-01: save + get round-trip",
    "IP-02: get returns None for missing key",
    "IP-03: delete returns True if existed",
    "IP-04: delete returns False if not existed",
    "IP-05: duplicate key → upsert overwrites",
    "IP-06: status=pending → run proceeds (not cached)",
    "IP-07: status=success → cached result returned without vm.run()",
    "IP-08: idempotency_key absent → normal run (backward compat)",
    "IP-09: GovernedRunProgramHandler saves key after successful run",
    "IP-10: expires_at stored and retrievable"
  ]
}
""").strip()

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def main() -> None:
    repo_path = os.environ.get(
        "NANO_VM_MCP_REPO_PATH",
        str(Path.home() / "nano-vm-mcp"),
    )

    if not Path(repo_path).exists():
        print(f"ERROR: nano-vm-mcp repo not found at {repo_path!r}")
        print("Set NANO_VM_MCP_REPO_PATH env var.")
        sys.exit(1)

    target_files = [
        "nano_vm_mcp/store.py",
        "nano_vm_mcp/handlers.py",
        "nano_vm_mcp/tools.py",
    ]
    test_file = "tests/test_sprint4_idempotency.py"

    print("=" * 60)
    print("DA-2: idempotency_store sprint")
    print(f"repo_path  : {repo_path}")
    print(f"model      : {_VIBECODE_MODEL}")
    print(f"targets    : {target_files}")
    print(f"test_file  : {test_file}")
    print("=" * 60)

    try:
        from nano_vm.models import TraceStatus

        from agent.runner import run_sprint
    except ImportError as exc:
        print(f"ERROR: import failed — {exc}")
        print("Run: pip install llm-nano-vm[litellm]")
        sys.exit(1)

    # Vibecode требует stream=True для длинных запросов (иначе таймаут 100с)
    # timeout=300 — запас для generate_patch (большой контекст)
    trace = await run_sprint(
        sprint_spec=SPRINT_SPEC,
        target_files=target_files,
        test_file=test_file,
        llm_model=_VIBECODE_MODEL,
        repo_path=repo_path,
        adapter_kwargs={"stream": True, "timeout": 300, "max_tokens": 8192},
    )

    # DEBUG: dump full generate_patch output
    for s in trace.steps:
        if s.step_id == "generate_patch":
            out = getattr(s, "output", "") or ""
            Path("patch_debug.txt").write_text(out)
            print(f"patch saved to patch_debug.txt ({len(out)} chars)")

    cost = trace.total_cost_usd() or 0.0

    print()
    print("=" * 60)
    print(f"STATUS : {trace.status.name}")
    print(f"STEPS  : {len(trace.steps)}")
    print(f"COST   : ${cost:.6f}")
    if trace.error:
        print(f"ERROR  : {trace.error}")
    print("=" * 60)

    if trace.status == TraceStatus.SUCCESS:
        print("✓ Patch applied. Run CI to verify.")
    else:
        print("✗ Patch NOT applied.")
        for s in trace.steps:
            out = getattr(s, "output", None)
            err = getattr(s, "error", None)
            print(f"\n  step={s.step_id}  status={s.status}  duration={s.duration_ms}ms")
            if out:
                print(f"  output: {str(out)[:600]}")
            if err:
                print(f"  error:  {str(err)[:600]}")

    summary = {
        "status": trace.status.name,
        "trace_id": str(trace.trace_id),
        "steps": [
            {
                "step_id": s.step_id,
                "status": s.status.name if hasattr(s.status, "name") else str(s.status),
                "duration_ms": s.duration_ms,
                "output": str(getattr(s, "output", None))[:300],
                "error": str(getattr(s, "error", None))[:300],
            }
            for s in trace.steps
        ],
        "error": trace.error,
        "total_cost_usd": cost,
    }
    out_path = Path("da2_trace.json")
    out_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False))
    print(f"\nTrace summary → {out_path}")


if __name__ == "__main__":
    asyncio.run(main())
