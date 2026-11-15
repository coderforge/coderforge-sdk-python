# CoderForge Cloud SDK for Python

Version 1.0.0. MIT, copyright 2026 CoderForge.org Ltd.

Package: `coderforge-cloud-sdk` (wheel and sdist, Python >=3.12).

The framework supplies a request-scoped protocol v1 transport. Standard Functions use the same [asynchronous VFS file pool and safe commit contract](docs/function-vfs-cache.md) across PHP, Python, JavaScript and compiled TypeScript. The trusted host runtime remains in the CloudBuilder project; this repository contains the independently buildable client SDK.

## Build and test

```sh
python -m pip install build==1.2.2.post1 setuptools==75.8.0 wheel==0.45.1
python tools/test.py
python -m build --no-isolation
```

The source is published on `master`, with the exact release identified by tag `v1.0.0`. Generated artifacts belong in the private registry after the source tag is verified; they are excluded from Git. Registry credentials belong in Vault or the package client environment. Never commit credentials, private certificates or local client configuration.

See [API and integration notes](docs/API.md) for capability calls and HTTP adapters. The initial 0.1.0 staging artifacts remain immutable; 1.0.0 is the first stable standalone release with build metadata and release provenance without changing protocol v1.
