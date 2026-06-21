"""
tests/test_streaming_adapter.py
================================
DA-22  StreamingLiteLLMAdapter.complete(): accumulates chunk.delta.content
       across an async-iterated stream into one string.
DA-23  StreamingLiteLLMAdapter.complete(): final usage-bearing chunk →
       usage_dict populated (prompt/completion/total tokens).
DA-24  StreamingLiteLLMAdapter.complete(): no usage chunk → usage_dict is None
       (tolerated, not an error).
DA-25  StreamingLiteLLMAdapter: 'stream' kwarg can never be forced to False —
       it is the adapter's entire reason for existing.
DA-26  build_adapter(): provider cfg with stream=True → StreamingLiteLLMAdapter
       selected, NOT the plain LiteLLMAdapter (which crashes on streamed
       responses — this is the exact regression this adapter exists to fix).
DA-27  build_adapter(): provider cfg without stream → plain LiteLLMAdapter.
"""

from __future__ import annotations

import os
import sys
import types
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _make_chunk(content: str | None, usage: Any = None) -> types.SimpleNamespace:
    delta = types.SimpleNamespace(content=content)
    choice = types.SimpleNamespace(delta=delta)
    return types.SimpleNamespace(choices=[choice], usage=usage)


class _FakeStream:
    """Minimal async-iterable mimicking litellm.CustomStreamWrapper."""

    def __init__(self, chunks: list[Any]) -> None:
        self._chunks = chunks

    def __aiter__(self) -> _FakeStream:
        self._iter = iter(self._chunks)
        return self

    async def __anext__(self) -> Any:
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration


@pytest.mark.asyncio
async def test_da_22_accumulates_stream_chunks() -> None:
    from agent.streaming_adapter import StreamingLiteLLMAdapter

    chunks = [_make_chunk("Hello"), _make_chunk(", "), _make_chunk("world!")]
    fake_stream = _FakeStream(chunks)

    with patch(
        "agent.streaming_adapter.acompletion", new=AsyncMock(return_value=fake_stream)
    ):
        adapter = StreamingLiteLLMAdapter(model="openai/claude-sonnet-4.6")
        text, usage = await adapter.complete([{"role": "user", "content": "hi"}])

    assert text == "Hello, world!"


@pytest.mark.asyncio
async def test_da_23_usage_chunk_populates_usage_dict() -> None:
    from agent.streaming_adapter import StreamingLiteLLMAdapter

    raw_usage = types.SimpleNamespace(
        prompt_tokens=10, completion_tokens=5, total_tokens=15
    )
    chunks = [
        _make_chunk("Hi"),
        _make_chunk(None, usage=raw_usage),  # final chunk: no content, has usage
    ]
    fake_stream = _FakeStream(chunks)

    with patch(
        "agent.streaming_adapter.acompletion", new=AsyncMock(return_value=fake_stream)
    ):
        adapter = StreamingLiteLLMAdapter(model="openai/claude-sonnet-4.6")
        text, usage = await adapter.complete([{"role": "user", "content": "hi"}])

    assert text == "Hi"
    assert usage == {
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "total_tokens": 15,
        "cost_usd": None,
    }


@pytest.mark.asyncio
async def test_da_24_no_usage_chunk_returns_none() -> None:
    from agent.streaming_adapter import StreamingLiteLLMAdapter

    chunks = [_make_chunk("ok")]
    fake_stream = _FakeStream(chunks)

    with patch(
        "agent.streaming_adapter.acompletion", new=AsyncMock(return_value=fake_stream)
    ):
        adapter = StreamingLiteLLMAdapter(model="openai/claude-sonnet-4.6")
        text, usage = await adapter.complete([{"role": "user", "content": "hi"}])

    assert text == "ok"
    assert usage is None


@pytest.mark.asyncio
async def test_da_25_stream_kwarg_cannot_be_forced_false() -> None:
    """Passing stream=False at construction or call time must not disable
    streaming — that would reintroduce the exact AttributeError this
    adapter exists to avoid (CustomStreamWrapper has no .choices)."""
    from agent.streaming_adapter import StreamingLiteLLMAdapter

    captured: dict[str, Any] = {}

    async def _fake_acompletion(**params: Any) -> _FakeStream:
        captured.update(params)
        return _FakeStream([_make_chunk("x")])

    with patch("agent.streaming_adapter.acompletion", new=_fake_acompletion):
        adapter = StreamingLiteLLMAdapter(model="m", stream=False)  # constructor try
        await adapter.complete([{"role": "user", "content": "hi"}], stream=False)  # call-time try

    assert captured["stream"] is True


def test_da_26_build_adapter_routes_stream_true_to_streaming_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent.runner import build_adapter
    from agent.streaming_adapter import StreamingLiteLLMAdapter

    monkeypatch.setenv("NANO_VM_AGENT_PROVIDER", "vibecode")
    monkeypatch.setenv("VIBECODE_API_KEY", "fake-key")

    adapter, provider_name = build_adapter()

    assert provider_name == "vibecode"
    assert isinstance(adapter, StreamingLiteLLMAdapter)


def test_da_27_build_adapter_routes_non_streaming_to_plain_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nano_vm.adapters.litellm_adapter import LiteLLMAdapter

    from agent.runner import build_adapter

    monkeypatch.setenv("NANO_VM_AGENT_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key")

    adapter, provider_name = build_adapter()

    assert provider_name == "anthropic"
    assert isinstance(adapter, LiteLLMAdapter)
