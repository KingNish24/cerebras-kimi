
from __future__ import annotations

import logging
import re

import httpx

from ..config import Settings, get_settings

log = logging.getLogger(__name__)

# Kept well under the client's post-``done`` socket hold (15s): the title is
# pushed as a late ``session_meta`` frame, so a slow endpoint must give up while
# the stream that carries the frame is still open.
_TIMEOUT = httpx.Timeout(6.0, connect=4.0)
_MODEL = "llama3.1-8B"
_MAX_TITLE_CHARS = 80

_STATS_TAIL = re.compile(r"<\|stats\|>.*", re.DOTALL)
_WRAPPER_QUOTES = re.compile(r"^[\s\"'`“”]+|[\s\"'`“”.]+$")


def _clean_title(text: str) -> str:
    title = _STATS_TAIL.sub("", text or "").strip()
    # Models like to wrap the answer in quotes or a markdown heading.
    title = re.sub(r"^#+\s*", "", title)
    title = _WRAPPER_QUOTES.sub("", title).strip()
    title = " ".join(title.split())
    if len(title) > _MAX_TITLE_CHARS:
        title = title[:_MAX_TITLE_CHARS].rstrip() + "…"
    return title


async def generate_title(
    user_text: str,
    assistant_text: str = "",
    *,
    settings: Settings | None = None,
) -> str | None:
    """Return a short chat title, or ``None`` when generation is unavailable."""
    settings = settings or get_settings()
    url = (settings.title_api_url or "").strip()
    if not url or url.lower() in {"off", "none", "disabled"}:
        return None
    user_text = " ".join((user_text or "").split())[:2000]
    if not user_text:
        return None
    assistant_text = " ".join((assistant_text or "").split())[:1000]

    prompt = (
        "Write a short title (3-8 words) for this conversation. "
        "Reply with only the title — no quotes, no punctuation at the end.\n\n"
        f"User: {user_text}\n"
    )
    if assistant_text:
        prompt += f"Assistant: {assistant_text}\n"

    body = {
        "messages": [{"role": "user", "content": prompt}],
        "chatOptions": {"selectedModel": _MODEL, "systemPrompt": "", "topK": 8},
        "attachment": None,
    }
    headers = {
        "content-type": "application/json",
        "origin": "https://chatjimmy.ai",
        "referer": "https://chatjimmy.ai/",
        "user-agent": "Mozilla/5.0 (X11; Linux x86_64) DeepTutor/0.1",
    }
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.post(url, headers=headers, json=body)
            response.raise_for_status()
            title = _clean_title(response.text)
    except Exception as exc:  # noqa: BLE001
        log.warning("title generation failed: %s", exc)
        return None
    return title or None
