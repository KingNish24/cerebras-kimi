"""One-shot text generation for features that are not chat turns.

Co-Writer's AI edits need plain "give me this text back" completions, not an
agent loop with tools. This module resolves the same provider/model the chat
turn would use (``provider_config.resolve_runtime_model``) and calls the
provider wire protocol directly, streaming deltas when the caller wants them.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import AsyncIterator
from typing import Any

import httpx

from .. import provider_config
from ..config import Settings

logger = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(120.0, connect=20.0)

# Providers whose wire format is not OpenAI chat-completions.
_ANTHROPIC = "anthropic"
_RESPONSES = "openai-responses"

_THINKING_TAGS = re.compile(
    r"<(thinking|reasoning|analysis)>.*?</\1>", re.DOTALL | re.IGNORECASE
)


def clean_thinking_tags(text: str) -> str:
    """Drop inline reasoning blocks some models still emit in their text."""
    return _THINKING_TAGS.sub("", text or "").strip()


def resolve(settings: Settings, llm_selection: dict[str, Any] | None = None) -> dict[str, Any]:
    resolved = provider_config.resolve_runtime_model(
        settings, {"llm_selection": llm_selection} if llm_selection else {}
    )
    if not resolved.get("model"):
        raise RuntimeError("No model configured — pick one in Settings → LLM")
    return resolved


def _endpoint(base_url: str | None, suffix: str) -> str:
    base = (base_url or "").strip().rstrip("/")
    if not base:
        raise RuntimeError("This model has no base URL configured")
    if not base.lower().startswith(("http://", "https://")):
        raise RuntimeError(f"Unsupported base URL: {base}")
    return f"{base}/{suffix.lstrip('/')}"


def _anthropic_body(model: str, system: str, prompt: str, stream: bool) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": model,
        "max_tokens": 8192,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        body["system"] = system
    if stream:
        body["stream"] = True
    return body


def _openai_body(model: str, system: str, prompt: str, stream: bool) -> dict[str, Any]:
    messages: list[dict[str, Any]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    body: dict[str, Any] = {"model": model, "messages": messages}
    if stream:
        body["stream"] = True
    return body


async def stream_text(
    settings: Settings,
    *,
    system: str,
    prompt: str,
    llm_selection: dict[str, Any] | None = None,
) -> AsyncIterator[str]:
    """Yield text deltas from the configured model."""
    resolved = resolve(settings, llm_selection)
    provider = resolved["provider"]
    model = resolved["model"]
    key = resolved.get("api_key")

    if provider == _ANTHROPIC:
        url = _endpoint(resolved.get("base_url"), "messages")
        headers = {
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        if key:
            headers["x-api-key"] = key
        body = _anthropic_body(model, system, prompt, stream=True)
    elif provider == _RESPONSES:
        url = _endpoint(resolved.get("base_url"), "responses")
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        body = _openai_body(model, system, prompt, stream=True) | {"stream": True}
        body["input"] = [
            *([{"role": "system", "content": system}] if system else []),
            {"role": "user", "content": prompt},
        ]
        body.pop("messages", None)
    else:
        url = _endpoint(resolved.get("base_url"), "chat/completions")
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        body = _openai_body(model, system, prompt, stream=True)

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        async with client.stream("POST", url, headers=headers, json=body) as response:
            if response.status_code >= 400:
                detail = (await response.aread()).decode("utf-8", "replace")[:800]
                raise RuntimeError(
                    f"{model} rejected the request ({response.status_code}): {detail}"
                )
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if not payload or payload == "[DONE]":
                    continue
                try:
                    event = json.loads(payload)
                except ValueError:
                    continue
                delta = _delta_text(event, provider)
                if delta:
                    yield delta


def _delta_text(event: dict[str, Any], provider: str) -> str:
    if provider == _ANTHROPIC:
        if event.get("type") == "content_block_delta":
            return str((event.get("delta") or {}).get("text") or "")
        return ""
    if provider == _RESPONSES:
        if event.get("type") in {"response.output_text.delta", "response.refusal.delta"}:
            return str(event.get("delta") or "")
        return ""
    choices = event.get("choices") or []
    if not choices:
        return ""
    piece = choices[0].get("delta") or choices[0].get("message") or {}
    content = piece.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(part.get("text") or "") for part in content if isinstance(part, dict)
        )
    return ""


async def complete_text(
    settings: Settings,
    *,
    system: str,
    prompt: str,
    llm_selection: dict[str, Any] | None = None,
) -> str:
    chunks: list[str] = []
    async for delta in stream_text(
        settings, system=system, prompt=prompt, llm_selection=llm_selection
    ):
        chunks.append(delta)
    return clean_thinking_tags("".join(chunks))
