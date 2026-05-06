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


def cmd_deploy(manifest_path: str, timeout: int):
    # ── Phase 1: Generate configs ────────────────────────────────────────────
    cmd_init(manifest_path)
    cmd_validate(manifest_path)

    manifest = load_manifest(manifest_path)
    opa_url = get(manifest, "opa", "url") or "http://localhost:8181"
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
