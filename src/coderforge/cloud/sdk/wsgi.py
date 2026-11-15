"""PEP 3333 adapter for Flask and other WSGI apps in a browser Function.

The platform's JSON event ABI buffers inside the worker. This adapter opens no
listener and does not move application execution or upload files to a server.
"""

from __future__ import annotations

import base64
import contextvars
import io
import sys
from urllib.parse import unquote, urlsplit

from .http import HttpError, Request
from .native import invocation_context


class WsgiAdapter:
    def __init__(
        self,
        application,
        *,
        max_request_bytes=25 * 1024 * 1024,
        max_response_bytes=64 * 1024 * 1024,
    ):
        self.application = application
        self.max_request_bytes = max_request_bytes
        self.max_response_bytes = max_response_bytes

    def __call__(self, event, context=None):
        if "wsgi.version" in event and callable(context):
            return self._native(event, context)
        headers = event.get("headers") or {}
        with invocation_context(headers):
            return self._event(event, context)

    def _native(self, environment, start_response):
        # WSGI iterators can outlive the initial application call. Run every
        # operation in a copied context rather than leaking it across yields.
        scope = contextvars.copy_context()
        captured = {
            "X-CoderForge-Invocation-Token": environment.get("HTTP_X_CODERFORGE_INVOCATION_TOKEN")
        }
        manager = invocation_context(captured)
        scope.run(manager.__enter__)
        try:
            iterable = scope.run(self.application, environment, start_response)
        except BaseException:
            scope.run(manager.__exit__, *sys.exc_info())
            raise

        def stream():
            try:
                iterator = iter(iterable)
                while True:
                    try:
                        chunk = scope.run(next, iterator)
                    except StopIteration:
                        break
                    yield chunk
            finally:
                try:
                    close = getattr(iterable, "close", None)
                    if close:
                        scope.run(close)
                finally:
                    scope.run(manager.__exit__, None, None, None)

        return stream()

    def _event(self, event, context=None):
        request = Request.from_event(event)
        if len(request.body) > self.max_request_bytes:
            raise HttpError(413, "WSGI request exceeds its worker limit")
        headers = {name.lower(): str(value) for name, value in request.headers.items()}
        host = headers.get("host", "function.local")
        parsed = urlsplit("//" + host)
        environment = {
            "REQUEST_METHOD": request.method,
            "SCRIPT_NAME": "",
            "PATH_INFO": unquote(request.path, encoding="latin-1"),
            "QUERY_STRING": urlsplit(request.url).query,
            "SERVER_NAME": parsed.hostname or "function.local",
            "SERVER_PORT": str(parsed.port or 443),
            "SERVER_PROTOCOL": "HTTP/1.1",
            "CONTENT_LENGTH": str(len(request.body)),
            "wsgi.version": (1, 0),
            "wsgi.url_scheme": headers.get("x-forwarded-proto", "https"),
            "wsgi.input": io.BytesIO(request.body),
            "wsgi.errors": sys.stderr,
            "wsgi.multithread": False,
            "wsgi.multiprocess": False,
            "wsgi.run_once": False,
            "coderforge.context": context,
        }
        for name, value in headers.items():
            if name == "content-type":
                environment["CONTENT_TYPE"] = value
            elif name not in ("content-length", "transfer-encoding"):
                environment["HTTP_" + name.upper().replace("-", "_")] = value
        status = 500
        response_headers = []
        pieces, size = [], 0

        def write(chunk):
            nonlocal size
            if not isinstance(chunk, bytes):
                raise TypeError("WSGI applications must yield bytes")
            size += len(chunk)
            if size > self.max_response_bytes:
                raise HttpError(413, "WSGI response exceeds its worker limit")
            pieces.append(chunk)

        def start_response(value, values, exc_info=None):
            nonlocal status, response_headers
            if exc_info:
                raise exc_info[1].with_traceback(exc_info[2])
            status, response_headers = int(value.split(" ", 1)[0]), values
            return write

        iterable = self.application(environment, start_response)
        try:
            for chunk in iterable:
                write(chunk)
        finally:
            close = getattr(iterable, "close", None)
            if close:
                close()
        cookies = [value for name, value in response_headers if name.lower() == "set-cookie"]
        folded = {}
        for name, value in response_headers:
            key = name.lower()
            if key not in ("set-cookie", "content-length", "transfer-encoding", "connection"):
                folded[key] = folded[key] + ", " + value if key in folded else value
        return {
            "statusCode": status,
            "headers": folded,
            "cookies": cookies,
            "body": base64.b64encode(b"".join(pieces)).decode(),
            "isBase64Encoded": True,
        }
