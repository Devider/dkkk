# Langfuse tracing setup for local development

`LangfuseSettings` (`aigw_service/config/langfuse/config.py`) requires
`LANGFUSE_TLS_CERT_FILEPATH`, `LANGFUSE_KEY_FILEPATH`, and `LANGFUSE_CA_BUNDLE_FILEPATH` to point at
existing files whenever both `LOCAL=True` and `LANGFUSE_TRACING_ENABLED=True`
(`config.py:validate_file_path`). With those two flags on and the cert paths blank, startup fails with
`FileNotFoundError`. The validator (`filepath_from_env_validator`, `config/utils.py`) only checks
`os.path.exists`/`os.path.isfile` — no parsing or CA-chain check — so throwaway self-signed files satisfy
it; see "Throwaway certs" below.

## Real client

`LangfuseClient` (`aigw_modules/hub_services/langfuse.py`) wraps the official `langfuse` SDK. It builds a
`langfuse.Langfuse` client plus a `langfuse.langchain.CallbackHandler` (exposed as
`self.callback_handler`, which `api/v1/router.py:get_cb_handler` picks up via
`hasattr(APP_CTX.tracing, "callback_handler")` and injects into `config["callbacks"]`), and a
`health_check()` that calls the `/api/public/health` endpoint plus `Langfuse.auth_check()` on
`on_startup()`.

`on_startup()`/`on_shutdown()` are plain sync methods (the class extends `BaseSyncInterface`) — `context.py`
calls `self.tracing.on_startup()`/`on_shutdown()` without `await`, so they must stay sync to actually run.

## Scheme (http vs https) — resolved entirely inside `LangfuseClient`

`LangfuseSettings.base_url` (`config/langfuse/config.py`) is untouched and still
`f"{self.protocol}://{host}{port}{endpoint}"`, i.e. `https` whenever `LOCAL=True` (`BaseAppSettings.protocol`
in `base_config.py`) — the same shared flag GigaChat/IDP need for their real corporate TLS requirement.
For Langfuse this doesn't fit: a local self-hosted Langfuse (its own `docker compose`) serves plain HTTP
regardless of `LOCAL`, while a real deployment might be http or https depending on infra. Since
`LangfuseClient` (`aigw_modules/hub_services/langfuse.py`) is a local-dev-only stand-in — prod uses the
real client from the private package instead — it resolves this mismatch itself, without touching
`config.py`:

- `_probe_health()` does a lightweight raw `httpx` GET against the health endpoint — no real `Langfuse`
  SDK object involved.
- `_resolve_scheme()` (called first thing in `on_startup()`) probes the scheme it was given; if that
  doesn't answer, it probes the other one (`_ALT_SCHEME`) and switches to it if that one does.
- Only *after* the scheme is settled does `init_client()` build the real `Langfuse(...)` object — this
  order matters: `Langfuse` caches its resources per `public_key` (`LangfuseResourceManager`), so calling
  `init_client()` a second time with a different `httpx_client`/`base_url` does **not** pick up the
  change — a stale, already-closed `httpx.Client` gets reused and every request fails with
  `RuntimeError: Cannot send a request, as the client has been closed.`. That's why the scheme has to be
  settled *before* the one-and-only `init_client()` call, not by retrying it.

So for local dev, `LANGFUSE_HOST=host.docker.internal` with `LOCAL=True` still produces
`https://host.docker.internal:3000...` from settings, but `LangfuseClient` probes it, finds nothing
answering on `https`, falls back to `http`, and proceeds — logged as a warning, not an error.

## Docker networking

If your local Langfuse runs as its own `docker compose` stack (published to the host, e.g.
`0.0.0.0:3000->3000`), the `app` container can't reach it via `LANGFUSE_HOST=localhost` — inside a
container, `localhost` is the container itself. Use `host.docker.internal` instead (works out of the box
on Docker Desktop for Mac/Windows; on Linux add
`extra_hosts: ["host.docker.internal:host-gateway"]` to the `app` service in `docker-compose.yml`).

Also remember: `.env`/volume-mount changes require `docker compose up -d` (recreate), not
`docker compose restart` (which reuses the existing container's env/mounts).

## Throwaway certs (only needed to satisfy `validate_file_path`)

```sh
mkdir -p certs/langfuse-dev
openssl req -x509 -newkey rsa:2048 -nodes -days 365 \
  -keyout certs/langfuse-dev/key.pem \
  -out certs/langfuse-dev/cert.pem \
  -subj "/CN=localhost"
```

In `.env` (relative paths so they resolve the same way locally and in the container):

```
LANGFUSE_HOST=host.docker.internal
LANGFUSE_PORT=3000
LANGFUSE_ENDPOINT=
LANGFUSE_CA_BUNDLE_FILEPATH=certs/langfuse-dev/cert.pem
LANGFUSE_TLS_CERT_FILEPATH=certs/langfuse-dev/cert.pem
LANGFUSE_KEY_FILEPATH=certs/langfuse-dev/key.pem
```

`docker-compose.yml` mounts the same directory into the container so the relative path still resolves
from the container's `/app` working directory:

```yaml
volumes:
  - app_tmp:/tmp
  - ./certs:/app/certs:ro
```
