"""Per-LAN stage decisions.

A later stage is not a measurement when an earlier stage failed.
Throughput stays unset so the report can say N/A instead of 0 Mbps.
"""

from __future__ import annotations

from tc_net_cert.core.models import Verdict


def link_failed(detected: bool, physical: Verdict) -> bool:
    return (not detected) or physical == Verdict.FAIL


def failure_text(reasons: list[str], fallback: str) -> str:
    for reason in reasons:
        text = reason.strip()
        if text:
            return text
    return fallback


def iperf_allowed(
    *,
    physical: Verdict,
    address: str,
    dhcp_verdict: Verdict | None,
    static: bool,
) -> bool:
    if physical != Verdict.PASS or not address:
        return False
    if static:
        return True
    return dhcp_verdict == Verdict.PASS


def throughput_text(mbps: float | None) -> str:
    if mbps is None:
        return "N/A"
    return f"{mbps:.0f} Mbps"
