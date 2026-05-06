package swiftdeploy.canary

import rego.v1

# ── Decision ─────────────────────────────────────────────────────────────────

default allow := false

decision := {
	"allow": allow,
	"reasons": reasons,
	"domain": "canary",
	"checked_at": input.checked_at,
	"window_seconds": input.window_seconds,
	"target_mode": input.target_mode,
}

allow if {
	count(violations) == 0
}

# ── Violations ───────────────────────────────────────────────────────────────

violations contains msg if {
	input.error_rate_pct > data.thresholds.max_error_rate_pct
	msg := sprintf(
		"Error rate %.2f%% exceeds maximum %.2f%% (window: %ds)",
		[input.error_rate_pct, data.thresholds.max_error_rate_pct, input.window_seconds],
	)
}

violations contains msg if {
	input.p99_latency_ms > data.thresholds.max_p99_latency_ms
	msg := sprintf(
		"P99 latency %.0fms exceeds maximum %.0fms (window: %ds)",
		[input.p99_latency_ms, data.thresholds.max_p99_latency_ms, input.window_seconds],
	)
}

violations contains msg if {
	input.sample_count < data.thresholds.min_sample_count
	msg := sprintf(
		"Insufficient traffic sample: %d requests (need at least %d in window)",
		[input.sample_count, data.thresholds.min_sample_count],
	)
}

# ── Reasons ──────────────────────────────────────────────────────────────────

reasons := violations if {
	count(violations) > 0
}

reasons := {msg} if {
	count(violations) == 0
	msg := sprintf(
		"Canary healthy: error rate %.2f%%, P99 %.0fms, %d samples",
		[input.error_rate_pct, input.p99_latency_ms, input.sample_count],
	)
}
