import asyncio
import json
from datetime import datetime
from pathlib import Path

from agent.runner import run_sprint


SPRINT_NAME = "sprint_m1_inventory_promotions"


async def main() -> None:
    trace = await run_sprint(
        sprint_spec=Path(
            "context/sprint_m1_inventory_promotions.md"
        ).read_text(encoding="utf-8"),
        target_files=[
            "app/domains/inventory/fsm.py",
            "app/domains/promotions/fsm.py",
            "app/domains/schedule/fsm.py",
            "app/domains/privacy/fsm.py",
        ],
        test_file="tests/unit/fsm/test_inventory_fsm.py",
        repo_path="/home/alexd/projects/sieshka",
    )

    print("\n" + "=" * 80)
    print(f"TRACE ID : {trace.trace_id}")
    print(f"STATUS   : {trace.status.name}")
    print(f"STEPS    : {len(trace.steps)}")
    print("=" * 80)

    for idx, step in enumerate(trace.steps, start=1):
        status = (
            step.status.name
            if hasattr(step.status, "name")
            else str(step.status)
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
                    s.status.name
                    if hasattr(s.status, "name")
                    else str(s.status)
                ),
                "duration_ms": s.duration_ms,
                "output": str(getattr(s, "output", "")),
                "error": str(getattr(s, "error", "")),
            }
            for s in trace.steps
        ],
    }

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_file = f"{SPRINT_NAME}_{ts}.json"

    Path(out_file).write_text(
        json.dumps(trace_json, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print("\n" + "=" * 80)
    print(f"Trace saved: {out_file}")
    print("=" * 80)


if __name__ == "__main__":
    asyncio.run(main())
