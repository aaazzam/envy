# envy

Declarative devboxes. Define an environment once — image, source, sync rules,
lifecycle — and get a sandbox spec out. Environment compilation is
backend-agnostic; deployment and launch integration currently targets Modal.

## Writing a devbox app

```python
import modal
import envy

app = envy.Envy("acme-devboxes", stamp="git-commit-or-release-id")

api = app.env(
    "api",
    base=modal.Image.debian_slim(),
    source=envy.GitSource.github("acme/api", ref="main"),
    build=[
        envy.apt_install("curl"),
        envy.pip_install("uv"),
    ],  # baked into the image
    setup=[envy.run_commands("uv sync")],  # after source, in workdir
    env={"ENV": "dev"},
    ports=[8000],
    resources=envy.Resources(cpu=2, memory=4096),
)


@api.ready
def boot(sb):
    return sb.exec("uv", "run", "alembic", "upgrade", "head")


@api.on_change("pyproject.toml", "uv.lock")
def deps_changed(sb, changes):
    sb.exec("uv", "sync")


@api.on_change("migrations/*")
def schema_changed(sb, changes):
    sb.exec("uv", "run", "alembic", "upgrade", "head")
```

`GitSource` automatically adds Git to the environment image before build steps
and source checkout, so it does not need to be listed in `build`.

Compose with layers — each brings its own source, steps, and rules:

```python
docs = envy.Layer(
    "docs",
    source=envy.GitSource.github("acme/docs"),
    build=[envy.pip_install("mkdocs")],
)
api.include(docs)
```

## Exposing environments over MCP

Install the optional MCP integration with `uv add 'envy[mcp]'`. Envy supports
FastMCP 3.x and 4.x. FastMCP 4 is currently a prerelease; use
`uv add --prerelease=allow 'fastmcp==4.0.0b3'` when you want to test that line.
Build the server from the same `Envy` object used to declare the environments:

```python
# devboxes.py
import os

import modal
import envy

app = envy.Envy("acme-devboxes")
api = app.env(
    "api",
    base=modal.Image.debian_slim(),
    # The MCP glob and grep tools use ripgrep inside the sandbox.
    build=[envy.apt_install("ripgrep")],
)

# The MCP control plane needs its own image. It must include the MCP
# dependencies and this module, because the server builds environments from
# the declaration at runtime.
control_plane_image = (
    modal.Image.debian_slim()
    .pip_install("envy[mcp]")
    .add_local_python_source("devboxes", copy=True)
)

github_secret = modal.Secret.from_name("acme-github")
modal_app = modal.App(app.name)
mcp = app.mcp(
    git_secret=github_secret,
    modal_app=modal_app,
    github_mcp_url=os.getenv(
        "GITHUB_MCP_URL", "https://api.githubcopilot.com/mcp/"
    ),
)


@modal_app.function(image=control_plane_image, secrets=[github_secret])
@modal.asgi_app()
def serve():
    return mcp.http_app(stateless_http=True)
```

Deploy the MCP control plane with `modal deploy devboxes.py`. Before the first
rebake, Envy builds an environment inline. After that, sandbox creation uses
the latest image ID recorded in a persistent Modal Dict, so image builds stay
off the latency-sensitive sandbox path.

The control-plane image is separate from the environment images: it only runs
the MCP server, while each sandbox uses the `base` image and transforms from
its `app.env(...)` declaration. The server builds and launches those
environments directly and combines Envy's sandbox tools with the tools from
the configured GitHub MCP server. Envy internally selects the complete
`pull_requests` toolset through the `X-MCP-Toolsets` header supported by
GitHub's remote server. The pull-request toolset includes create, list, read,
search, reviews, comments, merge, and update operations. FastMCP's tool search
transform keeps the catalog manageable while leaving the underlying tools
callable. Only sandboxes created through this server are accepted by the Envy
tools; ownership is checked using the reserved `envy.app` and `envy.env` tags.
The sandbox image must provide `bash` and `ripgrep`; the latter is installed
above for the `glob` and `grep` tools.

### Publishing a committed branch

MCP sandbox creation returns a stable logical `sandbox_id`; the underlying
Modal Sandbox may be replaced after an exit. Envy enables Modal exit snapshots
for these sandboxes and restores the workspace from its latest snapshot when a
later tool call finds that the physical sandbox has exited. See Modal's
[exit snapshot documentation](https://modal.com/docs/guide/sandbox-exit-snapshots)
for the feature's current limitations.

To let the server push branches, the control-plane function above must receive
a Modal Secret containing `GITHUB_TOKEN`; the same Secret is passed to Envy so
it can be injected only into the hidden publisher. When a branch-publication
tool such as `create_pull_request` or `update_pull_request` is invoked with the
current `sandbox_id` in MCP request metadata (`envy.sandbox_id`), Envy pushes
the committed workspace branch first. The rest of the pull-request toolset is
forwarded normally. Calls without that metadata are also forwarded directly,
which is useful for already-pushed branches and remote-only GitHub operations.

The publisher refuses dirty trees, detached HEADs, and protected branches; it
never stages or commits. It terminates the canonical sandbox into an exit
snapshot, restores the agent workspace without the Git Secret, and gives a
separate hidden sandbox the snapshot plus the Secret for the push. The Secret
is never placed in the agent-visible sandbox.

## Keeping images warm

When `ModalRunner` is bound to a `modal.App`—as it is through the MCP setup
above—Envy automatically registers an `envy-rebake` Modal cron that rebakes all
environments every 30 minutes. No extra schedule registration is required.

To add a private manual endpoint as well:

```python
runner = envy.ModalRunner(app, modal_app=modal_app)

runner.install_rebake_endpoint(
    modal_app,
    token_secret="acme-devbox-rebake-token",
)
```

The Modal Secret must contain `ENVY_REBAKE_TOKEN`. Call the endpoint with
`POST`, `Authorization: Bearer <token>`, and Modal’s `Modal-Key` and
`Modal-Secret` proxy-auth headers. Send `{"environment": "api"}` as JSON to
rebake one environment; omit the body to rebake all environments. Modal proxy
authentication is enabled by default, and can be disabled with
`requires_proxy_auth=False` when the bearer token alone is enough.
The scheduled function and endpoint can both be deployed with the control plane.

## Launching it

```python
runner = envy.ModalRunner(app, timeout=60 * 60, idle_timeout=15 * 60)
with runner.session(api) as session:
    # New sandboxes refresh their sources before this context is returned.
    print(session.sandbox_id)

# Reopen the same sandbox later with its persisted ID.
with runner.session(api, sandbox_id=session.sandbox_id) as session:
    api.refresh(session.sandbox)
```

Sessions detach their local handle on exit without terminating the remote
sandbox, so persist `session.sandbox_id` when it should be reopened later.

Install the Modal backend with `pip install 'envy[modal]'`. `runner.run(api)`
provides the imperative compile-build-launch path as well.

## Steps

Named after their `modal.Image` counterparts:

`apt_install` · `pip_install` · `run_commands` · `setenv` · `workdir` · `dockerfile_commands` · `add_local_file` · `add_local_dir` · `run_function`

## Sources

`GitSource` (clone at build, `pull` at refresh) · `LocalSource` (copy at build,
differential filesystem sync at refresh)
