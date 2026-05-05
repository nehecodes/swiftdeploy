import sys
import time
import subprocess
import urllib.request
from swiftdeploy.output import info, ok, die, err
from swiftdeploy.manifest import load_manifest, get, save_manifest, require, render


def _stack_is_up(app: str) -> bool:
    r = subprocess.run(
        ["docker", "compose", "ps", "--status", "running", "-q", app],
        capture_output=True,
        text=True,
    )
    return r.returncode == 0 and bool(r.stdout.strip())


def cmd_promote(manifest_path: str, mode: str):
    manifest = load_manifest(manifest_path)
    app = require(manifest, "services", "name")

    if not _stack_is_up(app):
        die("stack is not running — run 'swiftdeploy deploy' first")
    manifest = load_manifest(manifest_path)
    current = get(manifest, "services", "env", "MODE") or "stable"

    if current == mode:
        info(f"already in {mode} mode")
        sys.exit(0)

    info(f"{current} → {mode}")

    # Update manifest in-place
    manifest.setdefault("services", {})["env"]["MODE"] = mode
    save_manifest(manifest_path, manifest)
    ok("manifest.yaml updated")

    # Regenerate compose with new MODE env var
    try:
        render(manifest, mode)
    except Exception as e:
        die(f"render failed: {e}")
    ok("docker-compose.yml regenerated")

    # Restart app service only
    app = require(manifest, "services", "name")
    r = subprocess.run(
        ["docker", "compose", "restart", app], capture_output=True, text=True
    )
    if r.returncode != 0:
        die(r.stderr.strip())
    ok(f"service '{app}' restarted")

    # Confirm via /healthz
    port = int(require(manifest, "nginx", "port"))
    healthz = get(manifest, "services", "healthz_path") or "/healthz"
    url = f"http://localhost:{port}{healthz}"

    for _ in range(10):
        try:
            with urllib.request.urlopen(url, timeout=3) as resp:
                if resp.status < 500:
                    ok(f"promote to '{mode}' confirmed")
                    if mode == "canary":
                        info("run 'promote stable' to revert")
                    return
        except Exception:
            pass
        time.sleep(2)

    err(f"service restarted but {url} did not respond")
    sys.exit(1)
