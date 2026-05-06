package swiftdeploy.infra

import rego.v1

# ── Decision ─────────────────────────────────────────────────────────────────
# Every decision carries an explicit allow flag and a list of reasons.
# The CLI must never interpret a missing field as "allow".

default allow := false

decision := {
	"allow": allow,
	"reasons": reasons,
	"domain": "infra",
	"checked_at": input.checked_at,
}

allow if {
	count(violations) == 0
}

# ── Violations ───────────────────────────────────────────────────────────────

violations contains msg if {
	input.disk_free_gb < data.thresholds.min_disk_free_gb
	msg := sprintf(
		"Disk free %.1f GB is below minimum %.1f GB",
		[input.disk_free_gb, data.thresholds.min_disk_free_gb],
	)
}

violations contains msg if {
	input.cpu_load_1m > data.thresholds.max_cpu_load
	msg := sprintf(
		"CPU 1-min load %.2f exceeds maximum %.2f",
		[input.cpu_load_1m, data.thresholds.max_cpu_load],
	)
}

violations contains msg if {
	input.mem_used_pct > data.thresholds.max_mem_used_pct
	msg := sprintf(
		"Memory usage %.1f%% exceeds maximum %.1f%%",
		[input.mem_used_pct, data.thresholds.max_mem_used_pct],
	)
}

# ── Reasons ──────────────────────────────────────────────────────────────────

reasons := violations if {
	count(violations) > 0
}

reasons := {"Host resource checks passed"} if {
	count(violations) == 0
}
