"""
swiftdeploy status
~~~~~~~~~~~~~~~~~~
Live-refreshing terminal dashboard.

  ┌──────────────────────────────────────────────────────────────┐
  │  swiftdeploy status  │  myapp  │  mode: canary              │
  ├──────────────────────────────────────────────────────────────┤
  │  req/s   │  error %  │  P99 ms  │  scrape interval          │
  │  23.4    │  0.12%    │  84ms    │  every 10s                │
  ├──────────────────────────────────────────────────────────────┤
  │  Policy Compliance                                           │
  │  [PASS]  infra  — Host resource checks passed               │
  │  [PASS]  canary — Canary healthy …                          │
  └──────────────────────────────────────────────────────────────┘

Appends each scrape to history.jsonl for the audit trail.
"""

from __future__ import annotations

import json
import os
import sys
import time
import datetime
import signal

from swiftdeploy.manifest import load_manifest, get, require
from swiftdeploy.output import ok, err, info, step
from swiftdeploy.metrics import MetricsScraper, Snapshot
from swiftdeploy.opa import (
    OPAClient,
    build_infra_input,
    build_canary_input,
    Decision,
    OPAError,
)

_HISTORY_FILE = "history.jsonl"
_SCRAPE_INTERVAL = 10  # seconds between dashboard refreshes


def cmd_status(manifest_path: str):
    manifest = load_manifest(manifest_path)
    app_name = get(manifest, "services", "name") or "app"
    port = int(require(manifest, "nginx", "port"))
    opa_url = get(manifest, "opa", "url") or "http://localhost:8181"

    metrics_base = f"http://localhost:{port}"
    scraper = MetricsScraper(base_url=metrics_base, timeout=5.0)
    opa = OPAClient(base_url=opa_url, timeout=5.0)

    history_path = os.path.join(
        os.path.dirname(os.path.abspath(manifest_path)), _HISTORY_FILE
    )

    # Graceful Ctrl-C
    running = [True]

    def _stop(sig, frame):
        running[0] = False

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    print(
        f"\n  swiftdeploy status  │  {app_name}  │  refreshing every {_SCRAPE_INTERVAL}s"
    )
    print(f"  Writing audit trail → {history_path}")
    print("  Press Ctrl-C to exit\n")

    prev_snapshot: Snapshot | None = None

    while running[0]:
        now = datetime.datetime.utcnow()
        mode = get(manifest, "services", "env", "MODE") or "stable"

        # ── Metrics ──────────────────────────────────────────────────────────
        current = scraper.scrape()
        if current is not None and prev_snapshot is not None:
            wm = scraper.compute_window(prev_snapshot, current)
        else:
            wm = None

        # ── Policy compliance ─────────────────────────────────────────────────
        infra_input = build_infra_input()
        if wm:
            canary_input = build_canary_input(
                error_rate_pct=wm.error_rate_pct,
                p99_latency_ms=wm.p99_latency_ms,
                sample_count=wm.sample_count,
                window_seconds=int(wm.window_seconds),
                target_mode=mode,
            )
        else:
            canary_input = build_canary_input(
                error_rate_pct=0.0,
                p99_latency_ms=0.0,
                sample_count=0,
                window_seconds=_SCRAPE_INTERVAL,
                target_mode=mode,
            )

        infra_result = _safe_query(opa, "infra", infra_input)
        canary_result = _safe_query(opa, "canary", canary_input)

        # ── Build audit record ────────────────────────────────────────────────
        record = {
            "ts": now.isoformat() + "Z",
            "mode": mode,
            "metrics": {
                "req_per_second": wm.req_per_second if wm else None,
                "error_rate_pct": wm.error_rate_pct if wm else None,
                "p99_latency_ms": wm.p99_latency_ms if wm else None,
                "sample_count": wm.sample_count if wm else None,
            },
            "policy": {
                "infra": _decision_to_dict(infra_result),
                "canary": _decision_to_dict(canary_result),
            },
            "host": {
                "disk_free_gb": infra_input.get("disk_free_gb"),
                "cpu_load_1m": infra_input.get("cpu_load_1m"),
                "mem_used_pct": infra_input.get("mem_used_pct"),
            },
        }

        with open(history_path, "a") as f:
            f.write(json.dumps(record) + "\n")

        # ── Render dashboard ──────────────────────────────────────────────────
        _clear()
        _render_dashboard(record, app_name, mode, now)

        prev_snapshot = current
        # Sleep in small increments so Ctrl-C is responsive
        for _ in range(_SCRAPE_INTERVAL * 2):
            if not running[0]:
                break
            time.sleep(0.5)

    print("\n  Exiting status dashboard.")


