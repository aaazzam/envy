# boxen

Declarative devboxes. Define an environment once — image, source, sync rules,
lifecycle — and get a sandbox spec out. Environment compilation is
backend-agnostic; deployment and launch integration currently targets Modal.

## Writing a deployable devbox app

```python
import modal
import boxen as bx

boxen = bx.Boxen("acme-devboxes", stamp="git-commit-or-release-id")

api = boxen.env(
    "api",
    base=modal.Image.debian_slim(),
    source=bx.GitSource.github("acme/api", ref="main"),
    build=[bx.apt_install("git", "curl"), bx.pip_install("uv")],  # baked into the image
    setup=[bx.run_commands("uv sync")],  # after source, in workdir
    env={"ENV": "dev"},
    ports=[8000],
    resources=bx.Resources(cpu=2, memory=4096),
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
docs = bx.Layer(
    "docs", source=bx.GitSource.github("acme/docs"), build=[bx.pip_install("mkdocs")]
)
api.include(docs)

# Exporting the app freezes every environment, layer, rule, and hook.
app = boxen.app
```

Deploy the complete app. Modal builds every registered environment image before
publishing the new deployment version:

```bash
modal deploy devboxes.py
```

## Launching it

```python
runner = bx.ModalRunner("acme-devboxes", timeout=60 * 60, idle_timeout=15 * 60)
with runner.managed_launch("api") as sb:
    api.refresh(sb)
```

Install the Modal backend with `pip install 'boxen[modal]'`. For local iteration
without a deployment, `runner.run(api)` still provides the imperative
compile-build-launch path.

## Steps

Named after their `modal.Image` counterparts:

`apt_install` · `pip_install` · `run_commands` · `setenv` · `workdir` · `dockerfile_commands` · `add_local_file` · `add_local_dir` · `run_function`

## Sources

`GitSource` (clone at build, `pull` at refresh) · `LocalSource` (copy at build,
differential filesystem sync at refresh)
