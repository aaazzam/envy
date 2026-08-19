# envy

Declarative development environments for remote sandboxes. Define an
environment once—its image, source, setup, hooks, and resources—and compile it
to a sandbox specification. Modal is the supported runtime; the declaration
layer is backend-agnostic.

## Install

Install the core package and the optional integrations you need:

```shell
pip install envy
pip install 'envy[modal]'  # ModalRunner
uv add 'envy[mcp]'         # FastMCP server
```

## Define an environment

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
    ],
    setup=[envy.run_commands("uv sync")],
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

`build` transforms the image. `setup` runs after the source is available.
`ready` hooks run when a sandbox starts, and `on_change` hooks handle refreshed
files. `GitSource` adds Git to the image automatically.

Compose independent sources and setup with layers:

```python
docs = envy.Layer(
    "docs",
    source=envy.GitSource.github("acme/docs"),
    build=[envy.pip_install("mkdocs")],
)
api.include(docs)
```

## Expose environments over MCP

`app.mcp()` creates a FastMCP server with sandbox lifecycle and workspace tools:
`create_sandbox`, `kill_sandbox`, `bash`, `read`, `write`, `edit`, `glob`, and
`grep`. The sandbox image must provide `bash`; install `ripgrep` for `glob` and
`grep`.

The MCP control-plane image must include `envy[mcp]` and the module containing
the declaration. The control plane is separate from the sandbox image. Deploy
it with `modal deploy devboxes.py`:

```python
# devboxes.py
import modal
import envy

app = envy.Envy("acme-devboxes")
api = app.env(
    "api",
    base=modal.Image.debian_slim(),
    build=[envy.apt_install("ripgrep")],
)

control_plane_image = (
    modal.Image.debian_slim()
    .pip_install("envy[mcp]")
    .add_local_python_source("devboxes", copy=True)
)

mcp = app.mcp()
modal_app = modal.App(app.name)


@modal_app.function(image=control_plane_image)
@modal.asgi_app()
def serve():
    return mcp.http_app(stateless_http=True)
```

GitHub's remote MCP tools are optional. Pass `github_mcp_url` to `app.mcp()`
and provide `GITHUB_TOKEN` to the control-plane function when you need them.
For workspace publishing, also pass `git_secret`; Envy then publishes a clean,
committed workspace before a GitHub `create_pull_request` or
`update_pull_request` call that includes `envy.sandbox_id`.

## Launch on Modal

```python
runner = envy.ModalRunner(app, timeout=60 * 60, idle_timeout=15 * 60)
with runner.session(api) as session:
    print(session.sandbox_id)

# Persist the ID to reopen the same sandbox later.
with runner.session(api, sandbox_id=session.sandbox_id) as session:
    api.refresh(session.sandbox)
```

`session` builds, launches, refreshes, and starts a new sandbox. Exiting the
session detaches the local handle without terminating the remote sandbox.
`runner.run(api)` provides the imperative compile/build/launch path.

Optional image caching is available through `runner.rebake()` and the
`install_rebake_schedule()` / `install_rebake_endpoint()` helpers. Normal
launches do not require a rebake.

## Public building blocks

Image steps: `apt_install` · `pip_install` · `run_commands` · `setenv` ·
`workdir` · `dockerfile_commands` · `add_local_file` · `add_local_dir` ·
`run_function`

Sources: `GitSource` (clone at build, pull at refresh) · `LocalSource` (copy at
build, differential filesystem sync at refresh)
