import asyncio

from agent.runner import run_sprint


async def main():
    await run_sprint(
        sprint_spec=open(
            "context/sprint_m1_inventory_promotions.md",
            encoding="utf-8",
        ).read(),
        target_files=[
            "app/domains/inventory/fsm.py",
            "app/domains/promotions/fsm.py",
            "app/domains/schedule/fsm.py",
            "app/domains/privacy/fsm.py",
        ],
        test_file="tests/unit/fsm/test_inventory_fsm.py",
        repo_path="/home/alexd/projects/sieshka",
    )


if __name__ == "__main__":
    asyncio.run(main())
