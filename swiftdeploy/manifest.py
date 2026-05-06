import os
import yaml
from typing import Any
from .output import die
from jinja2 import Environment, FileSystemLoader

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATES = os.path.join(ROOT, "templates")
NGINX_CONF = os.path.join(ROOT, "nginx.conf")
COMPOSE_FILE = os.path.join(ROOT, "docker-compose.yaml")


def load_manifest(path: str) -> dict[str, Any]:
    if not os.path.exists(path):
        die(f"manifest not found: {path}")
    try:
        with open(path) as fh:
            data = yaml.safe_load(fh)
    except yaml.YAMLError as e:
        die(f"invalid YAML: {e}")
    if not isinstance(data, dict):
        die("manifest must be a YAML mapping")
    return data


def get(data: dict, *keys, default=None):
    node = data
    for k in keys:
        if not isinstance(node, dict):
            return default
        node = node.get(k, default)
    return node


def require(data: dict, *keys) -> Any:
    val = get(data, *keys)
    if val is None or (isinstance(val, str) and not val.strip()):
        die(f"required field missing or empty: {'.'.join(keys)}")
    return val


def save_manifest(path: str, data: dict):
    with open(path, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)


def render(manifest: dict, mode: str):
    """Render nginx.conf and docker-compose.yaml from Jinja2 templates.

    The OPA context block exposes all OPA-related variables to the template.
    Thresholds and tunables default here; operators override via manifest.yaml.
    """
    env = Environment(
        loader=FileSystemLoader(TEMPLATES),
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )

    ctx = dict(
        services={
            "name": require(manifest, "services", "name"),
            "image": require(manifest, "services", "image"),
            "port": int(require(manifest, "services", "port")),
            "restart": get(manifest, "services", "restart") or "unless-stopped",
            "env": {
                **(get(manifest, "services", "env") or {}),
                "MODE": mode,
                "APP_VERSION": get(manifest, "services", "version") or "1.0.0",
                "APP_PORT": str(require(manifest, "services", "port")),
            },
            "volumes": get(manifest, "services", "volumes") or [],
        },
        nginx={
            "image": get(manifest, "nginx", "image") or "nginx:latest",
            "port": int(require(manifest, "nginx", "port")),
            "server_name": get(manifest, "nginx", "server_name") or "localhost",
            "restart": get(manifest, "nginx", "restart") or "unless-stopped",
            "proxy_timeout": get(manifest, "nginx", "proxy_timeout") or "30s",
            "worker_processes": get(manifest, "nginx", "worker_processes") or "auto",
            "worker_connections": get(manifest, "nginx", "worker_connections") or 1024,
            "keepalive_timeout": get(manifest, "nginx", "keepalive_timeout") or 65,
            "log_level": get(manifest, "nginx", "log_level") or "warn",
            "log_format": get(manifest, "nginx", "log_format"),
            "volumes": get(manifest, "services", "volumes") or [],
        },
        # ── OPA sidecar configuration ────────────────────────────────────────
        # All values are safe defaults; operators may override via manifest.yaml
        # under the `opa:` key.  The CLI reads `opa.url` for direct queries.
        opa={
            "image": get(manifest, "opa", "image") or "openpolicyagent/opa:latest",
            "port": int(get(manifest, "opa", "port") or 8181),
            "log_level": get(manifest, "opa", "log_level") or "error",
            "restart": get(manifest, "opa", "restart") or "unless-stopped",
            # URL the CLI uses to reach OPA (host-bound loopback port)
            "url": get(manifest, "opa", "url") or "http://localhost:8181",
        },
        network={
            "name": require(manifest, "network", "name"),
            "driver": require(manifest, "network", "driver_type"),
        },
        volumes=get(manifest, "volumes") or [],
        meta={
            "service": (
                get(manifest, "meta", "service") or get(manifest, "services", "name")
            ),
            "contact": get(manifest, "meta", "contact") or "nehemiah.dev",
        },
        mode=mode,
    )

    with open(NGINX_CONF, "w") as f:
        f.write(env.get_template("nginx.conf.j2").render(**ctx))

    with open(COMPOSE_FILE, "w") as f:
        f.write(env.get_template("docker-compose.yaml.j2").render(**ctx))