# ── Rendering ─────────────────────────────────────────────────────────────────


def _render_dashboard(record: dict, app: str, mode: str, ts: datetime.datetime):
    W = 68
    bar = "─" * W

    def row(label: str, value: str):
        line = f"  {label:<22}{value}"
        print(line)

    print(f"\n  ┌{bar}┐")
    print(f"  │  {'swiftdeploy status':^20}  │  {app}  │  mode: {mode:<14}│")
    print(f"  ├{bar}┤")
    print(f"  │  {'Metrics':^{W}}│")
    print(f"  ├{bar}┤")

    m = record["metrics"]
    rps = f"{m['req_per_second']:.1f}" if m["req_per_second"] is not None else "n/a"
    errate = f"{m['error_rate_pct']:.2f}%" if m["error_rate_pct"] is not None else "n/a"
    p99 = f"{m['p99_latency_ms']:.0f}ms" if m["p99_latency_ms"] is not None else "n/a"

    print(f"  │  req/s: {rps:<10}  error%: {errate:<10}  P99: {p99:<10}{'':>9}│")

    h = record["host"]
    disk = f"{h['disk_free_gb']}GB" if h["disk_free_gb"] is not None else "n/a"
    cpu = f"{h['cpu_load_1m']}" if h["cpu_load_1m"] is not None else "n/a"
    mem = f"{h['mem_used_pct']}%" if h["mem_used_pct"] is not None else "n/a"
    print(f"  │  disk_free: {disk:<8}  cpu_load: {cpu:<8}  mem_used: {mem:<7}│")

    print(f"  ├{bar}┤")
    print(f"  │  {'Policy Compliance':^{W}}│")
    print(f"  ├{bar}┤")

    for domain, pol in record["policy"].items():
        status = pol.get("status", "ERROR")
        symbol = (
            "\033[32m✔ PASS\033[0m" if status == "PASS" else "\033[31m✘ FAIL\033[0m"
        )
        reasons = pol.get("reasons", [])
        first = reasons[0] if reasons else ""
        # Truncate long reason lines for display
        if len(first) > W - 20:
            first = first[: W - 23] + "…"
        print(f"  │  {symbol}  [{domain}]  {first:<{W - 22}}│")
        for r in reasons[1:]:
            if len(r) > W - 12:
                r = r[: W - 15] + "…"
            print(f"  │  {'':10}  {r:<{W - 14}}│")

    print(f"  ├{bar}┤")
    print(f"  │  Last scrape: {ts.strftime('%Y-%m-%d %H:%M:%S')} UTC{'':<{W - 38}}│")
    print(f"  └{bar}┘\n")


def _clear():
    if sys.platform == "win32":
        os.system("cls")
    else:
        print("\033[H\033[J", end="", flush=True)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _safe_query(client: OPAClient, domain: str, input_data: dict) -> dict:
    """Query OPA and return a serialisable dict regardless of outcome."""
    try:
        decision = client.query(domain, input_data)
        return {
            "status": "PASS" if decision.allow else "FAIL",
            "allow": decision.allow,
            "reasons": decision.reasons,
        }
    except OPAError as exc:
        return {
            "status": "ERROR",
            "allow": False,
            "reasons": [str(exc)],
        }


def _decision_to_dict(d: dict) -> dict:
    return d
