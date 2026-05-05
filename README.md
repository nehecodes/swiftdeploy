# swiftdeploy

Manifest-driven nginx + Docker deployment tool. Define your service in `manifest.yaml`, and swiftdeploy generates the configs, brings up the stack, and manages deployments.

---

## Requirements

- Python 3.11+
- [uv](https://github.com/astral-sh/uv)
- Docker

---

## Setup

```bash
git clone https://github.com/nehecodes/swiftdeploy.git
cd swiftdeploy

uv venv .venv
source .venv/bin/activate

uv pip install -e swiftdeploy/
```

---

## manifest.yaml

Place this at the project root and edit to match your service:

```yaml
meta:
  service: my-app
  contact: ops@example.com

services:
  name: my-app
  image: my-app:latest
  port: 8080
  env:
    mode: stable           # stable or canary
  health_path: /healthz

nginx:
  port: 8443
  server_name: localhost
  proxy_timeout: 30s

network:
  name: myapp-net
  driver_type: bridge
```

---

## Subcommands

### `init`
Generates `nginx.conf` and `docker-compose.yaml` from the manifest.

```bash
swiftdeploy init
```

### `validate`
Runs 5 pre-flight checks before deploying. Exits non-zero on any failure.

```bash
swiftdeploy validate
```

| Check | What it verifies |
|---|---|
| manifest.yaml exists and is valid YAML | File exists and parses cleanly |
| All required fields present | No empty or missing fields |
| Docker image exists locally | `docker image inspect` passes |
| Nginx port is not already bound | Port is free on the host |
| nginx.conf is syntactically valid | `nginx -t` or docker equivalent |

### `deploy`
Runs `init` → `validate` → `docker compose up`, then blocks until `/healthz` responds or timeout is reached.

```bash
swiftdeploy deploy
swiftdeploy deploy --timeout 90   # default: 60s
```

### `promote`
Switches deployment mode. Updates `mode` in `manifest.yaml`, regenerates `docker-compose.yaml` with the new `MODE` env var, restarts only the app container, then confirms via `/healthz`.

```bash
swiftdeploy promote canary
swiftdeploy promote stable    # revert
```

### `teardown`
Stops and removes all containers, networks, and volumes.

```bash
swiftdeploy teardown
swiftdeploy teardown --clean   # also deletes nginx.conf + docker-compose.yaml
```

---

## Custom manifest path

All subcommands accept `--manifest` to point at a non-default file:

```bash
swiftdeploy --manifest config/prod.yaml deploy
```