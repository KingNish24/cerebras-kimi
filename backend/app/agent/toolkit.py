"""Local ``@tool`` decorator — builds vtx ``BaseTool`` instances directly,
no ``vtx.agent.sdk`` involvement. Same call shape the SDK decorator had:
``@tool(mutating=..., tool_icon=...)`` over a plain function."""

from __future__ import annotations

import inspect
from typing import Any, get_type_hints

from pydantic import create_model
from vtx.agent.tools.base import BaseTool
from vtx.protocol.types import ToolResult


def _params_model(fn):
    hints = get_type_hints(fn)
    fields = {}
    for name, param in inspect.signature(fn).parameters.items():
        if name in ("self", "cls"):
            continue
        ann = hints.get(name, str)
        default = param.default
        if default is inspect.Parameter.empty:
            fields[name] = (ann, ...)
        else:
            fields[name] = (ann, default)
    # Pydantic builds the model from a dynamic field dict; the overloads
    # cannot express that shape.
    return create_model(f"{fn.__name__}Params", **fields)  # ty: ignore[no-matching-overload]


class FunctionTool(BaseTool):
    def __init__(self, fn, *, mutating: bool, tool_icon: str) -> None:
        self._fn = fn
        self.name = fn.__name__
        self.description = (fn.__doc__ or "").strip()
        self.params = _params_model(fn)
        self.mutating = mutating
        self.tool_icon = tool_icon
        self.prompt_guidelines: tuple = ()

    def format_call(self, params: Any) -> str:
        try:
            return ", ".join(f"{k}={v!r}" for k, v in params.model_dump().items())[:120]
        except Exception:  # noqa: BLE001
            return self.name

    def format_preview(self, params: Any) -> str | None:
        return None

    async def execute(self, params: Any, cancel_event: Any = None) -> ToolResult:
        try:
            kwargs = params.model_dump(exclude_none=True) if hasattr(params, "model_dump") else dict(params)
            if inspect.iscoroutinefunction(self._fn):
                out = await self._fn(**kwargs)
            else:
                out = self._fn(**kwargs)
            return ToolResult(success=True, result=str(out))
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, result=f"{self.name} error: {exc}")


def tool(*, mutating: bool = True, tool_icon: str = "🛠️"):
    def deco(fn):
        return FunctionTool(fn, mutating=mutating, tool_icon=tool_icon)
    return deco
