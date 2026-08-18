# envy

Declarative devboxes. Define an environment once — image, source, sync rules,
lifecycle — and get a sandbox spec out. Environment compilation is
backend-agnostic; deployment and launch integration currently targets Modal.

## Writing a deployable devbox app

```python
import modal
import envy

app = envy.Envy("acme-devboxes", stamp="git-commit-or-release-id")

api = app.env(
    "api",
    base=modal.Image.debian_slim(),
    source=envy.GitSource.github("acme/api", ref="main"),
    build=[envy.apt_install("git", "curl"), envy.pip_install("uv")],  # baked into the image
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

Compose with layers — each brings its own source, steps, and rules:

```python
docs = envy.Layer(
    "docs", source=envy.GitSource.github("acme/docs"), build=[envy.pip_install("mkdocs")]
)
api.include(docs)

# Exporting the Envy declaration freezes every environment, layer, rule, and hook.
modal_app = envy.ModalDeployment(app).export()
```

Deploy the complete app. Modal builds every registered environment image before
publishing the new deployment version:

```bash
modal deploy devboxes.py
```

## Exposing environments over MCP

Install the optional MCP integration with `uv add 'envy[mcp]'`. Build the
server from the same `Envy` object used to declare the environments:

```python
import modal
import envy

app = envy.Envy("acme-devboxes")
api = app.env("api", base=modal.Image.debian_slim())

mcp = app.mcp()
modal_app = envy.ModalDeployment(app).export()


@modal_app.function(image=control_plane_image)
@modal.asgi_app()
def serve():
    return mcp.http_app(stateless_http=True)
```

`control_plane_image` must contain the MCP dependencies and the module that
declares `app`. The MCP server launches environments through Envy's
deployed `launch_<environment>` functions and exposes `create_sandbox`,
`kill_sandbox`, `bash`, `read`, `write`, `edit`, `glob`, and `grep`.
Only sandboxes created through this server are accepted by the tools; ownership
is checked using the reserved `envy.app` and `envy.env` tags.

## Launching it

```python
runner = envy.ModalRunner("acme-devboxes", timeout=60 * 60, idle_timeout=15 * 60)
with runner.session(api) as session:
    api.refresh(session.sandbox)

# Reopen the same sandbox later with its persisted ID.
with runner.session(api, sandbox_id=session.sandbox_id) as session:
    api.refresh(session.sandbox)
```

Sessions detach their local handle on exit without terminating the remote
sandbox, so persist `session.sandbox_id` when it should be reopened later.

Install the Modal backend with `pip install 'envy[modal]'`. For local iteration
without a deployment, `runner.run(api)` still provides the imperative
compile-build-launch path.

## Steps

Named after their `modal.Image` counterparts:

`apt_install` · `pip_install` · `run_commands` · `setenv` · `workdir` · `dockerfile_commands` · `add_local_file` · `add_local_dir` · `run_function`

## Sources

`GitSource` (clone at build, `pull` at refresh) · `LocalSource` (copy at build,
differential filesystem sync at refresh)
