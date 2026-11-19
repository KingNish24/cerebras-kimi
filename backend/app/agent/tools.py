"""Built-in tools exposed to the tutor agent.

Each function is wrapped with vtx's ``@tool`` decorator: the decorator builds
a Pydantic params model from the signature and docstring, registers the tool
with the agent, and routes LLM tool calls through ``Runner``. ``mutating``
marks whether the tool changes state — the permission layer uses it to decide
what needs approval.
"""

from __future__ import annotations

import ast
import math
import time
from pathlib import Path
from typing import Any

import httpx
from .toolkit import tool
from ..config import Settings


def _safe_eval(node: ast.AST) -> float:
    """Evaluate a numeric AST — arithmetic, constants, and math functions only."""
    if isinstance(node, ast.Expression):
        return _safe_eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return float(node.value)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        operand = _safe_eval(node.operand)
        return operand if isinstance(node.op, ast.UAdd) else -operand
    if isinstance(node, ast.BinOp):
        left, right = _safe_eval(node.left), _safe_eval(node.right)
        ops = {
            ast.Add: lambda: left + right,
            ast.Sub: lambda: left - right,
            ast.Mult: lambda: left * right,
            ast.Div: lambda: left / right,
            ast.Mod: lambda: left % right,
            ast.Pow: lambda: left**right,
        }
        for op_type, fn in ops.items():
            if isinstance(node.op, op_type):
                return fn()
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        fn = getattr(math, node.func.id, None)
        if callable(fn):
            args = [_safe_eval(a) for a in node.args]
            return float(fn(*args))
    raise ValueError("unsupported expression")


@tool(mutating=False, tool_icon="🧮")
def calculate(expression: str) -> str:
    """Evaluate a mathematical expression (arithmetic + math functions).

    :param expression: Expression to evaluate, e.g. "sqrt(2) * pi / 4".
    """
    try:
        tree = ast.parse(expression, mode="eval")
        result = _safe_eval(tree)
        return f"{expression} = {result:g}"
    except Exception as exc:  # noqa: BLE001 - tool errors become tool results
        return f"Error: could not evaluate '{expression}': {exc}"


@tool(mutating=False, tool_icon="🕐")
def current_time() -> str:
    """Return the current local date and time."""
    return time.strftime("%Y-%m-%d %H:%M:%S %Z")


@tool(mutating=False, tool_icon="🌐")
async def fetch_url(url: str, max_chars: int = 8000) -> str:
    """Fetch a web page and return its text content (HTML stripped).

    :param url: HTTP(S) URL to fetch.
    :param max_chars: Maximum characters of text to return.
    """
    try:
        async with httpx.AsyncClient(
            timeout=20.0,
            follow_redirects=True,
            headers={"User-Agent": "DeepTutor/0.1 (+https://deeptutor.info)"},
        ) as client:
            response = await client.get(url)
            response.raise_for_status()
        text = response.text
        # Cheap HTML → text: drop scripts/styles/tags.
        import re

        text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", text)
        text = re.sub(r"(?s)<[^>]+>", " ", text)
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
        return text[:max_chars]
    except Exception as exc:  # noqa: BLE001
        return f"Error fetching {url}: {exc}"


class WorkspaceFiles:
    """File tools jailed to the configured workspace directory.

    vtx's ``@tool`` builds params from a plain function, so the jail root is
    captured by closure rather than passed as a tool argument (the model must
    never choose it).
    """

    def __init__(self, settings: Settings) -> None:
        self._root = settings.workspace_dir.resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    def _resolve(self, path: str) -> Path:
        candidate = (self._root / path.lstrip("/")).resolve()
        if candidate != self._root and self._root not in candidate.parents:
            raise ValueError(f"path escapes workspace: {path}")
        return candidate

    def tools(self) -> list[Any]:
        root = self._root

        @tool(mutating=False, tool_icon="📄")
        def read_file(path: str, max_chars: int = 12000) -> str:
            """Read a UTF-8 text file from the workspace.

            :param path: Workspace-relative file path.
            :param max_chars: Maximum characters to return.
            """
            try:
                resolved = WorkspaceFiles._resolve_static(root, path)
                text = resolved.read_text(encoding="utf-8", errors="replace")
                return text[:max_chars]
            except Exception as exc:  # noqa: BLE001
                return f"Error reading {path}: {exc}"

        @tool(mutating=True, tool_icon="📝")
        def write_file(path: str, content: str) -> str:
            """Write a UTF-8 text file into the workspace, creating parents.

            :param path: Workspace-relative file path.
            :param content: Full file content (overwrites existing file).
            """
            try:
                resolved = WorkspaceFiles._resolve_static(root, path)
                resolved.parent.mkdir(parents=True, exist_ok=True)
                resolved.write_text(content, encoding="utf-8")
                return f"Wrote {len(content)} chars to {path}"
            except Exception as exc:  # noqa: BLE001
                return f"Error writing {path}: {exc}"

        @tool(mutating=False, tool_icon="📁")
        def list_files(path: str = ".") -> str:
            """List files and directories under a workspace path.

            :param path: Workspace-relative directory path (default: root).
            """
            try:
                resolved = WorkspaceFiles._resolve_static(root, path)
                if not resolved.is_dir():
                    return f"Not a directory: {path}"
                entries = sorted(
                    f"{'d' if p.is_dir() else 'f'} {p.relative_to(root)}"
                    for p in resolved.iterdir()
                )
                return "\n".join(entries[:500]) or "(empty)"
            except Exception as exc:  # noqa: BLE001
                return f"Error listing {path}: {exc}"

        return [read_file, write_file, list_files]

    @staticmethod
    def _resolve_static(root: Path, path: str) -> Path:
        candidate = (root / path.lstrip("/")).resolve()
        if candidate != root and root not in candidate.parents:
            raise ValueError(f"path escapes workspace: {path}")
        return candidate




