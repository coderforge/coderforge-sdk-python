"""Invocation-scoped HTTP transport inside an explicitly configured mini-x64 guest."""

from __future__ import annotations

import asyncio
import contextvars
import json
import os
import re
import sys
import uuid
from contextlib import contextmanager
from urllib.parse import urlsplit

from .client import MAX_MESSAGE_BYTES, SdkError

# None means no HTTP request. A list containing None means an HTTP request
# without a grant, which must never inherit a process-level token.
_invocation = contextvars.ContextVar("coderforge_invocation", default=None)


def _header(headers):
    for name, value in headers.items():
        if name.lower() == "x-coderforge-invocation-token":
            return value
    return None


@contextmanager
def invocation_context(headers):
    """Capture the trusted invocation header before application code starts."""
    captured = [_header(headers)]
    marker = _invocation.set(captured)
    try:
        yield
    finally:
        captured[0] = None
        _invocation.reset(marker)


def _token():
    current = _invocation.get()
    if current is not None:
        value = current[0]
    else:
        # Flask already isolates concurrent requests. Importing Flask is not an
        # SDK dependency; only use its existing context when the app loaded it.
        flask = sys.modules.get("flask")
        if flask is not None and flask.has_request_context():
            value = flask.request.headers.get("X-CoderForge-Invocation-Token")
        else:
            value = os.environ.get("CODERFORGE_INVOCATION_TOKEN")
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9._~+/-]{1,8192}=*", value):
        raise SdkError("CAPABILITY_UNAVAILABLE", "Missing or invalid invocation token")
    return value


def native_transport():
    """Select native HTTP only for mini-x64; injected browser callbacks win."""
    if os.environ.get("CODERFORGE_RUNTIME") != "mini-x64":
        return None
    endpoint = os.environ.get("CODERFORGE_SDK_URL", "")
    try:
        parsed = urlsplit(endpoint)
        valid = (
            parsed.scheme in ("http", "https")
            and len(endpoint) <= 2048
            and parsed.hostname
            and parsed.port != 0
            and parsed.username is None
            and parsed.password is None
            and not parsed.fragment
            and not re.search(r"[\x00-\x20\x7f]", endpoint)
        )
    except ValueError:
        valid = False
    if not valid:
        raise SdkError("CAPABILITY_UNAVAILABLE", "Invalid mini-x64 SDK endpoint")

    def send(envelope, token):
        import urllib.error
        import urllib.request

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None

        body = json.dumps(
            envelope, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode()
        if len(body) > MAX_MESSAGE_BYTES:
            raise ValueError("SDK message exceeds 256 KiB")
        request = urllib.request.Request(
            endpoint,
            data=body,
            method="POST",
            headers={
                "Authorization": "Bearer " + token,
                "Content-Type": "application/json",
                "Accept": "application/json",
                "X-CoderForge-Request-Id": str(uuid.uuid4()),
            },
        )
        timeout = max(30, envelope.get("arguments", {}).get("timeout_ms", 0) / 1000 + 5)
        # The SDK talks to one configured bridge, without proxies or redirects.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        try:
            try:
                response = opener.open(request, timeout=timeout)
            except urllib.error.HTTPError as error:
                response = error
            with response:
                raw = response.read(MAX_MESSAGE_BYTES + 1)
                status = response.status
            if len(raw) > MAX_MESSAGE_BYTES:
                raise SdkError("PROTOCOL_ERROR", "Native SDK response exceeds 256 KiB")
            try:
                value = json.loads(raw)
            except (ValueError, UnicodeError) as error:
                raise SdkError("PROTOCOL_ERROR", "Invalid native SDK response") from error
            if not 200 <= status < 300 and not (
                isinstance(value, dict) and value.get("ok") is False
            ):
                raise SdkError("TRANSPORT_ERROR", "Native SDK request failed")
            return value
        except (OSError, urllib.error.URLError) as error:
            raise SdkError("TRANSPORT_ERROR", "Native SDK request failed") from error

    async def request(envelope):
        # Capture before yielding: a warm client never stores a request token.
        token = _token()
        return await asyncio.to_thread(send, envelope, token)

    return request
