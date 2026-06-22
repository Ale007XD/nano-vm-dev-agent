"""
run_sieshka.py
===============
Two-phase M1 sprint runner for the Sieshka target repo.

Phase 1 (sprint_m1_domain_models): creates app/domains/{inventory,promotions,
privacy}/models.py — the State/Event/TRANSITIONS modules that the fsm.py
sprint's files import from (same pattern as OrderFSM -> orders/models.py).
schedule/fsm.py is excluded — it defines its enums inline, no models.py needed.

Phase 2 (sprint_m1_inventory_promotions): creates/patches the 4 domain
fsm.py files, now with the real models.py content available as reference
context (in addition to BaseFSM + OrderFSM).

Why two phases, not one sprint with 7 target files: a sprint's per-file
patch/create steps only see EXISTING on-disk content (read_repo_files) or
reference_files at sprint start — not files staged earlier in the SAME
sprint (that's what read_staged_files is for, but it only feeds
generate_test, not other patch_i prompts). Running phase 1 to completion
and commit first means phase 2's reference_files read can simply read
models.py off disk like any other existing file — no new agent mechanism
needed.

Root cause this two-phase split fixes (DECISIONS.md 2026-06-22):
pyproject.toml's `ignore_missing_imports = true` made a missing models.py
silently resolve to Any rather than fail import — which collapsed fsm.py's
return-type annotations to Any, which made `# type: ignore[no-any-return]`
"unused" (confusing downstream symptom of models.py simply not existing).

Phase 2 only runs if phase 1's trace.status == SUCCESS — no point patching
fsm.py against a models.py that was never actually committed.
"""

import asyncio
import json
import shutil
from datetime import datetime
from pathlib import Path

from nano_vm.models import Trace, TraceStatus

from agent.runner import run_sprint

REPO_PATH = "/home/alexd/projects/sieshka"

# Stale fsm.py files from earlier (pre-models.py) sprint attempts get
# incrementally S&R-patched instead of cleanly created. Backed up (not
# deleted) so nothing is silently lost, then removed so phase 2 takes the
# clean create-file path. Safe to re-run: each run just overwrites the
# previous .bak with the latest pre-phase-2 state.
STALE_FSM_FILES = [
    "app/domains/inventory/fsm.py",
    "app/domains/promotions/fsm.py",
    "app/domains/schedule/fsm.py",
    "app/domains/privacy/fsm.py",
]


DOMAIN_MODELS_SPEC = Path("context/sprint_m1_domain_models.md")
FSM_FILES_SPEC = Path("context/sprint_m1_inventory_promotions.md")


def _require_spec_files() -> None:
    """Fail fast, before phase 1 even starts, if either spec file is
    missing. Without this, a missing FSM_FILES_SPEC was only discovered
    AFTER phase 1 committed models.py and cleanup had already backed up +
    removed the 4 stale fsm.py files — the worst possible point to crash
    (DECISIONS.md 2026-06-22: real Vibecode run, phase 1 fully SUCCESS,
    then bare FileNotFoundError on phase 2's spec read, mid-pipeline)."""
    missing = [p for p in (DOMAIN_MODELS_SPEC, FSM_FILES_SPEC) if not p.is_file()]
    if missing:
        names = ", ".join(str(p) for p in missing)
        raise FileNotFoundError(
            f"Missing sprint spec file(s): {names}\n"
            f"Both must exist (relative to cwd) BEFORE running any phase — "
            f"phase 1 commits real changes and phase 2's cleanup backs up/"
            f"removes fsm.py files; discovering a missing spec mid-pipeline "
            f"means redoing cleanup analysis for no reason. Restore the "
            f"file(s) and re-run."
        )


