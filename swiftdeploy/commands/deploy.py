import time
import urllib.request
import urllib.error
import subprocess
from .init import cmd_init
from .validate import cmd_validate
from swiftdeploy.output import ok, info, die, step
from swiftdeploy.manifest import get, load_manifest, require


def cmd_deploy(manifest_path: str, timeout: int):
    cmd_init(manifest_path)
    cmd_validate(manifest_path)

    step("docker compose up -d")
    r = subprocess.run(
        ["docker", "compose", "up", "-d", "--remove-orphans"],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        die(r.stderr.strip())
    ok("stack started")
    manifest = load_manifest(manifest_path)
    port = int(require(manifest, "nginx", "port"))
    health = get(manifest, "services", "health_path") or "/healthz"
    url = f"http://localhost:{port}{health}"

    info(f"waiting for {url} (timeout: {timeout}s)")
    time.sleep(5)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as resp:
                if resp.status < 500:
                    ok(f"health check passed (HTTP {resp.status})")
                    ok("deploy complete")
                    return
                else:
                    print(f" (Received HTTP {resp.status})")
        except urllib.error.HTTPError as e:
            print(f"\r  · App returned HTTP {e.code} for {health}")
            raise
        except urllib.error.URLError as e:
            print(f"\r  · Connection error: {e.reason}")
            raise
        except Exception as e:
            print(f"\r  · Unexpected error: {e}")
            raise
        time.sleep(2)

    print()
    die(f"health check timed out after {timeout}s")
