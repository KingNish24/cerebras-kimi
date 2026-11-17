"""Minimal shims replacing the FastAPI primitives the handler registry used.

The handlers in ``app/api/handlers.py`` were written against FastAPI
decorators/signatures; these classes keep that surface working without the
framework — the router is an unmounted route table and the dispatcher calls
endpoints directly.
"""

from __future__ import annotations

import re
from io import BytesIO
from typing import Any, Callable

# Handlers annotate ``request: Request`` but only touch ``request.app.state``.
Request = Any


class HTTPException(Exception):
    def __init__(self, status_code: int, detail: Any = None) -> None:
        super().__init__(str(detail))
        self.status_code = status_code
        self.detail = detail


class Query:
    """Marker for a query-string parameter default (``Query(50, ge=1)``)."""

    def __init__(self, default: Any = None, **_constraints: Any) -> None:
        self.default = default


class UploadFile:
    """Tiny UploadFile stand-in: handlers only ``await file.read()``."""

    def __init__(self, file: BytesIO, filename: str = "file", headers: dict | None = None) -> None:
        self.file = file
        self.filename = filename
        self.headers = headers or {}
        self._read = False

    async def read(self) -> bytes:
        self.file.seek(0)
        data = self.file.read()
        self._read = True
        return data

    async def close(self) -> None:
        self.file.close()


class _Route:
    __slots__ = ("path", "methods", "endpoint")

    def __init__(self, path: str, methods: list[str], endpoint: Callable) -> None:
        self.path = path
        self.methods = methods
        self.endpoint = endpoint

    def compile(self) -> re.Pattern:
        pattern = re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", self.path)
        return re.compile(f"^{pattern}$")


class APIRouter:
    """Route-table builder — records (method, path, endpoint) only."""

    def __init__(self, prefix: str = "") -> None:
        self.prefix = prefix
        self.routes: list[_Route] = []

    def _add(self, methods: list[str], path: str):
        def decorator(fn: Callable) -> Callable:
            self.routes.append(_Route(self.prefix + path, methods, fn))
            return fn
        return decorator

    def get(self, path: str, **_kw: Any):
        return self._add(["GET"], path)

    def post(self, path: str, **_kw: Any):
        return self._add(["POST"], path)

    def put(self, path: str, **_kw: Any):
        return self._add(["PUT"], path)

    def delete(self, path: str, **_kw: Any):
        return self._add(["DELETE"], path)

    def patch(self, path: str, **_kw: Any):
        return self._add(["PATCH"], path)

    def api_route(self, path: str, methods: list[str] | None = None, **_kw: Any):
        return self._add(methods or ["GET"], path)
