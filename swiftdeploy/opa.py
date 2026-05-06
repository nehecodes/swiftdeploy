"""
swiftdeploy.opa
~~~~~~~~~~~~~~~
All Open Policy Agent interaction lives here.

Design contract
---------------
* The CLI NEVER makes allow/deny decisions itself.
* Every decision returned by OPA carries explicit reasoning.
* Every distinct failure mode produces a different human-readable outcome.
* This module raises typed exceptions so callers can handle each case cleanly.
"""

from __future__ import annotations

import datetime
import json
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from typing import Any

# ── Typed exceptions for every distinct failure mode ─────────────────────────


class OPAError(Exception):
    """Base for all OPA-related failures."""


class OPAUnavailable(OPAError):
    """OPA container is not reachable (connection refused / DNS failure)."""


class OPATimeout(OPAError):
    """OPA responded too slowly."""


class OPABadResponse(OPAError):
    """OPA returned an unexpected HTTP status or malformed JSON."""


class OPAMissingDecision(OPAError):
    """OPA returned JSON but the expected decision path was absent.
    This means the policy package/rule name doesn't match what we queried."""


# ── Decision dataclass ────────────────────────────────────────────────────────


@dataclass
class Decision:
    domain: str
    allow: bool
    reasons: list[str]
    raw: dict = field(repr=False, default_factory=dict)

    @classmethod
    def from_opa_result(cls, domain: str, result: dict) -> "Decision":
        """Parse the nested OPA response into a Decision.

        OPA wraps policy output in {"result": <value>}.
        Our policies emit a `decision` object with allow + reasons.
        """
        # result == {"result": {"allow": bool, "reasons": set/list, ...}}
        inner = result.get("result")
        if inner is None:
            raise OPAMissingDecision(
                f"[{domain}] OPA response had no 'result' key — "
                f"check that the policy package and rule name are correct."
            )
        if not isinstance(inner, dict):
            raise OPAMissingDecision(
                f"[{domain}] OPA 'result' was {type(inner).__name__}, expected dict."
            )
        if "allow" not in inner:
            raise OPAMissingDecision(
                f"[{domain}] OPA decision object missing 'allow' field."
            )

        allow = bool(inner["allow"])
        raw_reasons = inner.get("reasons", [])
        # OPA sets come back as lists in JSON
        reasons = list(raw_reasons) if raw_reasons else []

        return cls(domain=domain, allow=allow, reasons=reasons, raw=result)


# ── OPA client ────────────────────────────────────────────────────────────────


