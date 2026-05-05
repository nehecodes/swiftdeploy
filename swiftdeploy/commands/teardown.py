import subprocess
import os
from swiftdeploy.output import die, ok


def cmd_teardown(manifest_path: str, clean: bool):
    root = os.path.dirname(os.path.abspath(manifest_path))
    nginx_conf = os.path.join(root, "nginx.conf")
    compose_file = os.path.join(root, "docker-compose.yaml")
    r = subprocess.run(
        ["docker", "compose", "down", "--volumes", "--remove-orphans"],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        die(r.stderr.strip())
    ok("containers, networks, volumes removed")

    if clean:
        for f in [nginx_conf, compose_file]:
            if os.path.exists(f):
                os.remove(f)
                ok(f"deleted {os.path.basename(f)}")

    ok("teardown complete")
