"""Native HTTP uses the same capabilities and never retains a warm request token."""

import asyncio
import json
import os
import re
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from coderforge.cloud.sdk import Client, SdkError, WsgiAdapter
from coderforge.cloud.sdk.native import invocation_context


class NativeTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.calls = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                cls.calls.append((dict(self.headers), body))
                if self.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", "/bridge")
                    self.end_headers()
                    self.wfile.write(b"{}")
                    return
                if self.path == "/large":
                    value = b" " * (256 * 1024 + 1)
                elif body["operation"] == "secrets.get":
                    value = json.dumps(
                        {"ok": False, "error": {"code": "FORBIDDEN", "message": "Refused"}}
                    ).encode()
                else:
                    value = json.dumps({"ok": True, "value": body["arguments"]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(value)))
                self.end_headers()
                self.wfile.write(value)

            def log_message(self, *_):
                pass

        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        self.environment = patch.dict(
            os.environ,
            {
                "CODERFORGE_RUNTIME": "mini-x64",
                "CODERFORGE_SDK_URL": self.url + "/bridge",
                "CODERFORGE_INVOCATION_TOKEN": "single-job",
            },
        )
        self.environment.start()
        self.calls.clear()

    def tearDown(self):
        self.environment.stop()

    async def test_wire_and_warm_concurrent_request_tokens(self):
        client = Client()

        async def call(token):
            with invocation_context({"X-CoderForge-Invocation-Token": token}):
                await asyncio.sleep(0)
                return await client.messaging.publish("orders", {"token": token})

        await asyncio.gather(call("first"), call("second"))
        ids = []
        for headers, body in self.calls:
            self.assertEqual(set(body), {"version", "operation", "arguments"})
            self.assertEqual(body["version"], 1)
            self.assertEqual(
                headers["Authorization"], "Bearer " + body["arguments"]["value"]["token"]
            )
            ids.append(headers["X-Coderforge-Request-Id"])
        self.assertEqual(len(set(ids)), 2)
        self.assertTrue(all(re.fullmatch(r"[a-f0-9-]{36}", value) for value in ids))
        await client.functions.invoke("c-ita-x1:s1:fnc:1", {})
        self.assertEqual(self.calls[-1][0]["Authorization"], "Bearer single-job")

    async def test_missing_http_token_does_not_inherit_environment(self):
        with invocation_context({}):
            with self.assertRaisesRegex(SdkError, "invocation token"):
                await Client().messaging.publish("orders", {})
        self.assertEqual(self.calls, [])

    async def test_completed_http_context_cannot_be_reused_by_background_task(self):
        proceed = asyncio.Event()
        client = Client()

        async def background():
            await proceed.wait()
            with self.assertRaises(SdkError):
                await client.messaging.publish("orders", {})

        with invocation_context({"X-CoderForge-Invocation-Token": "finished"}):
            task = asyncio.create_task(background())
        proceed.set()
        await task
        self.assertEqual(self.calls, [])

    async def test_errors_bounds_and_no_redirect_or_retry(self):
        with self.assertRaises(SdkError) as error:
            await Client().secrets.get("c-ita-x1:s1:secret:1", "key")
        self.assertEqual(error.exception.code, "FORBIDDEN")
        for path in ("/redirect", "/large"):
            os.environ["CODERFORGE_SDK_URL"] = self.url + path
            before = len(self.calls)
            with self.assertRaises(SdkError):
                await Client().messaging.publish("orders", {})
            self.assertEqual(len(self.calls), before + 1)

    async def test_explicit_browser_transport_and_invalid_native_config(self):
        os.environ["CODERFORGE_SDK_URL"] = "file:///bad"
        with self.assertRaises(SdkError):
            Client()

        async def bridge(request):
            return {"ok": True, "value": request["version"]}

        self.assertEqual(await Client(bridge).messaging.publish("orders", {}), 1)
        os.environ["CODERFORGE_RUNTIME"] = "browser"
        with self.assertRaises(SdkError) as error:
            await Client().messaging.publish("orders", {})
        self.assertEqual(error.exception.code, "CAPABILITY_UNAVAILABLE")

    async def test_native_wsgi_keeps_lazy_iterator_context(self):
        client = Client()

        def app(environment, start_response):
            start_response("200 OK", [])

            def body():
                loop = asyncio.new_event_loop()
                try:
                    loop.run_until_complete(client.messaging.publish("orders", {}))
                finally:
                    loop.close()
                yield b"ok"

            return body()

        adapter = WsgiAdapter(app)

        def run():
            first = adapter(
                {"wsgi.version": (1, 0), "HTTP_X_CODERFORGE_INVOCATION_TOKEN": "wsgi-first"},
                lambda *_: None,
            )
            second = adapter(
                {"wsgi.version": (1, 0), "HTTP_X_CODERFORGE_INVOCATION_TOKEN": "wsgi-second"},
                lambda *_: None,
            )
            self.assertEqual(list(first), [b"ok"])
            self.assertEqual(list(second), [b"ok"])

        await asyncio.to_thread(run)
        self.assertEqual(
            [value[0]["Authorization"] for value in self.calls],
            ["Bearer wsgi-first", "Bearer wsgi-second"],
        )


if __name__ == "__main__":
    unittest.main()
