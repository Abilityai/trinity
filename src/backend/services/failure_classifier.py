"""
Shared auth-class failure classifier (SUB-003 decision on the backend; log-labelling on the
scheduler). CANONICAL SOURCE: src/backend/services/failure_classifier.py. The src/scheduler/
copy is a byte-identical vendored mirror — edit the backend copy and regenerate the mirror;
byte-identity is enforced by tests/unit/test_904_sigkill_no_false_auth.py::TestBackendSchedulerParity.
"""

# Substrings that indicate an auth-class subscription failure.
AUTH_INDICATORS = [
    "credit balance",
    "unauthorized",
    "authentication",
    "credentials",
    "forbidden",
    "401",
    "403",
    "oauth",
    "token expired",
    "not authenticated",
]

# #904: unambiguous signal-kill / OOM / timeout markers. When the error
# message contains any of these we short-circuit `is_auth_failure` to
# False even if an AUTH_INDICATOR also happens to match — a SIGKILL is
# evidence the subprocess died from outside, not from a real auth
# response on the wire, and triggering SUB-003 burns the 2h skip-list
# slot for the alternative subscription without fixing anything.
NON_AUTH_KILL_MARKERS = [
    "sigkill",
    "sigterm",
    "sigint",
    "exit code -9",
    "exit code -15",
    "exit code -2",
    "exit code 137",   # 128 + 9 (shell-encoded SIGKILL)
    "exit code 143",   # 128 + 15 (shell-encoded SIGTERM)
    "exit code 130",   # 128 + 2 (shell-encoded SIGINT)
    "terminated by",
    "killed by",
    "out of memory",
    "oom",
    "memory cgroup",
]


def is_auth_failure(error_message: str) -> bool:
    """Return True if `error_message` contains any AUTH_INDICATORS substring
    AND does not also contain an unambiguous signal-kill / OOM / timeout
    marker (#904)."""
    if not error_message:
        return False
    error_lower = error_message.lower()
    if any(marker in error_lower for marker in NON_AUTH_KILL_MARKERS):
        return False
    return any(ind in error_lower for ind in AUTH_INDICATORS)
