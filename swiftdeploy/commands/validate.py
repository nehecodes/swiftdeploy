import os
import sys
import shutil
import socket
import subprocess
import yaml
from swiftdeploy.output import err, ok, check
from swiftdeploy.manifest import get


REQUIRED_FIELDS = [
    ("services", "name"),
    ("services", "image"),
    ("services", "port"),
    ("nginx", "port"),
    ("network", "name"),
    ("network", "driver_type"),
]


def cmd_validate(manifest_path: str) -> bool:
    results = []
    root = os.path.dirname(os.path.abspath(manifest_path))
    nginx_conf = os.path.join(root, "nginx.conf")
    compose_file = os.path.join(root, "docker-compose.yaml")
    # 1 — manifest exists and is valid YAML
    exists = os.path.exists(manifest_path)
    manifest = None
    if exists:
        try:
            with open(manifest_path) as f:
                manifest = yaml.safe_load(f)
            valid_yaml = isinstance(manifest, dict)
        except yaml.YAMLError:
            valid_yaml = False
    else:
        valid_yaml = False
    results.append(
        (
            "manifest.yaml exists and is valid YAML",
            exists and valid_yaml,
            manifest_path if exists and valid_yaml else "not found or invalid",
        )
    )

    # 2 — required fields
    if manifest:
        missing = []
        for keys in REQUIRED_FIELDS:
            val = get(manifest, *keys)
            if val is None or (isinstance(val, str) and not val.strip()):
                missing.append(".".join(keys))
        results.append(
            (
                "All required fields present",
                not missing,
                ", ".join(missing) if missing else "all present",
            )
        )
    else:
        results.append(("All required fields present", False, "skipped"))

    # 3 — Docker image exists locally
    if manifest:
        image = get(manifest, "services", "image") or ""
        if not shutil.which("docker"):
            results.append(("Docker image exists locally", False, "docker not in PATH"))
        else:
            r = subprocess.run(
                ["docker", "image", "inspect", image], capture_output=True
            )
            results.append(("Docker image exists locally", r.returncode == 0, image))
    else:
        results.append(("Docker image exists locally", False, "skipped"))

    # 4 — nginx port free
    if manifest:
        port = int(get(manifest, "nginx", "port") or 0)

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1)
            try:
                s.bind(("0.0.0.0", port))
                port_free = True
            except OSError:
                port_free = False
        results.append(("Nginx port is not bound", port_free, f"port {port}"))
    else:
        results.append(("Nginx port is already bound", False, "skipped"))

    # 5 — nginx.conf syntax
    print(f"Checking nginx file at {root}")
    if not os.path.exists(nginx_conf):
        results.append(("nginx.conf is missing", False, "run 'init' first"))
    elif shutil.which("nginx"):
        r = subprocess.run(
            ["nginx", "-t", "-c", nginx_conf], capture_output=True, text=True
        )
        results.append(
            (
                "nginx.conf is syntactically valid",
                r.returncode == 0,
                "OK" if r.returncode == 0 else r.stderr.strip().split("\n")[0],
            )
        )
    elif shutil.which("docker"):
        r = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "-v",
                f"{nginx_conf}:/etc/nginx/nginx.conf:ro",
                "nginx:latest",
                "nginx",
                "-t",
            ],
            capture_output=True,
            text=True,
        )
        results.append(
            (
                "nginx.conf is syntactically valid",
                r.returncode == 0,
                "OK" if r.returncode == 0 else r.stderr.strip().split("\n")[0],
            )
        )
    else:
        results.append(
            (
                "nginx.conf is syntactically valid",
                True,
                "skipped — nginx/docker not in PATH",
            )
        )

    print()
    all_passed = all(p for _, p, _ in results)
    for label, passed, detail in results:
        check(label, passed, detail)
    print()

    if all_passed:
        ok("All checks passed")
    else:
        err("One or more checks failed")
        sys.exit(1)

    return all_passed
