"""Typed capability requests; the host owns identity, policy and deadlines."""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from typing import Any

Transport = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
MAX_MESSAGE_BYTES = 256 * 1024


class SdkError(Exception):
    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.code, self.retryable = code, retryable


def json_transport(bridge: Callable[[str], Awaitable[str]]) -> Transport:
    """Adapt an explicitly supplied WASM/JS host callback using JSON text."""

    async def request(envelope: dict[str, Any]) -> dict[str, Any]:
        raw = await bridge(json.dumps(envelope, ensure_ascii=False, allow_nan=False))
        if not isinstance(raw, str) or len(raw.encode()) > MAX_MESSAGE_BYTES:
            raise SdkError("PROTOCOL_ERROR", "Invalid JSON bridge response")
        try:
            return json.loads(raw)
        except (ValueError, TypeError) as error:
            raise SdkError("PROTOCOL_ERROR", "Invalid JSON bridge response") from error

    return request


class Secret:
    """Explicit disclosure only. This does not hide values from a worker owner."""

    __slots__ = ("_value",)

    def __init__(self, value: str):
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "Secret([REDACTED])"

    __str__ = __repr__


def _rid(value: str, kind: str) -> None:
    # region:zone:kind:number; region and zone are lowercase letters, digits, dots and dashes
    if not isinstance(value, str) or not re.fullmatch(
        rf"[a-z0-9][a-z0-9.-]*:[a-z0-9][a-z0-9.-]*:{kind}:[0-9]+", value
    ):
        raise ValueError(f"A canonical {kind} RID is required")


def _json_copy(value: Any) -> Any:
    def validate(item: Any, parents: set[int]) -> None:
        if item is None or type(item) in (str, bool, int, float):
            return
        if type(item) not in (list, dict) or id(item) in parents:
            raise ValueError("SDK messages must contain only JSON values")
        if isinstance(item, dict) and any(not isinstance(key, str) for key in item):
            raise ValueError("JSON object keys must be strings")
        parents.add(id(item))
        for child in item.values() if isinstance(item, dict) else item:
            validate(child, parents)
        parents.remove(id(item))

    try:
        validate(value, set())
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        if len(encoded.encode()) > MAX_MESSAGE_BYTES:
            raise ValueError("SDK message exceeds 256 KiB")
        return json.loads(encoded)
    except (TypeError, OverflowError, RecursionError) as error:
        raise ValueError("SDK messages must contain only JSON values") from error


class Client:
    def __init__(self, transport: Transport | None = None):
        if transport is None:
            from .native import native_transport

            transport = native_transport()
        self._transport = transport
        self.functions = Functions(self)
        self.messaging = Messaging(self)
        self.secrets = Secrets(self)

    async def _request(self, operation: str, arguments: dict[str, Any]) -> Any:
        if self._transport is None:
            raise SdkError(
                "CAPABILITY_UNAVAILABLE",
                "The runtime has not supplied an SDK transport",
            )
        request = _json_copy({"version": 1, "operation": operation, "arguments": arguments})
        response = await self._transport(request)
        if not isinstance(response, dict) or type(response.get("ok")) is not bool:
            raise SdkError("PROTOCOL_ERROR", "Invalid capability response")
        if response["ok"]:
            if "value" not in response:
                raise SdkError("PROTOCOL_ERROR", "Missing capability response value")
            return _json_copy(response["value"])
        error = response.get("error")
        if (
            not isinstance(error, dict)
            or not isinstance(error.get("code"), str)
            or not isinstance(error.get("message"), str)
        ):
            raise SdkError("PROTOCOL_ERROR", "Invalid capability error")
        raise SdkError(error["code"], error["message"], error.get("retryable") is True)


class Functions:
    def __init__(self, client: Client):
        self._client = client

    async def invoke(self, rid: str, event: Any, *, timeout_ms: int = 30_000) -> Any:
        _rid(rid, "fnc")
        if type(timeout_ms) is not int or not 1 <= timeout_ms <= 300_000:
            raise ValueError("timeout_ms must be an integer between 1 and 300000")
        return await self._client._request(
            "functions.invoke", {"rid": rid, "event": event, "timeout_ms": timeout_ms}
        )


class Messaging:
    def __init__(self, client: Client):
        self._client = client

    async def publish(
        self,
        topic: str,
        value: Any,
        *,
        key: str | None = None,
        idempotency_key: str | None = None,
    ) -> Any:
        if (
            not isinstance(topic, str)
            or topic in (".", "..")
            or not re.fullmatch(r"[A-Za-z0-9._-]{1,249}", topic)
        ):
            raise ValueError("Invalid Kafka topic")
        if key is not None and not isinstance(key, str):
            raise ValueError("key must be a string")
        if idempotency_key is not None and (
            not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key.encode()) <= 128
        ):
            raise ValueError("idempotency_key must contain 1 to 128 UTF-8 bytes")
        return await self._client._request(
            "messaging.publish",
            {
                "topic": topic,
                "value": value,
                "key": key,
                "idempotency_key": idempotency_key,
            },
        )


class Secrets:
    def __init__(self, client: Client):
        self._client = client

    async def get(self, rid: str, key: str) -> Secret:
        _rid(rid, "secret")
        if not isinstance(key, str) or not 1 <= len(key.encode()) <= 256:
            raise ValueError("A secret key with 1 to 256 UTF-8 bytes is required")
        value = await self._client._request("secrets.get", {"rid": rid, "key": key})
        if not isinstance(value, str):
            raise SdkError("PROTOCOL_ERROR", "Secret response must be a string")
        return Secret(value)
