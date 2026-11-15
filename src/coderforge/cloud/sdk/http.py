"""HTTP routing with single-consumer byte streams and an explicit event adapter."""

from __future__ import annotations

import base64
import inspect
import json
import re
from collections.abc import AsyncIterable, AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit


class HttpError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


async def _resolve(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


@dataclass
class Request:
    method: str
    url: str
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes | AsyncIterable[bytes] = b""
    params: dict[str, str] = field(default_factory=dict)
    _consumed: bool = field(default=False, init=False, repr=False)

    @property
    def path(self) -> str:
        return urlsplit(self.url).path or "/"

    @property
    def query(self) -> dict[str, list[str]]:
        return parse_qs(urlsplit(self.url).query, keep_blank_values=True)

    async def stream(self) -> AsyncIterator[bytes]:
        if self._consumed:
            raise HttpError(400, "Request body has already been consumed")
        self._consumed = True
        if isinstance(self.body, bytes):
            if self.body:
                yield self.body
        else:
            async for chunk in self.body:
                if not isinstance(chunk, bytes):
                    raise TypeError("Body chunks must be bytes")
                yield chunk

    async def read(self, limit: int = 256 * 1024) -> bytes:
        pieces, size = [], 0
        async for chunk in self.stream():
            size += len(chunk)
            if size > limit:
                raise HttpError(413, "Request body exceeds its read limit")
            pieces.append(chunk)
        return b"".join(pieces)

    async def json(self, limit: int = 256 * 1024) -> Any:
        try:
            return json.loads(await self.read(limit))
        except (ValueError, UnicodeError) as error:
            raise HttpError(400, "Invalid JSON body") from error

    @classmethod
    def from_event(cls, event: Mapping[str, Any]) -> Request:
        http = (event.get("requestContext") or {}).get("http") or {}
        method = http.get("method") or event.get("httpMethod") or event.get("method") or "GET"
        path = event.get("rawPath") or event.get("path") or "/"
        query = event.get("rawQueryString") or ""
        raw = event.get("body") or ""
        try:
            body = (
                base64.b64decode(raw, validate=True)
                if event.get("isBase64Encoded")
                else raw.encode("utf-8")
            )
        except (ValueError, AttributeError) as error:
            raise HttpError(400, "Invalid event body") from error
        return cls(
            method.upper(),
            path + ("?" + query if query else ""),
            event.get("headers") or {},
            body,
        )


@dataclass
class Response:
    body: bytes | AsyncIterable[bytes] = b""
    status: int = 200
    headers: dict[str, str] = field(default_factory=dict)

    @classmethod
    def json(cls, value: Any, status: int = 200) -> Response:
        return cls(
            json.dumps(value, ensure_ascii=False, allow_nan=False).encode(),
            status,
            {"content-type": "application/json; charset=utf-8"},
        )

    async def to_event(self, limit: int = 256 * 1024) -> dict[str, Any]:
        # The legacy JSON ABI is necessarily buffered. Native stream adapters
        # must consume Response.body directly instead of calling this method.
        size, pieces = 0, []
        stream = Request("GET", "/", body=self.body).stream()
        async for chunk in stream:
            size += len(chunk)
            if size > limit:
                raise HttpError(413, "Response exceeds the event adapter limit")
            pieces.append(chunk)
        return {
            "statusCode": self.status,
            "headers": self.headers,
            "body": base64.b64encode(b"".join(pieces)).decode(),
            "isBase64Encoded": True,
        }


class Router:
    """Handlers receive (request, context); middleware also receives next()."""

    def __init__(self):
        self._routes: list[tuple[str, re.Pattern, Callable]] = []
        self._middleware: list[Callable] = []

    def route(self, method: str, path: str, handler: Callable | None = None):
        def register(function: Callable):
            if not re.fullmatch(r"/[A-Za-z0-9_./{}-]*", path):
                raise ValueError("Routes must use absolute paths and {parameter} segments")
            names = re.findall(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", path)
            if len(names) != len(set(names)):
                raise ValueError("Duplicate route parameter")
            pattern = re.escape(path)
            for name in names:
                pattern = pattern.replace(re.escape("{" + name + "}"), f"(?P<{name}>[^/]+)")
            if "\\{" in pattern or "\\}" in pattern:
                raise ValueError("Invalid route parameter")
            verb = method.upper()
            if not re.fullmatch(r"[A-Z]+", verb) or any(
                m == verb and p.pattern == "^" + pattern + "$" for m, p, _ in self._routes
            ):
                raise ValueError("Invalid or duplicate route")
            self._routes.append((verb, re.compile("^" + pattern + "$"), function))
            return function

        return register(handler) if handler else register

    def use(self, middleware: Callable) -> Router:
        self._middleware.append(middleware)
        return self

    async def handle(self, request: Request, context: Any = None) -> Response:
        method = request.method.upper()
        matches = [
            (m, pattern.fullmatch(request.path), h)
            for m, pattern, h in self._routes
            if pattern.fullmatch(request.path)
        ]
        allowed = {m for m, _, _ in matches} | (
            {"HEAD"} if any(m == "GET" for m, _, _ in matches) else set()
        )
        selected = next(((match, h) for m, match, h in matches if m == method), None)
        if selected is None and method == "HEAD":
            selected = next(((match, h) for m, match, h in matches if m == "GET"), None)

        async def endpoint():
            if not selected:
                if not matches:
                    return Response.json({"error": "Not found"}, 404)
                return Response(
                    b"",
                    204 if method == "OPTIONS" else 405,
                    {"allow": ", ".join(sorted(allowed | {"OPTIONS"}))},
                )
            _, handler = selected
            return await _resolve(handler(request, context))

        async def dispatch(index: int):
            if index == len(self._middleware):
                return await endpoint()
            called = False

            async def following():
                nonlocal called
                if called:
                    raise RuntimeError("Middleware next() can only be called once")
                called = True
                return await dispatch(index + 1)

            return await _resolve(self._middleware[index](request, context, following))

        try:
            request.params = {}
            if selected:
                match, _ = selected
                if any(
                    re.search(r"%(?![0-9A-Fa-f]{2})", value) for value in match.groupdict().values()
                ):
                    raise HttpError(400, "Invalid path encoding")
                request.params = {
                    key: unquote(value, errors="strict") for key, value in match.groupdict().items()
                }
            response = await dispatch(0)
        except HttpError as error:
            response = Response.json({"error": str(error)}, error.status)
        except UnicodeError:
            response = Response.json({"error": "Invalid path encoding"}, 400)
        if not isinstance(response, Response):
            raise TypeError("A route must return a Response")
        return Response(b"", response.status, response.headers) if method == "HEAD" else response
