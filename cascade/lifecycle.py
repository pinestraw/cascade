from __future__ import annotations

from enum import Enum


class AgentLifecycleState(str, Enum):
    claimed = "claimed"
    running = "running"
    blocked = "blocked"
    implementation_done = "implementation_done"
    implementing = "implementing"
    preflight_running = "preflight_running"
    preflight_failed = "preflight_failed"
    preflight_passed = "preflight_passed"
    closeout_ready = "closeout_ready"
    closing_out = "closing_out"
    closeout_failed = "closeout_failed"
    closed = "closed"