class OPAClient:
    """Thin HTTP client for OPA's REST API.

    Parameters
    ----------
    base_url:
        e.g. ``http://localhost:8181``
    timeout:
        Seconds to wait for a response before raising OPATimeout.
    """

    def __init__(self, base_url: str = "http://localhost:8181", timeout: float = 5.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    # ── Public API ────────────────────────────────────────────────────────────

    def query(self, domain: str, input_data: dict[str, Any]) -> Decision:
        """Query a single policy domain and return a Decision.

        ``domain`` maps to a Rego package:
            ``infra``   → ``swiftdeploy/infra/decision``
            ``canary``  → ``swiftdeploy/canary/decision``

        Raises
        ------
        OPAUnavailable  – cannot connect
        OPATimeout      – took too long
        OPABadResponse  – non-200 or non-JSON body
        OPAMissingDecision – JSON OK but expected fields absent
        """
        path = f"/v1/data/swiftdeploy/{domain}/decision"
        url = self.base_url + path
        payload = json.dumps({"input": input_data}).encode()

        try:
            req = urllib.request.Request(
                url,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read()
                status = resp.status
        except urllib.error.URLError as exc:
            reason = str(exc.reason)
            if "timed out" in reason.lower() or isinstance(exc.reason, TimeoutError):
                raise OPATimeout(
                    f"[{domain}] OPA did not respond within {self.timeout}s. "
                    f"Is the OPA container healthy?"
                ) from exc
            raise OPAUnavailable(
                f"[{domain}] Cannot reach OPA at {self.base_url}. "
                f"Reason: {reason}. "
                f"Ensure the OPA container is running ('docker compose ps')."
            ) from exc
        except TimeoutError as exc:
            raise OPATimeout(
                f"[{domain}] Connection to OPA timed out after {self.timeout}s."
            ) from exc

        if status != 200:
            raise OPABadResponse(
                f"[{domain}] OPA returned HTTP {status} for {path}. "
                f"Body: {body[:200].decode(errors='replace')}"
            )

        try:
            result = json.loads(body)
        except json.JSONDecodeError as exc:
            raise OPABadResponse(
                f"[{domain}] OPA response was not valid JSON: {exc}. "
                f"Body snippet: {body[:200].decode(errors='replace')}"
            ) from exc

        return Decision.from_opa_result(domain, result)

    def query_all(
        self, domains: list[str], input_data: dict[str, Any]
    ) -> dict[str, Decision | OPAError]:
        """Query multiple domains and return a mapping of domain → Decision or error.

        Never raises — each domain either yields a Decision or a typed error.
        Callers decide how to surface errors.
        """
        results: dict[str, Decision | OPAError] = {}
        for domain in domains:
            try:
                results[domain] = self.query(domain, input_data)
            except OPAError as exc:
                results[domain] = exc
        return results

    def healthcheck(self) -> tuple[bool, str]:
        """Probe OPA's /health endpoint.

        Returns (True, "ok") or (False, human-readable reason).
        Never raises.
        """
        url = self.base_url + "/health"
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=3.0) as resp:
                if resp.status == 200:
                    return True, "ok"
                return False, f"OPA /health returned HTTP {resp.status}"
        except urllib.error.URLError as exc:
            return False, f"OPA unreachable: {exc.reason}"
        except Exception as exc:  # noqa: BLE001
            return False, f"OPA healthcheck failed: {exc}"


# ── Input builders ────────────────────────────────────────────────────────────


def build_infra_input() -> dict[str, Any]:
    """Collect host resource stats for the infra policy domain.

    This is the canonical pre-deploy input shape.  All keys are required by
    infra.rego — adding keys here is safe; removing them will break the policy.
    """
    import shutil
    import os

    disk = shutil.disk_usage("/")
    disk_free_gb = disk.free / (1024**3)

    # CPU load average (1-min)
    try:
        cpu_load_1m = os.getloadavg()[0]
    except AttributeError:
        # Windows does not support getloadavg
        cpu_load_1m = 0.0

    # Memory
    mem_total, mem_used_pct = _read_mem()

    return {
        "check_type": "pre_deploy",
        "disk_free_gb": round(disk_free_gb, 2),
        "cpu_load_1m": round(cpu_load_1m, 4),
        "mem_used_pct": round(mem_used_pct, 2),
        "mem_total_gb": round(mem_total / (1024**3), 2),
        "checked_at": _utcnow(),
    }


def build_canary_input(
    error_rate_pct: float,
    p99_latency_ms: float,
    sample_count: int,
    window_seconds: int,
    target_mode: str,
) -> dict[str, Any]:
    """Build the pre-promote input for the canary policy domain.

    Deliberately separate from infra input — a promote check is a different
    question and must carry different context.
    """
    return {
        "check_type": "pre_promote",
        "target_mode": target_mode,
        "error_rate_pct": round(error_rate_pct, 4),
        "p99_latency_ms": round(p99_latency_ms, 2),
        "sample_count": sample_count,
        "window_seconds": window_seconds,
        "checked_at": _utcnow(),
    }


# ── Helpers ───────────────────────────────────────────────────────────────────


def _utcnow() -> str:
    return datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_mem() -> tuple[float, float]:
    """Return (total_bytes, used_pct).  Falls back gracefully on non-Linux."""
    try:
        with open("/proc/meminfo") as f:
            lines = f.readlines()
        info: dict[str, int] = {}
        for line in lines:
            parts = line.split()
            if len(parts) >= 2:
                info[parts[0].rstrip(":")] = int(parts[1])
        total = info.get("MemTotal", 0) * 1024
        available = info.get("MemAvailable", 0) * 1024
        if total == 0:
            return 0.0, 0.0
        used_pct = (1 - available / total) * 100
        return total, used_pct
    except FileNotFoundError:
        # macOS / Windows fallback — best effort via `vm_stat` or skip
        return 0.0, 0.0
