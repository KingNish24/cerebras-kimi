"""Example guardrails for the tutor agent.

vtx guardrails are plain functions decorated with ``@input_guardrail`` /
``@output_guardrail`` / ``@tool_input_guardrail`` / ``@tool_output_guardrail``.
They run concurrently via ``asyncio.gather``; returning a tripwire aborts or
rewrites what the model sees. See ``docs/sdk.md`` in vtx-coding-agent.
"""

from __future__ import annotations

from vtx.agent.sdk import (
    GuardrailFunctionOutput,
    ToolGuardrailFunctionOutput,
    input_guardrail,
    output_guardrail,
    tool_input_guardrail,
)

_MAX_INPUT_CHARS = 200_000


@input_guardrail
def limit_input_size(data):
    """Abort before any model call when the user input is absurdly large."""
    if len(data.input or "") > _MAX_INPUT_CHARS:
        return GuardrailFunctionOutput(
            output_info={"reason": "input_too_large"},
            tripwire_triggered=True,
        )
    return GuardrailFunctionOutput()


@tool_input_guardrail
def block_sensitive_tool_args(context, tool_name, tool_arguments):
    """Stop file tools from touching obvious secret paths."""
    import json

    try:
        args = json.loads(tool_arguments or "{}")
    except json.JSONDecodeError:
        return ToolGuardrailFunctionOutput.allow()
    path = str(args.get("path", ""))
    if any(token in path for token in (".env", "id_rsa", ".ssh", "credentials")):
        return ToolGuardrailFunctionOutput.reject_content(
            "Access to credential or secret files is not allowed."
        )
    return ToolGuardrailFunctionOutput.allow()


@output_guardrail
def strip_empty_output(data):
    """Replace an empty final output with a fallback so the UI never blanks."""
    if not (data.output or "").strip():
        return GuardrailFunctionOutput(
            output_info={"substituted": "empty_output"},
            tripwire_triggered=True,
        )
    return GuardrailFunctionOutput()
