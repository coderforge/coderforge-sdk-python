# CoderForge Cloud SDK for Python and Pyodide

Standard Functions share the framework's [asynchronous VFS file pool](function-vfs-cache.md)
with PHP, JavaScript and compiled TypeScript. Warm non-writing requests do not
wait on VFS; source updates invalidate application modules between invocations.
Writes retain version-checked, acknowledged atomic commits. See the contract for freshness and conflicts.

Version 1.0.0 API; protocol v1. MIT, copyright 2026 CoderForge.org Ltd.

Distribution: `coderforge-cloud-sdk`. Import: `coderforge.cloud.sdk`.
Python >=3.12, pure Python, no runtime dependencies. Namespace packages let
other CoderForge libraries coexist under `coderforge`.

```python
from coderforge.cloud.sdk import Client, Request, Response, Router

app = Router()

@app.route("GET", "/health")
def health(request, context):
    return Response.json({"status": "ok"})

async def handler(event, context=None):
    response = await app.handle(Request.from_event(event), context)
    return await response.to_event()
```

The JSON event adapter buffers a bounded body. For native streaming, pass an
async byte iterable into Request and consume Response.body directly. This SDK
does not change the deployed Function event ABI or install a runtime adapter.

For capabilities, explicitly construct `Client(host_transport)` where
`host_transport(request)` is an async callback implementing protocol v1:

```python
result = await sdk.functions.invoke("c-ita-x1:s1:fnc:0021", {"action": "inspect"})
receipt = await sdk.messaging.publish("documents.ready", {"id": "demo"}, key="demo")
secret = await sdk.secrets.get("c-ita-x1:s1:secret:0001", "API_KEY")
value = secret.reveal()
```

`sdk` above is a Client supplied with the host transport. CloudBuilder Manager
1.2.x and protocol 2 inject context["sdk_bridge"], adapted with json_transport.
Without one, Client fails with
`CAPABILITY_UNAVAILABLE` outside a configured mini-x64 guest. Authorization belongs to
the host/Manager. No automatic retries. Secret values are visible in worker
memory even though str/repr are redacted. Keep infrastructure credentials on
the server. See the source SDK repository's protocol.md and root README for
integration requirements and the local test/build commands.

For a JS/WASM callback exchanging JSON text, adapt it explicitly with
`Client(json_transport(host_bridge))` (`json_transport` is exported by the SDK).
The real Pyodide test exercises this asynchronous language boundary.

For a standard Flask application, export a Function handler returning
`WsgiAdapter(app)(event, context)`. WsgiAdapter implements PEP 3333 and
converts the bounded browser event body into WSGI input. Package Flask and its
pure-Python dependencies in the ZIP. See the Terraform SDK acceptance example.

## Native mini-x64 guests

The same `Client()` automatically uses the guest bridge when the runtime sets
`CODERFORGE_RUNTIME=mini-x64` and `CODERFORGE_SDK_URL`. The endpoint must be an
HTTP or HTTPS URL without credentials or a fragment. Invalid native settings
fail closed. Explicit browser callbacks continue to work unchanged.

Every call POSTs the existing protocol-v1 JSON body, with
`Authorization: Bearer <invocation token>` and a fresh UUID in
`X-CoderForge-Request-Id`. Replies remain `{ok,value}` or `{ok,error}`; requests
and replies are limited to 256 KiB. Calls are never retried or redirected.
Only the runtime supplies the bridge URL and short-lived invocation token;
platform credentials never belong in application configuration.

For HTTP applications, the runtime injects `X-CoderForge-Invocation-Token` into
each request. Flask's existing request context is recognized automatically.
For other WSGI applications, serve `WsgiAdapter(app)` as the WSGI application;
it also accepts native `(environ, start_response)` calls and preserves streaming
iteration in an isolated request context. A warm `Client()` can be shared:
it captures the current request token on each operation. Missing HTTP tokens
never inherit process environment credentials. Only non-HTTP single-process
jobs use `CODERFORGE_INVOCATION_TOKEN` as their token source.

Native HTTP applications start their usual server from the bundle's `boot.sh`,
listening on `PORT` (default 8080). The runtime forwards the original HTTP
request; a JSON Function invocation is an HTTP POST containing its event
envelope. The SDK does not start a server or change the application's routes.

Local native transport checks use only a loopback stdlib HTTP fixture:
`PYTHONPATH=python/src python -m unittest discover -s python/tests`.
