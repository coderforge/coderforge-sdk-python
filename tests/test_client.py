"""Conformance tests also executed unchanged by real Pyodide."""

import base64
import json
from pathlib import Path
import unittest

from coderforge.cloud.sdk import Client, HttpError, Request, Response, Router, SdkError

FIXTURE = json.loads((Path(__file__).parent / "capabilities.json").read_text(encoding="utf-8"))


class ClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_wire_contract_and_redaction(self):
        calls = []

        async def transport(request):
            calls.append(request)
            return {"ok": True, "value": FIXTURE["values"][len(calls) - 1]}

        sdk = Client(transport)
        self.assertEqual(
            await sdk.functions.invoke(
                FIXTURE["function_rid"],
                {"message": "hello", "count": 2},
                timeout_ms=1500,
            ),
            {"processed": True},
        )
        await sdk.messaging.publish(
            "documents.ready",
            {"document": "test.pdf"},
            key="document-1",
            idempotency_key="request-1",
        )
        secret = await sdk.secrets.get(FIXTURE["secret_rid"], "API_KEY")
        self.assertEqual(secret.reveal(), "test-only-secret")
        self.assertNotIn(secret.reveal(), repr(secret))
        with self.assertRaises(TypeError):
            json.dumps(secret)
        self.assertEqual(calls, FIXTURE["requests"])

    async def test_no_transport_fails_closed(self):
        with self.assertRaises(SdkError) as error:
            await Client().functions.invoke(FIXTURE["function_rid"], None)
        self.assertEqual(error.exception.code, "CAPABILITY_UNAVAILABLE")

    async def test_denial_propagates_without_retry(self):
        calls = []

        async def denied(request):
            calls.append(request)
            return FIXTURE["denied"]

        with self.assertRaises(SdkError) as error:
            await Client(denied).messaging.publish("events", None)
        self.assertEqual(error.exception.code, "FORBIDDEN")
        self.assertFalse(error.exception.retryable)
        self.assertEqual(len(calls), 1)

    async def test_malformed_replies_fail(self):
        for reply in [
            None,
            {"ok": 1},
            {"ok": True},
            {"ok": False, "error": {}},
            {"ok": True, "value": []},
        ]:

            async def transport(_):
                return reply

            with self.assertRaises(SdkError):
                await Client(transport).secrets.get(FIXTURE["secret_rid"], "key")

    async def test_invalid_or_large_requests_never_reach_host(self):
        async def forbidden(_):
            self.fail("Invalid request reached the host")

        sdk = Client(forbidden)
        for event in [float("nan"), {"a": object()}, "x" * 262144]:
            with self.assertRaises(ValueError):
                await sdk.functions.invoke(FIXTURE["function_rid"], event)
        for rid in ["fnc:0021", "https://node/functions/21", "../fnc/21"]:
            with self.assertRaises(ValueError):
                await sdk.functions.invoke(rid, {})
        with self.assertRaises(ValueError):
            await sdk.functions.invoke(FIXTURE["function_rid"], {}, timeout_ms=True)
        for topic in [".", "..", "bad/topic", ""]:
            with self.assertRaises(ValueError):
                await sdk.messaging.publish(topic, {})


class RouterTests(unittest.IsolatedAsyncioTestCase):
    def router(self):
        router = Router()

        @router.route("GET", "/users/{id}")
        def user(request, context):
            return Response.json({"id": request.params["id"], "context": context})

        @router.route("POST", "/echo")
        async def echo(request, _):
            return Response.json(await request.json(16))

        return router

    async def test_parameter_context_and_middleware(self):
        router = self.router()

        async def middleware(request, context, following):
            self.assertEqual(request.params, {"id": "A B"})
            response = await following()
            response.headers["x-trace"] = context
            return response

        router.use(middleware)
        response = await router.handle(Request("GET", "/users/A%20B?x=1&x=2"), "trace")
        self.assertEqual(json.loads(response.body), {"id": "A B", "context": "trace"})
        self.assertEqual(response.headers["x-trace"], "trace")

    async def test_not_found_method_head_and_options(self):
        router = self.router()
        self.assertEqual((await router.handle(Request("GET", "/missing"))).status, 404)
        response = await router.handle(Request("POST", "/users/1"))
        self.assertEqual(response.status, 405)
        self.assertEqual(response.headers["allow"], "GET, HEAD, OPTIONS")
        self.assertEqual((await router.handle(Request("OPTIONS", "/users/1"))).status, 204)
        self.assertEqual((await router.handle(Request("HEAD", "/users/1"))).body, b"")

    async def test_json_limit_and_malformed_json(self):
        router = self.router()
        for body, status in [(b"x" * 17, 413), (b"{bad", 400), (b"{}", 200)]:
            self.assertEqual(
                (await router.handle(Request("POST", "/echo", body=body))).status,
                status,
            )

    async def test_stream_is_lazy_and_single_consumer(self):
        count = 0

        async def body():
            nonlocal count
            for chunk in [b"a", b"b", b"c"]:
                count += 1
                yield chunk

        router = Router()
        router.route("POST", "/stream", lambda request, _: Response(request.stream()))
        request = Request("POST", "/stream", body=body())
        response = await router.handle(request)
        self.assertEqual(count, 0)
        iterator = response.body.__aiter__()
        self.assertEqual(await anext(iterator), b"a")
        self.assertEqual(count, 1)
        self.assertEqual(b"".join([chunk async for chunk in iterator]), b"bc")
        with self.assertRaises(HttpError):
            await request.read()

    async def test_binary_event_adapter_is_explicit_and_bounded(self):
        request = Request.from_event(
            {
                "rawPath": "/echo",
                "requestContext": {"http": {"method": "POST"}},
                "body": "AP8=",
                "isBase64Encoded": True,
            }
        )
        self.assertEqual(await request.read(), b"\x00\xff")
        result = await Response(b"\x00\xff").to_event()
        self.assertEqual(base64.b64decode(result["body"]), b"\x00\xff")
        with self.assertRaises(HttpError):
            await Response(b"x" * 10).to_event(5)
        with self.assertRaises(HttpError):
            Request.from_event({"body": "@@@", "isBase64Encoded": True})

    async def test_route_and_middleware_mistakes(self):
        for path in ["relative", "/{id}/{id}", "/{invalid-name}"]:
            with self.assertRaises(ValueError):
                Router().route("GET", path, lambda *_: Response())
        router = self.router()
        with self.assertRaises(ValueError):
            router.route("GET", "/users/{id}", lambda *_: Response())

        async def twice(_request, _context, following):
            await following()
            return await following()

        router.use(twice)
        with self.assertRaises(RuntimeError):
            await router.handle(Request("GET", "/users/1"))


if __name__ == "__main__":
    unittest.main()