def _print_and_save_trace(sprint_name: str, trace: Trace) -> None:
    print("\n" + "=" * 80)
    print(f"SPRINT   : {sprint_name}")
    print(f"TRACE ID : {trace.trace_id}")
    print(f"STATUS   : {trace.status.name}")
    print(f"STEPS    : {len(trace.steps)}")
    print("=" * 80)

    for idx, step in enumerate(trace.steps, start=1):
        status = (
            step.status.name if hasattr(step.status, "name") else str(step.status)
        )
        print(f"\n[{idx}] {step.step_id}")
        print(f"    status      : {status}")
        print(f"    duration_ms : {step.duration_ms}")

        output = getattr(step, "output", None)
        if output:
            print(f"    output      : {str(output)[:1000]}")

        error = getattr(step, "error", None)
        if error:
            print(f"    error       : {str(error)[:1000]}")

    trace_json = {
        "trace_id": str(trace.trace_id),
        "status": trace.status.name,
        "error": trace.error,
        "steps": [
            {
                "step_id": s.step_id,
                "status": (
                    s.status.name if hasattr(s.status, "name") else str(s.status)
                ),
                "duration_ms": s.duration_ms,
                "output": str(getattr(s, "output", "")),
                "error": str(getattr(s, "error", "")),
            }
            for s in trace.steps
        ],
    }

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_file = f"{sprint_name}_{ts}.json"
    Path(out_file).write_text(
        json.dumps(trace_json, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    print("\n" + "=" * 80)
    print(f"Trace saved: {out_file}")
    print("=" * 80)


def _backup_and_remove_stale_fsm_files() -> list[str]:
    """Back up (rename to .pre_models_sprint.bak) and remove each stale
    fsm.py target file that exists on disk, so phase 2 takes the clean
    create-file path instead of incrementally S&R-patching an old broken
    generation. Returns the list of files actually backed up."""
    backed_up = []
    for rel_path in STALE_FSM_FILES:
        full_path = Path(REPO_PATH) / rel_path
        if full_path.is_file():
            backup_path = full_path.with_suffix(full_path.suffix + ".pre_models_sprint.bak")
            shutil.copy2(full_path, backup_path)
            full_path.unlink()
            backed_up.append(rel_path)
            print(f"[cleanup] backed up + removed stale: {rel_path} -> {backup_path.name}")
    return backed_up


async def run_phase1_domain_models() -> Trace:
    """Create app/domains/{inventory,promotions,privacy}/models.py."""
    return await run_sprint(
        sprint_spec=DOMAIN_MODELS_SPEC.read_text(encoding="utf-8"),
        target_files=[
            "app/domains/inventory/models.py",
            "app/domains/promotions/models.py",
            "app/domains/privacy/models.py",
        ],
        test_file="tests/unit/fsm/test_domain_models.py",
        repo_path=REPO_PATH,
        reference_files=["app/domains/orders/models.py"],
    )


async def run_phase2_fsm_files() -> Trace:
    """Create/patch the 4 domain fsm.py files, now with real models.py
    content available as reference (in addition to BaseFSM + OrderFSM)."""
    return await run_sprint(
        sprint_spec=FSM_FILES_SPEC.read_text(encoding="utf-8"),
        target_files=STALE_FSM_FILES,
        test_file="tests/unit/fsm/test_inventory_fsm.py",
        repo_path=REPO_PATH,
        # BaseFSM + OrderFSM (structural pattern) + the 3 models.py files
        # phase 1 just committed (State/Event/TRANSITIONS each fsm.py
        # imports from). schedule/fsm.py has no models.py — it inlines its
        # enums, intentionally (cyclic FSM, see sprint_m1_domain_models.md).
        reference_files=[
            "app/fsm/core/base.py",
            "app/domains/orders/fsm.py",
            "app/domains/orders/models.py",
            "app/domains/inventory/models.py",
            "app/domains/promotions/models.py",
            "app/domains/privacy/models.py",
        ],
    )


async def main() -> None:
    _require_spec_files()

    print("[runner] === PHASE 1: sprint_m1_domain_models ===")
    phase1_trace = await run_phase1_domain_models()
    _print_and_save_trace("sprint_m1_domain_models", phase1_trace)

    if phase1_trace.status != TraceStatus.SUCCESS:
        print(
            "\n[runner] PHASE 1 did not reach SUCCESS "
            f"(status={phase1_trace.status.name}) — "
            "skipping phase 2. Fix models.py generation first; fsm.py "
            "sprint depends on these files actually existing on disk."
        )
        return

    print("\n[runner] === cleanup: stale fsm.py files from earlier attempts ===")
    _backup_and_remove_stale_fsm_files()

    print("\n[runner] === PHASE 2: sprint_m1_inventory_promotions ===")
    phase2_trace = await run_phase2_fsm_files()
    _print_and_save_trace("sprint_m1_inventory_promotions", phase2_trace)


if __name__ == "__main__":
    asyncio.run(main())
