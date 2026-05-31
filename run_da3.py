"""
run_da3.py
==========
DA-3: боевой прогон nano-vm-dev-agent — TRACE projection logging в SQLite.

Провайдер: Vibecode (https://api.vibecode-claude.online/v1)

Переменные окружения:
  VIBECODE_API_KEY       — ключ Vibecode (sk-...)
  NANO_VM_MCP_REPO_PATH  — путь к репо nano-vm-mcp (default: ~/nano-vm-mcp)

Запуск:
  export VIBECODE_API_KEY=sk-...
  python run_da3.py
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
## Sprint: TRACE projection logging в SQLite (nano-vm-mcp)

### Цель
Каждый успешный `run_program` должен записывать TRACE-проекцию шага в SQLite.
Сейчас audit trail живёт только in-memory. После спринта — персистентный лог.

### store.py — добавить

1. В `_init_schema`, после блока `idempotency_keys`, добавить новую таблицу:

```sql
CREATE TABLE IF NOT EXISTS execution_traces (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    execution_id    TEXT    NOT NULL,
    step_index      INTEGER NOT NULL,
    step_id         TEXT    NOT NULL,
    projected_json  TEXT    NOT NULL,
    canonical_hash  TEXT    NOT NULL,
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_exec_traces_execution_id
    ON execution_traces (execution_id);
```

2. Добавить два метода в класс `ProgramStore` после метода `delete_envelopes`:

```python
def save_trace_step(
    self,
    execution_id: str,
    step_index: int,
    step_id: str,
    projected: dict[str, Any],
    canonical_hash: str,
) -> int:
    with self._lock:
        cur = self._con.execute(
            \"\"\"INSERT INTO execution_traces
                   (execution_id, step_index, step_id, projected_json, canonical_hash)
               VALUES (?, ?, ?, ?, ?)\"\"\",
            (execution_id, step_index, step_id, json.dumps(projected), canonical_hash),
        )
        self._con.commit()
        return cur.lastrowid  # type: ignore[return-value]

def get_trace_steps(self, execution_id: str) -> list[dict[str, Any]]:
    rows = self._con.execute(
        \"\"\"SELECT execution_id, step_index, step_id, projected_json,
                  canonical_hash, created_at
           FROM execution_traces
           WHERE execution_id = ?
           ORDER BY step_index\"\"\",
        (execution_id,),
    ).fetchall()
    return [
        {
            "execution_id": r["execution_id"],
            "step_index": r["step_index"],
            "step_id": r["step_id"],
            "projected": json.loads(r["projected_json"]),
            "canonical_hash": r["canonical_hash"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]
```

### handlers.py — изменить

В методе `GovernedRunProgramHandler._try_handle`, внутри блока
`if trace_id and not result.get("error"):` (после `store.save_envelope(...)`),
добавить вызов `store.save_trace_step(...)` перед `return _ok(result)`.

Переменная `envelope` уже объявлена выше — использовать `envelope.canonical_snapshot_hash`.

```python
        # 7. TRACE projection logging — persist per-execution audit record (v0.4.1)
        if trace_id and not result.get("error"):
            store.save_trace_step(
                execution_id=trace_id,
                step_index=0,
                step_id="run_program",
                projected={
                    "trace_id": trace_id,
                    "status": result.get("status"),
                    "steps": result.get("steps", 0),
                    "cost": result.get("cost", 0.0),
                    "projection_target": "TRACE",
                },
                canonical_hash=envelope.canonical_snapshot_hash,
            )
```

### tools.py — без изменений

Для tools.py верни единственный no-op патч:

<<<SEARCH
\"\"\"nano_vm_mcp.tools — MCP tool implementations.\"\"\"
=======
\"\"\"nano_vm_mcp.tools — MCP tool implementations.\"\"\"
>>>REPLACE

### Тест: tests/test_sprint_trace_logging.py

Тест-кейсы TL-01..TL-06:

TL-01: save_trace_step сохраняет запись, get_trace_steps возвращает её
TL-02: get_trace_steps возвращает пустой список для неизвестного execution_id
TL-03: несколько шагов — возвращаются отсортированными по step_index
TL-04: save_trace_step возвращает rowid > 0
TL-05: GovernedRunProgramHandler записывает trace_step после успешного run_program
       (использовать MockLLMAdapter, tool-only program, проверить get_trace_steps непустой)
TL-06: GovernedRunProgramHandler НЕ записывает trace_step если result содержит error
       (передать невалидную программу — проверить что get_trace_steps пуст)

Использовать tmp_path для SQLite. Все тесты async.
Добавить в начало файла: pytestmark = pytest.mark.asyncio
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
    ]
    test_file = "tests/test_sprint_trace_logging.py"

    print("=" * 60)
    print("DA-3: TRACE projection logging sprint")
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

    trace = await run_sprint(
        sprint_spec=SPRINT_SPEC,
        target_files=target_files,
        test_file=test_file,
        llm_model=_VIBECODE_MODEL,
        repo_path=repo_path,
        adapter_kwargs={"stream": True, "timeout": 300, "max_tokens": 8192},
    )

    try:
        cost_raw = trace.total_cost_usd
        cost: float = float(cost_raw() if callable(cost_raw) else (cost_raw or 0.0))
    except Exception:
        cost = 0.0

    print()
    print("=" * 60)
    print(f"STATUS : {trace.status.name}")
    print(f"STEPS  : {len(trace.steps)}")
    print(f"COST   : ${cost:.6f}")
    if trace.error:
        print(f"ERROR  : {trace.error}")
    print("=" * 60)

    for s in trace.steps:
        out = getattr(s, "output", None)
        err = getattr(s, "error", None)
        status_name = s.status.name if hasattr(s.status, "name") else str(s.status)
        print(f"\n  [{s.step_id:25s}] {status_name:10s}  {s.duration_ms:.0f}ms")
        if out and status_name != "SUCCESS":
            print(f"  output: {str(out)[:800]}")
        if err:
            print(f"  error:  {str(err)[:800]}")

    if trace.status == TraceStatus.SUCCESS:
        print("\n✓ Patch applied. Run CI to verify.")
    else:
        print("\n✗ Patch NOT applied.")

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
    out_path = Path("da3_trace.json")
    out_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False))
    print(f"Trace summary → {out_path}")


if __name__ == "__main__":
    asyncio.run(main())
