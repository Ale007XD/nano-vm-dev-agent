# nano-vm-dev-agent

> FSM-driven autonomous development agent. Patches Python repositories using
> Search&Replace deltas, validates with mypy + pytest, and commits only when
> the full validation gate passes.

Built on [llm-nano-vm](https://github.com/Ale007XD/nano_vm) — the deterministic
FSM execution kernel.

---

## What It Does

nano-vm-dev-agent is a self-modifying development loop: given a sprint specification
and a target repository, it generates S&R patches via LLM, stages them in memory,
validates with mypy and pytest, and commits to disk only on success.

The FSM runtime controls every transition. The LLM generates content — it does not
control execution order, validation gates, or rollback decisions.

```
sprint_spec + repo files
        ↓
  LLM → S&R patch (per file)
        ↓
  stage_patch()          ← in-memory buffer, disk unchanged
        ↓
  commit_patches()       ← atomic disk write (all files at once)
        ↓
  mypy --strict
        ↓
  [FAIL] rollback_patches() → repo stays clean
  [OK]   pytest
        ↓
  [FAIL] git checkout    ← safety net (caller's responsibility)
  [OK]   DONE
```

---

## Architecture

```
nano-vm-dev-agent
├── agent/
│   ├── tools.py      — sync tool functions registered with ExecutionVM
│   ├── programs.py   — PROGRAM_SPRINT FSM definition (nano-vm DSL)
│   └── runner.py     — async run_sprint() entry point
└── tests/
    └── test_agent.py — DA-01..20 unit + integration tests
```

### Layer separation

| Layer | What it does |
| :--- | :--- |
| `programs.py` (DSL) | Declares the workflow topology — step order, branches, retry limits |
| `tools.py` (tools) | Implements side effects — file I/O, subprocess, LLM parsing |
| `nano-vm` (runtime) | Enforces transitions, budgets, error policies — no arbitrary code |

---

## Transactional Patching

Patches are applied in three phases:

| Function | Behaviour |
| :--- | :--- |
| `stage_patch(file, patch)` | Apply S&R blocks in-memory (`_patch_buffer`). Disk untouched. |
| `commit_patches()` | Flush entire buffer to disk atomically. Buffer cleared. |
| `rollback_patches()` | Discard buffer without writing. Disk stays at pre-patch state. |

On mypy failure, `rollback_patches()` runs before the terminal rejection step —
the repository is guaranteed to be in its original state.

On pytest failure (after commit), the caller (`run_da*.py`) runs
`git checkout <files>` as a safety net.

### S&R block format

```
<<<SEARCH
<exact lines to find — must match exactly once>
=======
<replacement lines>
>>>REPLACE
```

`stage_patch` raises `ValueError` if a SEARCH block matches 0 or more than 1 time.
The FSM `on_error=retry` re-asks the LLM for a corrected patch.

---

## PROGRAM_SPRINT Flow

```
read_store → patch_store(llm) → stage_store
→ read_handlers → patch_handlers(llm) → stage_handlers
→ read_tools → patch_tools(llm) → stage_tools
→ commit_patches                    ← atomic disk write
→ generate_test(llm) → write_test
→ run_mypy → mypy_guard
    → [FAIL] do_rollback → reject_mypy   (is_terminal)
→ run_tests → pytest_guard
    → [FAIL] reject_pytest               (is_terminal)
→ notify_done                            (is_terminal)
```

Each LLM step generates a Search&Replace delta — not a full file. This keeps every
response well under provider token limits regardless of file size.

---

## Tools

| Tool | Type | Description |
| :--- | :--- | :--- |
| `read_repo_files` | sync | Read file(s) from disk (or buffer if staged) |
| `stage_patch` | sync | Apply S&R in-memory |
| `commit_patches` | sync | Flush buffer to disk |
| `rollback_patches` | sync | Discard buffer |
| `run_mypy` | sync | `mypy --strict` on target paths |
| `run_pytest` | sync | `pytest -v --tb=short` on test file |
| `write_repo_files` | sync | Write generated test file to disk |
| `notify_done` | sync | Terminal: DONE |
| `notify_rejected_mypy` | sync | Terminal: REJECTED_MYPY |
| `notify_rejected_pytest` | sync | Terminal: REJECTED_PYTEST |

All tools accept `**kwargs` — ExecutionVM passes the full FSM context on every call.

---

## LLM Provider

Configured for [Vibecode proxy](https://api.vibecode-claude.online/v1) with
`openai/claude-sonnet-4.6`. Any LiteLLM-compatible provider works.

Environment setup (must precede all litellm/nano_vm imports):

```python
import os
os.environ["OPENAI_API_KEY"]  = "<vibecode_key>"
os.environ["OPENAI_API_BASE"] = "https://api.vibecode-claude.online/v1"
```

Runner kwargs: `stream=True`, `timeout=300`, `max_tokens=8192`.

---

## Running Tests

```bash
pip install llm-nano-vm pytest pytest-asyncio
pytest tests/test_agent.py -v
```

Expected: **20/20 PASS** (DA-01..DA-20).

No real API key required — integration tests use `MockLLMAdapter`.

---

## Sprint Context Variables

Pass these in the `context` dict when calling `vm.run(program, context=...)`:

| Key | Example | Description |
| :--- | :--- | :--- |
| `sprint_spec` | `"add unique index on (execution_id, step_index)"` | Natural language spec for LLM |
| `store_file` | `"/path/to/nano_vm_mcp/store.py"` | Absolute path — staged first |
| `handlers_file` | `"/path/to/nano_vm_mcp/handlers.py"` | Absolute path — staged second |
| `tools_file` | `"/path/to/nano_vm_mcp/tools.py"` | Absolute path — staged third |
| `store_path` | `'["/path/to/store.py"]'` | JSON list for `read_repo_files` |
| `handlers_path` | `'["/path/to/handlers.py"]'` | JSON list |
| `tools_path` | `'["/path/to/tools.py"]'` | JSON list |
| `target_files` | `'["/path/store.py", "/path/handlers.py"]'` | JSON list for mypy |
| `test_file` | `"/path/to/tests/test_sprint.py"` | Path for test generation + pytest |
| `repo_path` | `"/path/to/repo"` | Working directory for pytest |

---

## patch_outcome vs trace.status

`trace.status` reflects **execution outcome** — whether the FSM graph completed
without runtime errors. It will be `SUCCESS` even when the patch was rejected.

`patch_outcome` is the **business outcome** — derived by the runner from the
terminal step output:

| Terminal step output | `patch_outcome` |
| :--- | :--- |
| `DONE` | `ACCEPTED` |
| `REJECTED_MYPY` | `REJECTED` |
| `REJECTED_PYTEST` | `REJECTED` |

The runner (`run_da*.py`) reads the last step output and reports both values
separately.

---

## Status

| Sprint | Focus | Status |
| :--- | :--- | :---: |
| DA-1 | Agent scaffold: tools, programs, runner | ✅ DONE |
| DA-2 | idempotency_store in nano-vm-mcp (manual patch) | ✅ DONE |
| DA-3 | S&R patching + battle run: TRACE projection logging | ✅ DONE |
| DA-4 | Transactional patching (stage/commit/rollback) | ✅ DONE |
| DA-5 | Next battle run (TL-07 or circuit_breaker) | ⬜ PLANNED |

**Test suite:** DA-01..20 · 20/20 PASS  
**Battle runs completed:** DA-3 (TRACE projection logging, 16 FSM steps, 6/6 tests)

---

## Relationship to nano-vm Ecosystem

```
nano-vm-dev-agent        — FSM agent (this repo)
    ↓ uses
llm-nano-vm              — deterministic FSM execution kernel (PyPI: llm-nano-vm)
    ↓ governed by
nano-vm-mcp              — MCP gateway with GovernanceEnvelope + audit trail
```

nano-vm-dev-agent dogfoods the stack it maintains: every sprint that patches
`nano-vm-mcp` runs through the same FSM runtime that `nano-vm-mcp` exposes.

---

## License

MIT
