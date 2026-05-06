import sys
import time
import subprocess
import urllib.request

from swiftdeploy.output import info, ok, die, err, step, warn
from swiftdeploy.manifest import load_manifest, get, save_manifest, require, render
from swiftdeploy.opa import (
    OPAClient,
    OPAUnavailable,
    OPATimeout,
    OPABadResponse,
    OPAMissingDecision,
    OPAError,
    build_canary_input,
)
from swiftdeploy.metrics import MetricsScraper

_OPA_DOMAINS = ["canary"]  # pre-promote checks only canary domain
_METRICS_WINDOW = 30  # seconds of traffic to observe


def cmd_promote(manifest_path: str, mode: str):
    manifest = load_manifest(manifest_path)
    app = require(manifest, "services", "name")

    if not _stack_is_up(app):
        die("stack is not running — run 'swiftdeploy deploy' first")

    current = get(manifest, "services", "env", "MODE") or "stable"
    if current == mode:
        info(f"already in {mode} mode")
        sys.exit(0)

    info(f"Promoting: {current} → {mode}")

    # ── Phase 1: Scrape /metrics for the pre-promote window ──────────────────
    port = int(require(manifest, "nginx", "port"))
    metrics_base = f"http://localhost:{port}"
    scraper = MetricsScraper(base_url=metrics_base, timeout=5.0)

    print()
    step(f"Scraping /metrics over {_METRICS_WINDOW}s window …")
    before = scraper.scrape()

    if before is None:
        warn(
            "/metrics endpoint not reachable — proceeding with zero-traffic snapshot.\n"
            "  OPA will evaluate based on 0 samples; canary min_sample_count may block."
        )
        window = None
    else:
        time.sleep(_METRICS_WINDOW)
        after = scraper.scrape()
        window = scraper.compute_window(before, after) if after else None

    if window:
        info(
            f"Window metrics: "
            f"error_rate={window.error_rate_pct}%  "
            f"p99={window.p99_latency_ms}ms  "
            f"req/s={window.req_per_second}  "
            f"samples={window.sample_count}"
        )
        canary_input = build_canary_input(
            error_rate_pct=window.error_rate_pct,
            p99_latency_ms=window.p99_latency_ms,
            sample_count=window.sample_count,
            window_seconds=_METRICS_WINDOW,
            target_mode=mode,
        )
    else:
        canary_input = build_canary_input(
            error_rate_pct=0.0,
            p99_latency_ms=0.0,
            sample_count=0,
            window_seconds=_METRICS_WINDOW,
            target_mode=mode,
        )

    # ── Phase 2: OPA canary gate ──────────────────────────────────────────────
    print()
    step("Running pre-promote policy checks …")

    opa_url = get(manifest, "opa", "url") or "http://localhost:8181"
    client = OPAClient(base_url=opa_url, timeout=8.0)

    healthy, health_msg = client.healthcheck()
    if not healthy:
        err(f"OPA unavailable: {health_msg}")
        die("Cannot proceed without policy evaluation.")

    results = client.query_all(_OPA_DOMAINS, canary_input)
    blocked = _print_decisions(results, phase="pre-promote")

    if blocked:
        print()
        die("Promotion blocked by policy. Investigate the issues above.")

    ok("All pre-promote policy checks passed")

    # ── Phase 3: Apply the promotion ──────────────────────────────────────────
    print()
    manifest.setdefault("services", {}).setdefault("env", {})["MODE"] = mode
    save_manifest(manifest_path, manifest)
    ok("manifest.yaml updated")

    try:
        render(manifest, mode)
    except Exception as e:
        die(f"render failed: {e}")
    ok("docker-compose.yaml regenerated")

    r = subprocess.run(
        ["docker", "compose", "restart", app], capture_output=True, text=True
    )
    if r.returncode != 0:
        die(r.stderr.strip())
    ok(f"service '{app}' restarted")

    # ── Phase 4: Confirm via /healthz ─────────────────────────────────────────
    healthz = get(manifest, "services", "healthz_path") or "/healthz"
    url = f"http://localhost:{port}{healthz}"

    for attempt in range(10):
        try:
            with urllib.request.urlopen(url, timeout=3) as resp:
                if resp.status < 500:
                    ok(f"promote to '{mode}' confirmed (HTTP {resp.status})")
                    if mode == "canary":
                        info("run 'swiftdeploy promote stable' to roll back")
                    return
        except Exception:
            pass
        time.sleep(2)

    err(f"service restarted but {url} did not respond after 20s")
    sys.exit(1)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _stack_is_up(app: str) -> bool:
    r = subprocess.run(
        ["docker", "compose", "ps", "--status", "running", "-q", app],
        capture_output=True,
        text=True,
    )
    return r.returncode == 0 and bool(r.stdout.strip())


def _print_decisions(results: dict, phase: str) -> bool:
    blocked = False
    for domain, outcome in results.items():
        if isinstance(outcome, OPAUnavailable):
            err(f"[{domain}] OPA is unreachable — {outcome}")
            blocked = True
        elif isinstance(outcome, OPATimeout):
            err(f"[{domain}] OPA timed out — {outcome}")
            blocked = True
        elif isinstance(outcome, OPABadResponse):
            err(f"[{domain}] OPA bad response — {outcome}")
            blocked = True
        elif isinstance(outcome, OPAMissingDecision):
            err(f"[{domain}] Policy missing decision — {outcome}")
            blocked = True
        elif isinstance(outcome, OPAError):
            err(f"[{domain}] Policy error — {outcome}")
            blocked = True
        else:
            if outcome.allow:
                ok(f"[{domain}] ALLOW")
                for reason in outcome.reasons:
                    info(f"  ↳ {reason}")
            else:
                err(f"[{domain}] DENY  ({phase})")
                for reason in outcome.reasons:
                    err(f"  ✗ {reason}")
                blocked = True
    return blocked
