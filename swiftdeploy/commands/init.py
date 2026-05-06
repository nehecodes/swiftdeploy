import os
import shutil

from swiftdeploy.manifest import load_manifest, get, render
from swiftdeploy.output import die, ok, info, step

# Canonical policy directory relative to the manifest
_POLICIES_DIR = "policies"
_DEFAULT_DATA_JSON = """{
  "thresholds": {
    "min_disk_free_gb": 10.0,
    "max_cpu_load": 2.0,
    "max_mem_used_pct": 90.0,
    "max_error_rate_pct": 1.0,
    "max_p99_latency_ms": 500.0,
    "min_sample_count": 10
  }
}
"""


def cmd_init(manifest_path: str):
    manifest = load_manifest(manifest_path)
    mode = get(manifest, "services", "env", "MODE") or "stable"

    # ── 1. Render nginx.conf and docker-compose.yaml from templates ──────────
    try:
        render(manifest, mode)
    except Exception as e:
        die(f"render failed: {e}")

    ok("nginx.conf written")
    ok("docker-compose.yaml written")

    # ── 2. Ensure policies/ directory exists with at minimum a data.json ────
    root = os.path.dirname(os.path.abspath(manifest_path))
    policies_dir = os.path.join(root, _POLICIES_DIR)

    if not os.path.isdir(policies_dir):
        os.makedirs(policies_dir, exist_ok=True)
        step(f"Created {_POLICIES_DIR}/")

    # Write default data.json only if none exists — never overwrite user's data
    data_json_path = os.path.join(policies_dir, "data.json")
    if not os.path.exists(data_json_path):
        with open(data_json_path, "w") as f:
            f.write(_DEFAULT_DATA_JSON)
        ok(f"{_POLICIES_DIR}/data.json written (default thresholds)")
    else:
        info(f"{_POLICIES_DIR}/data.json already exists — skipping")

    # Count .rego files so the operator knows what OPA will load
    rego_files = [f for f in os.listdir(policies_dir) if f.endswith(".rego")]
    if rego_files:
        ok(
            f"OPA will load {len(rego_files)} policy file(s): {', '.join(sorted(rego_files))}"
        )
    else:
        info(
            f"No .rego files found in {_POLICIES_DIR}/. "
            "Place your policy files there before deploying."
        )

    info(f"mode: {mode}")
