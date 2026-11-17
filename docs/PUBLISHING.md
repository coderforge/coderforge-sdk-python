# Public source and package releases

Gitea is the primary source repository. Its native push mirror automatically
propagates commits, branches and tags to `https://github.com/coderforge/coderforge-sdk-python`.
A merge into `master` therefore updates GitHub without a workstation push.
The mirror also retries on an eight-hour schedule. Maintain source changes
on Gitea; independent GitHub changes can be overwritten by the mirror.

The stable `v1.0.0` tag is immutable and MIT-licensed. Merging source does not
reuse an existing package version. Increment the package version and create
a new matching stable tag for a subsequent package release.

GitHub Actions publishes new stable tags to PyPI through
`.github/workflows/publish-pypi.yml` and the `pypi` environment. The
PyPI trusted publisher is restricted to this repository, workflow and
environment; no long-lived PyPI token is stored. Build and validation
run without publishing permissions. The publishing job receives only
the reviewed distributions and an ephemeral OpenID Connect identity.

The initial 1.0.0 dispatch downloads the original internal-release
archives from the public GitHub release and verifies their fixed
SHA-256 values. Subsequent tags build their checked-out source and
validate the package name, version and included MIT license.
