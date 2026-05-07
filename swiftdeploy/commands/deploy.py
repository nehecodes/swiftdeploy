import time
import urllib.request
import urllib.error
import subprocess

from .init import cmd_init
from .validate import cmd_validate
from swiftdeploy.output import ok, info, die, step, err, warn
from swiftdeploy.manifest import get, load_manifest, require
from swiftdeploy.opa import (
    OPAClient,
    OPAError,
    OPAUnavailable,
    OPATimeout,
    OPABadResponse,
    OPAMissingDecision,
    build_infra_input,
)

_OPA_DOMAINS = ["infra"]  # pre-deploy checks only infra domain
_OPA_CONTAINER = "swiftdeploy-opa"


def _ensure_opa(opa_url: str, policies_dir: str):
    """Start the OPA container if it isn't already running."""
    import os, urllib.parse

    port = urllib.parse.urlparse(opa_url).port or 8181

    # Already running?
    r = subprocess.run(
        ["docker", "inspect", "--format", "{{.State.Running}}", _OPA_CONTAINER],
        capture_output=True,
        text=True,
    )
    if r.returncode == 0 and r.stdout.strip() == "true":
        info(f"OPA container '{_OPA_CONTAINER}' already running")
        return

    # Remove any stopped container with the same name
    subprocess.run(["docker", "rm", "-f", _OPA_CONTAINER], capture_output=True)

    abs_policies = os.path.abspath(policies_dir)
    step(f"Starting OPA container (policies: {abs_policies}) …")
    r = subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            _OPA_CONTAINER,
            "-p",
            f"{port}:{port}",
            "-v",
            f"{abs_policies}:/policies",
            "openpolicyagent/opa:latest",
            "run",
            "--server",
            "--addr",
            f"0.0.0.0:{port}",
            "/policies",
        ],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        die(f"Failed to start OPA: {r.stderr.strip()}")

    # Wait up to 15s for OPA to be ready
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{opa_url}/health", timeout=2):
                ok(f"OPA ready at {opa_url}")
                return
        except Exception:
            time.sleep(1)

    die("OPA container started but /health did not respond within 15s")


def cmd_deploy(manifest_path: str, timeout: int):
    # ── Phase 1: Generate configs ────────────────────────────────────────────
    cmd_init(manifest_path)
    cmd_validate(manifest_path)

    manifest = load_manifest(manifest_path)
    opa_url = get(manifest, "opa", "url") or "http://localhost:8181"
    policies_dir = get(manifest, "opa", "policies_dir") or "policies"

    print()
    _ensure_opa(opa_url, policies_dir)

    client = OPAClient(base_url=opa_url, timeout=8.0)

    # ── Phase 2: OPA pre-deploy gate ─────────────────────────────────────────
    print()
    step("Running pre-deploy policy checks …")

    healthy, health_msg = client.healthcheck()
    if not healthy:
        err(f"OPA unavailable: {health_msg}")
        die("Cannot proceed without policy evaluation. Start the OPA container first.")

    input_data = build_infra_input()
    _print_input_summary(input_data)

    results = client.query_all(_OPA_DOMAINS, input_data)
    blocked = _print_decisions(results, phase="pre-deploy")

    if blocked:
        print()
        die("Deployment blocked by policy. Fix the issues above and retry.")

    print()
    ok("All pre-deploy policy checks passed")

    # ── Phase 3: Start the stack ──────────────────────────────────────────────
    print()
    step("docker compose up -d")
    r = subprocess.run(
        ["docker", "compose", "up", "-d", "--remove-orphans"],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        die(r.stderr.strip())
    ok("stack started")

    # ── Phase 4: Health-check loop (retry, never crash on transient errors) ──
    port = int(require(manifest, "nginx", "port"))
    health = get(manifest, "services", "health_path") or "/healthz"
    url = f"http://localhost:{port}{health}"
    info(f"waiting for {url} (timeout: {timeout}s)")

    time.sleep(5)
    deadline = time.time() + timeout
    last_msg = ""

    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as resp:
                if resp.status < 500:
                    ok(f"health check passed (HTTP {resp.status})")
                    ok("deploy complete")
                    return
                msg = f"HTTP {resp.status}"
        except urllib.error.HTTPError as e:
            msg = f"HTTP {e.code}"
        except urllib.error.URLError as e:
            msg = f"connection error: {e.reason}"
        except Exception as e:
            msg = f"unexpected: {e}"

        if msg != last_msg:
            print(f"  · waiting … ({msg})", flush=True)
            last_msg = msg

        time.sleep(2)

    print()
    die(f"health check timed out after {timeout}s — last status: {last_msg}")


# ── Helpers ───────────────────────────────────────────────────────────────────


def _print_input_summary(data: dict):
    info(
        f"Host: disk_free={data['disk_free_gb']}GB  "
        f"cpu_load={data['cpu_load_1m']}  "
        f"mem_used={data['mem_used_pct']}%"
    )


def _print_decisions(results: dict, phase: str) -> bool:
    """Print per-domain decisions.  Returns True if any domain blocked."""
    blocked = False

    for domain, outcome in results.items():
        if isinstance(outcome, OPAUnavailable):
            err(f"[{domain}] OPA is unreachable — {outcome}")
            blocked = True
        elif isinstance(outcome, OPATimeout):
            err(f"[{domain}] OPA timed out — {outcome}")
            blocked = True
        elif isinstance(outcome, OPABadResponse):
            err(f"[{domain}] OPA returned a bad response — {outcome}")
            blocked = True
        elif isinstance(outcome, OPAMissingDecision):
            err(f"[{domain}] Policy returned no decision — {outcome}")
            blocked = True
        elif isinstance(outcome, OPAError):
            err(f"[{domain}] Policy error — {outcome}")
            blocked = True
        else:
            # outcome is a Decision
            decision = outcome
            if decision.allow:
                ok(f"[{domain}] ALLOW")
                for reason in decision.reasons:
                    info(f"  ↳ {reason}")
            else:
                err(f"[{domain}] DENY  ({phase})")
                for reason in decision.reasons:
                    err(f"  ✗ {reason}")
                blocked = True

    return blocked
