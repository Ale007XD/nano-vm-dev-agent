"""tests/test_runner_fingerprint.py — DA-5: runner clears fingerprints on sprint start."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent.tools import FailureFingerprint, record_fingerprint, get_seen_fingerprints


@pytest.mark.asyncio
async def test_run_sprint_clears_fingerprints() -> None:
    """run_sprint() clears fingerprint state at sprint start."""
    fp = FailureFingerprint(tool="run_mypy", error_class="arg-type")
    record_fingerprint(fp)
    assert fp.key() in get_seen_fingerprints()

    with (
        patch("agent.runner.clear_fingerprints") as mock_clear,
        patch("agent.runner.build_adapter") as mock_build,
        patch("agent.runner.ExecutionVM") as mock_vm_cls,
    ):
        mock_build.return_value = (AsyncMock(), "mock")
        mock_vm = AsyncMock()
        mock_vm.run = AsyncMock(return_value=MagicMock())
        mock_vm_cls.return_value = mock_vm

        from agent.runner import run_sprint
        await run_sprint(
            sprint_spec="test",
            target_files=["agent/tools.py"],
            test_file="tests/test_fingerprint.py",
        )

    mock_clear.assert_called_once()
