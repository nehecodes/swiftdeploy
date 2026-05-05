from swiftdeploy.manifest import load_manifest, get, render
from swiftdeploy.output import die, ok, info


def cmd_init(manifest_path):
    manifest = load_manifest(manifest_path)
    mode = get(manifest, "services", "env", "MODE") or "stable"
    try:
        render(manifest, mode)
    except Exception as e:
        die(f"render failed: {e}")
    ok("nginx.conf")
    ok("docker-compose.yml")
    info(f"mode: {mode}")
