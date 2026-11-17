"""Internal dispatch for ``http.request`` RPC calls.

Matches ``(method, path)`` against the unmounted route table in
``app/api/handlers.py`` and invokes the endpoint directly — no HTTP layer.
"""

from __future__ import annotations

import inspect
import json
import re
from io import BytesIO
from types import SimpleNamespace
from typing import Any

from pydantic import BaseModel

from .shims import HTTPException, UploadFile

from . import handlers


def _shim_request(app: Any) -> Any:
    """Handlers only touch ``request.app.state`` — give them a duck-typed shim."""
    return SimpleNamespace(app=SimpleNamespace(state=app.state))


def _ann_name(annotation: Any) -> str:
    """Annotation may be a string (``from __future__ import annotations``)."""
    if annotation is inspect.Parameter.empty:
        return ""
    if isinstance(annotation, str):
        return annotation.split("|")[0].strip().strip("'" ).strip('"')
    return getattr(annotation, "__name__", "")


def _coerce(value: Any, annotation: Any) -> Any:
    if value is None or annotation is inspect.Parameter.empty:
        return value
    name = _ann_name(annotation)
    try:
        if name == "int":
            return int(value)
        if name == "float":
            return float(value)
        if name == "bool":
            return str(value).lower() in ("1", "true", "yes", "on")
        if name == "str":
            return str(value)
    except (TypeError, ValueError):
        return value
    return value


async def dispatch(
    app: Any,
    method: str,
    path: str,
    query: dict[str, Any] | None,
    body: Any,
) -> tuple[int, Any]:
    """Returns ``(http_status, json_payload)``."""
    query = query or {}
    route = None
    path_params: dict[str, str] = {}
    for r in handlers.router.routes:
        if method.upper() not in r.methods:
            continue
        pattern = re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", r.path)
        m = re.compile(f"^{pattern}$").match(path)
        if m:
            route = r
            path_params = m.groupdict()
            break
    if route is None:
        return 404, {"detail": f"Not Found: {method} {path}"}

    endpoint = route.endpoint
    sig = inspect.signature(endpoint)

    # Multipart body -> UploadFile params.
    files_spec: dict[str, Any] = {}
    if isinstance(body, dict) and "__multipart__" in body:
        spec = body["__multipart__"]
        for f in spec.get("files") or []:
            import base64

            data = base64.b64decode(str(f.get("data_b64") or ""))
            files_spec[str(f.get("name") or "file")] = UploadFile(
                file=BytesIO(data),
                filename=str(f.get("filename") or "file"),
                headers={"content-type": str(f.get("content_type") or "application/octet-stream")},
            )
        body = spec.get("fields") or {}

    kwargs: dict[str, Any] = {}
    body_dict = body if isinstance(body, dict) else {}
    # Resolve string annotations once (handlers use ``from __future__``).
    try:
        import typing as _typing

        type_hints = _typing.get_type_hints(endpoint)
    except Exception:  # noqa: BLE001
        type_hints = {}
    for name, param in sig.parameters.items():
        ann = type_hints.get(name, param.annotation)
        default = param.default

        if name == "request" or ann is inspect.Parameter.empty and name == "request":
            kwargs[name] = _shim_request(app)
        elif name in path_params:
            kwargs[name] = _coerce(path_params[name], ann)
        elif name in files_spec:
            kwargs[name] = files_spec[name]
        elif isinstance(ann, type) and issubclass(ann, BaseModel):
            payload = body_dict if body_dict else (body if isinstance(body, dict) else {})
            try:
                kwargs[name] = ann(**payload)
            except Exception as exc:  # noqa: BLE001
                return 422, {"detail": f"Invalid body for {name}: {exc}"}
        elif ann is UploadFile or name == "file":
            kwargs[name] = files_spec.get(name)
        elif name in ("body", "payload") or _ann_name(ann) in ("dict", "Dict"):
            kwargs[name] = body if body is not None else {}
        elif name in body_dict and default is inspect.Parameter.empty:
            kwargs[name] = body_dict[name]
        elif name in query:
            kwargs[name] = _coerce(query[name], ann)
        elif default is not inspect.Parameter.empty:
            # Query(...) defaults: use the scalar default, not the Query object.
            dv = getattr(default, "default", default)
            kwargs[name] = _coerce(query.get(name, dv), ann)
        elif name in query:
            kwargs[name] = _coerce(query[name], ann)
        else:
            return 422, {"detail": f"Missing parameter: {name}"}

    try:
        if inspect.iscoroutinefunction(endpoint):
            result = await endpoint(**kwargs)
        else:
            result = endpoint(**kwargs)
    except HTTPException as exc:
        return exc.status_code, {"detail": exc.detail}
    except Exception as exc:  # noqa: BLE001
        return 500, {"detail": str(exc)}

    if isinstance(result, dict | list):
        return 200, result
    return 200, {"result": result}
