"""Inline reasoning tags must never be published as the assistant's answer."""

from __future__ import annotations

import unittest

from app.services.inline_thinking import (
    InlineThinkingSplitter,
    split_inline_reasoning,
    strip_inline_reasoning,
)
from app.services.llm_text import clean_thinking_tags


class InlineThinkingTests(unittest.TestCase):
    def test_splits_unknown_tag_families(self) -> None:
        for text, answer in (
            ("<thought>scratch</thought>Hi", "Hi"),
            ("<reasoning>scratch</reasoning>Hi", "Hi"),
            ("<analysis reason='x'>scratch</analysis>Hi", "Hi"),
            ("<|begin_of_thought|>scratch<|end_of_thought|>Hi", "Hi"),
        ):
            self.assertEqual(strip_inline_reasoning(text), answer, text)

    def test_stream_handles_tags_split_across_chunks(self) -> None:
        splitter = InlineThinkingSplitter()
        segments = []
        for chunk in ("<thou", "ght>scr", "atch</tho", "ught>Hello"):
            segments.extend(splitter.feed(chunk))
        segments.extend(splitter.flush())
        thinking = "".join(piece for kind, piece in segments if kind == "think")
        answer = "".join(piece for kind, piece in segments if kind == "text")
        self.assertEqual(thinking, "scratch")
        self.assertEqual(answer, "Hello")

    def test_unclosed_block_is_reasoning_not_answer(self) -> None:
        self.assertEqual(strip_inline_reasoning("<thought>still thinking"), "")
        self.assertEqual(clean_thinking_tags("<thinking>x</thinking>\nAnswer"), "Answer")

    def test_plain_text_is_untouched(self) -> None:
        segments = split_inline_reasoning("17 × 23 = 391")
        self.assertEqual(segments, [("text", "17 × 23 = 391")])


if __name__ == "__main__":
    unittest.main()
