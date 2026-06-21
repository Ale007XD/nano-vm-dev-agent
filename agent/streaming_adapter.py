"""
agent/streaming_adapter.py
===========================
StreamingLiteLLMAdapter — LLMAdapter implementation that correctly consumes
litellm's streamed response when stream=True.

Why this exists:
    nano_vm.adapters.litellm_adapter.LiteLLMAdapter.complete() does:
        response = await acompletion(**params)
        text = response.choices[0].message.content
    This is correct ONLY for stream=False. When stream=True, acompletion()
    returns a litellm.CustomStreamWrapper (an async generator of chunks),
    which has no .choices attribute — hence:
        AttributeError: 'CustomStreamWrapper' object has no attribute 'choices'

Why stream=True is needed at all (not just removable):
    The Vibecode proxy enforces a ~100s server-side timeout on non-streaming
    requests (run_da2.py:161 comment, validated DA-2/DA-3). claude-sonnet-4.6
    reasons before answering — patch-generation calls regularly exceed 100s.
    stream=True avoids that proxy-side cutoff. Dropping stream=True would
    trade a loud, fixed AttributeError for an intermittent, harder-to-diagnose
    timeout on exactly the long completions this pipeline depends on.

Fix: don't rely on LiteLLMAdapter for the streaming path. Call
litellm.acompletion() directly and async-iterate the CustomStreamWrapper,
accumulating chunk.choices[0].delta.content — the standard litellm streaming
pattern. Implements the same LLMAdapter Protocol (nano_vm.adapters.base),
so it's a drop-in replacement wherever stream=True is needed.
"""

from __future__ import annotations

from typing import Any

try:
    from litellm import acompletion
except ImportError as exc:
    raise ImportError("litellm не установлен. Выполни: pip install nano-vm[litellm]") from exc


class StreamingLiteLLMAdapter:
    """LLMAdapter for providers that require stream=True (e.g. Vibecode proxy
    avoiding its ~100s non-streaming timeout). Accumulates the streamed
    response itself rather than delegating to LiteLLMAdapter, which only
    handles the non-streaming response shape.

    Args:
        model:       litellm model string, e.g. "openai/claude-sonnet-4.6".
        timeout:     request timeout in seconds.
        max_retries: litellm num_retries on provider error.
        temperature: generation temperature (0.0 = deterministic).
        **kwargs:    any extra litellm.acompletion params (api_base, api_key, ...).
                     'stream' is always forced to True internally — pass
                     other provider config (api_base/api_key/max_tokens) here.
    """

    def __init__(
        self,
        model: str,
        timeout: float = 300.0,
        max_retries: int = 2,
        temperature: float = 0.0,
        **kwargs: Any,
    ) -> None:
        self.model = model
        self.timeout = timeout
        self.max_retries = max_retries
        self.temperature = temperature
        # 'stream' must never be settable from the outside — this adapter's
        # entire purpose is the stream=True consumption path. Anyone wanting
        # stream=False should use the plain LiteLLMAdapter instead.
        self._extra = {k: v for k, v in kwargs.items() if k != "stream"}

    async def complete(
        self,
        messages: list[dict[str, str]],
        **kwargs: Any,
    ) -> tuple[str, dict[str, Any] | None]:
        """Stream the completion and accumulate it into a single string.

        Returns:
            (text, usage_dict | None) — usage_dict populated only if the
            provider sends a final usage-bearing chunk (stream_options=
            {"include_usage": True}); most OpenAI-compatible proxies do,
            but absence is tolerated, not treated as an error.
        """
        call_kwargs = {k: v for k, v in kwargs.items() if k != "stream"}
        params: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "timeout": self.timeout,
            "num_retries": self.max_retries,
            "stream": True,
            "stream_options": {"include_usage": True},
            **self._extra,
            **call_kwargs,  # per-call kwargs win
        }
        params["stream"] = True  # non-negotiable for this adapter

        chunks: list[str] = []
        usage_dict: dict[str, Any] | None = None

        response_stream = await acompletion(**params)
        async for chunk in response_stream:
            choice = chunk.choices[0] if chunk.choices else None
            delta_content = getattr(choice.delta, "content", None) if choice else None
            if delta_content:
                chunks.append(delta_content)

            raw_usage = getattr(chunk, "usage", None)
            if raw_usage is not None:
                prompt_tokens = getattr(raw_usage, "prompt_tokens", 0) or 0
                completion_tokens = getattr(raw_usage, "completion_tokens", 0) or 0
                total_tokens = getattr(raw_usage, "total_tokens", 0) or (
                    prompt_tokens + completion_tokens
                )
                usage_dict = {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": total_tokens,
                    "cost_usd": None,
                }

        return "".join(chunks), usage_dict

    def __repr__(self) -> str:
        return f"StreamingLiteLLMAdapter(model={self.model!r})"
