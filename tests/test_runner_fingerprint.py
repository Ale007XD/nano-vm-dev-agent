"""tests/test_runner_fingerprint.py — DA-5: runner clears fingerprints on sprint start."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

import agent.tools as tools_mod
from agent.tools import FailureFingerprint


@pytest.mark.asyncio
async def test_run_sprint_clears_fingerprints() -> None:
    """run_sprint() must reset fingerprint state before execution."""
    fp = FailureFingerprint(tool="run_mypy", error_class="arg-type")
    tools_mod.record_fingerprint(fp)
    assert fp.key() in tools_mod.get_seen_fingerprints()

    with (
        patch("agent.runner.build_adapter") as mock_build,
        patch("agent.runner.ExecutionVM") as mock_vm_cls,
    ):
        mock_build.return_value = (AsyncMock(), "mock")
        mock_vm = AsyncMock()
        mock_vm.run = AsyncMock(return_value=object())
        mock_vm_cls.return_value = mock_vm

        from agent.runner import run_sprint
        await run_sprint(
            sprint_spec="test",
            target_files=["agent/tools.py"],
            test_file="tests/test_fingerprint.py",
        )

    assert tools_mod.get_seen_fingerprints() == frozenset()
