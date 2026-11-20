"""Split inline reasoning out of provider text streams.

vtx already understands the `` thinking … </think>`` form that DeepSeek/Qwen/GLM
style gateways wrap their scratchpad in.  Other reasoning models use different
spellings and, when a gateway forgets to move them into a dedicated
``reasoning``/``reasoning_content`` field, those blocks arrive as ordinary
assistant text and are rendered as if they were the answer.

This module is the provider-agnostic safety net: it recognises the common
tag families (``<thought>``, ``<reasoning>``, ``<analysis>``, the
``<|begin_of_thought|>`` token form, …) as a stream arrives, including tags
split across chunks, and yields ``("think", …)`` / ``("text", …)`` segments so
callers can route them to the right surface.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Final, Literal

SegmentKind = Literal["think", "text"]

# Tags whose contents are model scratchpad rather than a user-facing answer.
REASONING_TAG_NAMES: Final[tuple[str, ...]] = (
    "think",
    "thinking",
    "thought",
    "thoughts",
    "reasoning",
    "analysis",
    "reflection",
)

# OpenAI-style special-token form, e.g. ``<|begin_of_thought|>…<|end_of_thought|>``.
_SPECIAL_TAGS: Final[tuple[str, ...]] = (
    *(f"begin_of_{name}" for name in REASONING_TAG_NAMES),
    *(f"begin_{name}" for name in REASONING_TAG_NAMES),
)

_OPEN_TAGS: tuple[str, ...] = (
    *(f"<{name}>" for name in REASONING_TAG_NAMES),
    *(f"<{name} " for name in REASONING_TAG_NAMES),  # <thinking duration="3s">
    *(f"<|{name}|>" for name in _SPECIAL_TAGS),
)

# close tag for each opening tag prefix, plus the general ``</name>`` form.
_CLOSE_TAGS: dict[str, tuple[str, ...]] = {}
for _name in REASONING_TAG_NAMES:
    _CLOSE_TAGS[f"<{_name}>"] = (f"</{_name}>",)
    _CLOSE_TAGS[f"<{_name} "] = (f"</{_name}>",)
for _special in _SPECIAL_TAGS:
    _CLOSE_TAGS[f"<|{_special}|>"] = (
        f"<|end_of_{_special.removeprefix('begin_of_').removeprefix('begin_')}|>",
        f"<|end_{_special.removeprefix('begin_of_').removeprefix('begin_')}|>",
    )


def _longest_partial_tag_suffix(buffer: str, tags: tuple[str, ...]) -> int:
    """Length of the longest suffix that could begin one of ``tags``."""
    limit = min(max((len(tag) for tag in tags), default=0) - 1, len(buffer))
    for size in range(limit, 0, -1):
        tail = buffer[-size:]
        if any(tag.startswith(tail) for tag in tags):
            return size
    return 0


def _find_open_tag(buffer: str) -> tuple[int, str] | None:
    lowered = buffer.lower()
    best: tuple[int, str] | None = None
    for tag in _OPEN_TAGS:
        index = lowered.find(tag)
        if index == -1:
            continue
        if best is None or index < best[0] or (index == best[0] and len(tag) > len(best[1])):
            best = (index, tag)
    return best


def _find_close_tag(buffer: str, open_tag: str) -> tuple[int, str] | None:
    lowered = buffer.lower()
    best: tuple[int, str] | None = None
    for tag in _CLOSE_TAGS.get(open_tag.lower(), ()):
        index = lowered.find(tag)
        if index == -1:
            continue
        if best is None or index < best[0]:
            best = (index, tag)
    return best


class InlineThinkingSplitter:
    """Incremental splitter for inline reasoning blocks in a text stream."""

    def __init__(self) -> None:
        self._buffer = ""
        self._thinking = False
        self._open_tag = ""

    def feed(self, text: str) -> list[tuple[SegmentKind, str]]:
        if not text:
            return []
        self._buffer += text
        return list(self._drain(final=False))

    def flush(self) -> list[tuple[SegmentKind, str]]:
        return list(self._drain(final=True))

    def _drain(self, *, final: bool) -> Iterator[tuple[SegmentKind, str]]:
        while self._buffer:
            if self._thinking:
                found = _find_close_tag(self._buffer, self._open_tag)
                if found is None:
                    if final:
                        yield ("think", self._buffer)
                        self._buffer = ""
                        break
                    hold = _longest_partial_tag_suffix(
                        self._buffer, _CLOSE_TAGS.get(self._open_tag.lower(), ())
                    )
                    if hold:
                        head = self._buffer[:-hold]
                        self._buffer = self._buffer[-hold:]
                    else:
                        head = self._buffer
                        self._buffer = ""
                    if head:
                        yield ("think", head)
                    break
                index, close_tag = found
                head = self._buffer[:index]
                if head:
                    yield ("think", head)
                self._buffer = self._buffer[index + len(close_tag):]
                self._thinking = False
                self._open_tag = ""
                continue

            found = _find_open_tag(self._buffer)
            if found is None:
                if final:
                    if self._buffer:
                        yield ("text", self._buffer)
                        self._buffer = ""
                    break
                hold = _longest_partial_tag_suffix(self._buffer, _OPEN_TAGS)
                if hold:
                    head = self._buffer[:-hold]
                    self._buffer = self._buffer[-hold:]
                else:
                    head = self._buffer
                    self._buffer = ""
                if head:
                    yield ("text", head)
                break

            index, open_tag = found
            head = self._buffer[:index]
            if head:
                yield ("text", head)
            self._buffer = self._buffer[index + len(open_tag):]
            self._thinking = True
            self._open_tag = open_tag


def split_inline_reasoning(text: str) -> list[tuple[SegmentKind, str]]:
    """Split a complete string into text / reasoning segments."""
    if not text:
        return []
    splitter = InlineThinkingSplitter()
    return splitter.feed(text) + splitter.flush()


def strip_inline_reasoning(text: str) -> str:
    """Return only the user-facing text of ``text`` with reasoning removed."""
    return "".join(
        piece for kind, piece in split_inline_reasoning(text) if kind == "text"
    ).strip()
