"""Tool result text must remain readable in the user-facing turn trace."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from app.agent.runtime import _tool_result_is_error, _tool_result_text


def result(text: str, is_error: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        content=[SimpleNamespace(text=text)],
        is_error=is_error,
    )


class ToolResultTextTests(unittest.TestCase):
    def test_extracts_text_content(self) -> None:
        r = result("Video sub-agent failed: APIError: upstream interrupted")
        self.assertEqual(_tool_result_text(r), "Video sub-agent failed: APIError: upstream interrupted")

    def test_joins_multiple_text_parts(self) -> None:
        r = SimpleNamespace(
            content=[SimpleNamespace(text="render failed:"), SimpleNamespace(
                text='  scene voice "s1" has no narration entry')],
            is_error=False,
        )
        self.assertIn("no narration entry", _tool_result_text(r))

    def test_plain_string_passthrough(self) -> None:
        self.assertEqual(_tool_result_text("video ready"), "video ready")

    def test_error_text_is_flagged(self) -> None:
        r = result("render failed: bad scene")
        self.assertTrue(_tool_result_is_error(r, _tool_result_text(r)))
        self.assertTrue(_tool_result_is_error(result("x", is_error=True), "x"))
        self.assertFalse(_tool_result_is_error(result("Video ready."), "Video ready."))


if __name__ == "__main__":
    unittest.main()