def _expand_magic(code: str) -> str:
    """Rewrite ``%bash <cmd>`` lines into subprocess calls."""
    out_lines = []
    used = False
    for line in code.split("\n"):
        stripped = line.lstrip()
        indent = line[: len(line) - len(stripped)]
        if stripped.startswith("%bash"):
            cmd = stripped[len("%bash"):].strip()
            if cmd.startswith(('"', "'")) and cmd.endswith(cmd[0]):
                cmd = cmd[1:-1]
            out_lines.append(
                f'{indent}import subprocess as _sp\n'
                f'{indent}_r = _sp.run({cmd!r}, shell=True, capture_output=True, text=True, timeout=120)\n'
                f'{indent}print(_r.stdout); print(_r.stderr, file=__import__("sys").stderr) if _r.stderr else None'
            )
            used = True
        else:
            out_lines.append(line)
    if used and "import subprocess" not in code:
        out_lines.insert(0, "import subprocess")
    return "\n".join(out_lines)



def adapted_registry_tools(registry: dict[str, Any]) -> list[Any]:
    """Adapt SDK FunctionTools to vtx.codemode.CodemodeTool declarations."""
    from vtx.codemode import CodemodeTool

    out = []
    for name, fn in registry.items():
        if name == "codemode":
            continue

        async def invoke(args: dict, signal, _fn=fn, _name=name):  # noqa: ANN001
            try:
                params = _fn.params(**{k: v for k, v in args.items() if v is not None})
                res = await _fn.execute(params)
                return getattr(res, "result", None) or str(res)
            except Exception as exc:  # noqa: BLE001
                return f"tool {_name} error: {exc}"

        schema_props = {}
        try:
            for fname, f in fn.params.model_fields.items():
                schema_props[fname] = {
                    "type": "string",
                    "description": getattr(f, "description", "") or "",
                }
        except Exception:  # noqa: BLE001
            pass
        out.append(CodemodeTool(
            name=name,
            description=(fn.description or name)[:400],
            execute=invoke,
            input_schema={"type": "object", "properties": schema_props},
        ))
    return out


def builtin_tools(settings: Settings, enabled: list[str] | None = None) -> list[Any]:
    """All builtin tools, filtered by the turn's ``tools`` allowlist.

    ``enabled=None`` means "everything"; an empty list also means everything
    (the frontend sends the user's tool selection — absent = unrestricted).
    Names follow DeepTutor's tool catalog (see ``reff/deeptutor/tools``).
    """
    from . import dt_tools
    from . import kb_tools as _kb

    workspace = WorkspaceFiles(settings)
    ws_tools = workspace.tools()
    registry: dict[str, Any] = {
        # DeepTutor portable ports (dt_tools.py)
        "web": dt_tools.web,
        "paper_search": dt_tools.paper_search,
        "notes": dt_tools.notes,
        "question_bank": dt_tools.question_bank,
        "memory": dt_tools.memory,
        "knowledge_base_search": _kb.knowledge_base_search,
        # File-inspection/editing tools are intentionally NOT exposed: DeepTutor
        # is a tutor, not a coding agent. The learner-facing `workspace` tool
        # still handles artifacts the app itself creates (notes, media).
        "learning": dt_tools.learning,
        "workspace": dt_tools.workspace,
        # `ask_user` is intentionally not registered for now.
        "mastery": dt_tools.mastery,
        "video": dt_tools.video,
    }
    # DeepTutor semantics: only the user-toggleable tools are gated by the
    # frontend's `tools` list; every other built-in (workspace files, exec,
    # web_fetch, github, notes, memory, rag/kb, calculate…) is locked-on and
    # must always reach the model's schema.
    toggleable = {
        "web", "web_search", "paper_search",
        "geogebra_analysis", "videogen",
    }
    # web_search is the frontend's toggle name for the merged `web` tool.
    enabled = [{"web_search": "web"}.get(n, n) for n in (enabled or [])]
    locked_on = set(registry) - toggleable
    if enabled:
        keep = locked_on | ({n for n in enabled if n in toggleable} & set(registry))
        return [registry[name] for name in registry if name in keep]
    return list(registry.values())
