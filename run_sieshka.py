# run_sieshka.py

import asyncio

from agent.runner import run_sprint


async def main():
    await run_sprint(
        sprint_spec=open("context/sprint_m1_inventory_promotions.md").read(),
        target_files=[...],
        test_file="tests/...",
        repo_path="~/projects/sieshka",
    )

asyncio.run(main())
