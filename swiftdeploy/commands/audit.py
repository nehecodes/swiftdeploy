"""
swiftdeploy audit
~~~~~~~~~~~~~~~~~
Generates ``audit_report.md`` from ``history.jsonl``.

Sections
--------
1. Summary — total scrapes, time range, modes seen
2. Timeline — table of mode-change events and chaos injections
3. Metrics Trends — min/max/avg of req/s, error rate, P99
4. Policy Violations — every scrape where any domain returned FAIL or ERROR
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from swiftdeploy.manifest import load_manifest, get
from swiftdeploy.output import ok, err, info, die

_HISTORY_FILE = "history.jsonl"
_REPORT_FILE = "audit_report.md"


# ── Data model ────────────────────────────────────────────────────────────────


@dataclass
class ScrapeRecord:
    ts: datetime
    mode: str
    req_per_second: Optional[float]
    error_rate_pct: Optional[float]
    p99_latency_ms: Optional[float]
    sample_count: Optional[int]
    disk_free_gb: Optional[float]
    cpu_load_1m: Optional[float]
    mem_used_pct: Optional[float]
    policy: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, d: dict) -> "ScrapeRecord":
        m = d.get("metrics", {})
        h = d.get("host", {})
        return cls(
            ts=datetime.fromisoformat(d["ts"].rstrip("Z")),
            mode=d.get("mode", "unknown"),
            req_per_second=m.get("req_per_second"),
            error_rate_pct=m.get("error_rate_pct"),
            p99_latency_ms=m.get("p99_latency_ms"),
            sample_count=m.get("sample_count"),
            disk_free_gb=h.get("disk_free_gb"),
            cpu_load_1m=h.get("cpu_load_1m"),
            mem_used_pct=h.get("mem_used_pct"),
            policy=d.get("policy", {}),
            raw=d,
        )

    @property
    def has_violation(self) -> bool:
        return any(v.get("status") in ("FAIL", "ERROR") for v in self.policy.values())


# ── Command ───────────────────────────────────────────────────────────────────


def cmd_audit(manifest_path: str):
    root = os.path.dirname(os.path.abspath(manifest_path))
    history_path = os.path.join(root, _HISTORY_FILE)
    report_path = os.path.join(root, _REPORT_FILE)

    if not os.path.exists(history_path):
        die(
            f"No history file found at {history_path}. "
            "Run 'swiftdeploy status' first to build the audit trail."
        )

    records: list[ScrapeRecord] = []
    parse_errors = 0

    with open(history_path) as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(ScrapeRecord.from_dict(json.loads(line)))
            except Exception as exc:
                err(f"Skipping malformed line {lineno}: {exc}")
                parse_errors += 1

    if not records:
        die("history.jsonl is empty or entirely unreadable.")

    info(f"Loaded {len(records)} records ({parse_errors} skipped)")
    records.sort(key=lambda r: r.ts)

    md = _build_report(records, history_path, parse_errors)

    with open(report_path, "w") as f:
        f.write(md)

    ok(f"Audit report written → {report_path}")


# ── Report builder ────────────────────────────────────────────────────────────


def _build_report(records: list[ScrapeRecord], source: str, parse_errors: int) -> str:
    lines: list[str] = []

    def h(level: int, text: str):
        lines.append(f"\n{'#' * level} {text}\n")

    def p(text: str = ""):
        lines.append(text)

    generated_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    first_ts = records[0].ts.strftime("%Y-%m-%d %H:%M:%S")
    last_ts = records[-1].ts.strftime("%Y-%m-%d %H:%M:%S")
    span_minutes = max((records[-1].ts - records[0].ts).total_seconds() / 60, 0)

    h(1, "swiftdeploy Audit Report")
    p(f"_Generated: {generated_at}_")
    p(f"_Source: `{source}`_")
    p(f"_Parse errors: {parse_errors}_")

    # ── 1. Summary ────────────────────────────────────────────────────────────
    h(2, "Summary")

    modes_seen = sorted({r.mode for r in records})
    violations = [r for r in records if r.has_violation]

    p(f"| Field | Value |")
    p(f"|---|---|")
    p(f"| Total scrapes | {len(records)} |")
    p(f"| Time range | {first_ts} → {last_ts} UTC |")
    p(f"| Span | {span_minutes:.1f} minutes |")
    p(f"| Modes observed | {', '.join(modes_seen)} |")
    p(f"| Policy violations | {len(violations)} |")

    # ── 2. Timeline ───────────────────────────────────────────────────────────
    h(2, "Timeline")
    p("Mode changes and notable events.")
    p()
    p("| Timestamp (UTC) | Event | Mode | Details |")
    p("|---|---|---|---|")

    prev_mode = None
    for r in records:
        events = []
        if r.mode != prev_mode:
            events.append(f"Mode → **{r.mode}**")
            prev_mode = r.mode
        if r.has_violation:
            failing = [
                d for d, v in r.policy.items() if v.get("status") in ("FAIL", "ERROR")
            ]
            events.append(f"⚠ Policy violation ({', '.join(failing)})")
        # Detect chaos: error_rate spike
        if r.error_rate_pct is not None and r.error_rate_pct > 1.0:
            events.append(f"🔥 Error spike: {r.error_rate_pct:.2f}%")
        if r.p99_latency_ms is not None and r.p99_latency_ms > 500:
            events.append(f"🐢 Latency spike: {r.p99_latency_ms:.0f}ms")

        if events:
            ts_str = r.ts.strftime("%Y-%m-%d %H:%M:%S")
            for event in events:
                p(f"| {ts_str} | {event} | {r.mode} | |")

    # ── 3. Metrics Trends ─────────────────────────────────────────────────────
    h(2, "Metrics Trends")

    def _stats(values: list[float]) -> str:
        if not values:
            return "n/a"
        return f"min={min(values):.2f}  avg={sum(values) / len(values):.2f}  max={max(values):.2f}"

    rps_vals = [r.req_per_second for r in records if r.req_per_second is not None]
    err_vals = [r.error_rate_pct for r in records if r.error_rate_pct is not None]
    p99_vals = [r.p99_latency_ms for r in records if r.p99_latency_ms is not None]
    disk_vals = [r.disk_free_gb for r in records if r.disk_free_gb is not None]
    cpu_vals = [r.cpu_load_1m for r in records if r.cpu_load_1m is not None]
    mem_vals = [r.mem_used_pct for r in records if r.mem_used_pct is not None]

    p("| Metric | Statistics |")
    p("|---|---|")
    p(f"| req/s           | {_stats(rps_vals)} |")
    p(f"| error_rate %    | {_stats(err_vals)} |")
    p(f"| P99 latency ms  | {_stats(p99_vals)} |")
    p(f"| disk_free GB    | {_stats(disk_vals)} |")
    p(f"| cpu_load_1m     | {_stats(cpu_vals)} |")
    p(f"| mem_used %      | {_stats(mem_vals)} |")

    # ── 4. Policy Violations ──────────────────────────────────────────────────
    h(2, "Policy Violations")

    if not violations:
        p("_No policy violations recorded in this history file._")
    else:
        p(f"**{len(violations)} violation event(s) detected.**")
        p()
        p("| Timestamp (UTC) | Domain | Status | Reasons |")
        p("|---|---|---|---|")
        for r in violations:
            ts_str = r.ts.strftime("%Y-%m-%d %H:%M:%S")
            for domain, pol in r.policy.items():
                if pol.get("status") in ("FAIL", "ERROR"):
                    reasons_str = "; ".join(pol.get("reasons", []))
                    if len(reasons_str) > 120:
                        reasons_str = reasons_str[:117] + "…"
                    p(f"| {ts_str} | {domain} | {pol['status']} | {reasons_str} |")

    p()
    p("---")
    p(f"_Report generated by swiftdeploy audit — {generated_at}_")

    return "\n".join(lines)
