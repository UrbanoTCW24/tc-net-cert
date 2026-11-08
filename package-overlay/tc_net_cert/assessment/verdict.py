"""Apply YAML acceptance criteria. Numbers are never hardcoded here."""

from __future__ import annotations

from tc_net_cert.core.models import (
    AcceptanceProfile,
    Bottleneck,
    DhcpFacts,
    ErrorDelta,
    LinkFacts,
    PingFacts,
    Verdict,
)


def judge_link(link: LinkFacts, profile: AcceptanceProfile) -> tuple[Verdict, list[str]]:
    reasons: list[str] = []
    failed = False
    if not link.detected:
        return Verdict.FAIL, ["Link detected: no"]
    reasons.append("Link detected: yes")
    if link.speed_mbps is None:
        failed = True
        reasons.append("Speed was not reported")
    elif link.speed_mbps < profile.expected_link_speed:
        failed = True
        reasons.append(f"Speed {link.speed_mbps} Mbps < expected {profile.expected_link_speed} Mbps")
    else:
        reasons.append(f"Link {link.speed_mbps} Mbps")
    duplex = link.duplex.lower()
    expected = profile.expected_duplex.lower()
    if not duplex:
        failed = True
        reasons.append("Duplex was not reported")
    elif duplex != expected:
        failed = True
        reasons.append(f"Duplex {duplex} != expected {expected}")
    else:
        reasons.append(f"Duplex {duplex.upper()}")
    if profile.require_autoneg and link.autoneg not in {"on", "yes"}:
        failed = True
        reasons.append("Auto-negotiation is off")
    elif link.autoneg:
        reasons.append(f"Auto-negotiation {link.autoneg}")
    return (Verdict.FAIL if failed else Verdict.PASS), reasons


def judge_counters(delta: ErrorDelta, profile: AcceptanceProfile) -> tuple[Verdict, list[str]]:
    reasons = [
        f"Errors before: {delta.rx_errors_before + delta.tx_errors_before}",
        f"Errors after: {delta.rx_errors_after + delta.tx_errors_after}",
        f"Delta RX errors: {delta.delta_rx_errors}",
        f"Delta TX errors: {delta.delta_tx_errors}",
        f"Delta drops: {delta.delta_rx_dropped + delta.delta_tx_dropped}",
    ]
    failed = (
        delta.delta_rx_errors > profile.max_rx_errors
        or delta.delta_tx_errors > profile.max_tx_errors
        or (delta.delta_rx_dropped + delta.delta_tx_dropped) > profile.max_drops
    )
    if failed:
        return Verdict.FAIL, reasons
    reasons.append("PASS")
    return Verdict.PASS, reasons


def judge_stability(delta: ErrorDelta, profile: AcceptanceProfile) -> tuple[Verdict, list[str]]:
    flaps = max(delta.link_flaps, 1 if delta.delta_carrier_changes > profile.max_carrier_changes else 0)
    reasons = [f"Carrier changes delta {delta.delta_carrier_changes}", f"LINK_FLAP {flaps}"]
    if delta.link_flaps > 0 or delta.delta_carrier_changes > profile.max_carrier_changes:
        verdict = Verdict.FAIL if profile.link_flap_result == "FAIL" else Verdict.WARNING
        return verdict, reasons
    return Verdict.PASS, reasons


def judge_ping(ping: PingFacts, profile: AcceptanceProfile) -> tuple[Verdict, list[str]]:
    if ping.loss_percent is None:
        return Verdict.FAIL, ["Ping result could not be parsed"]
    reasons = [
        f"{ping.loss_percent:.0f}% loss",
        f"avg {ping.rtt_avg_ms:.1f} ms" if ping.rtt_avg_ms is not None else "avg n/a",
    ]
    if ping.loss_percent > profile.max_ping_loss_percent:
        reasons.append(
            f"Loss {ping.loss_percent:.0f}% > maximum {profile.max_ping_loss_percent:.0f}%"
        )
        return Verdict.FAIL, reasons
    return Verdict.PASS, reasons


def judge_throughput(
    mbps: float | None,
    profile: AcceptanceProfile,
    bottleneck: Bottleneck,
    assessment: str,
) -> tuple[Verdict, list[str]]:
    minimum = profile.minimum_throughput_mbps
    if mbps is None:
        return Verdict.FAIL, ["No throughput measurement"]
    shown = f"Throughput {mbps:.0f} Mbps"
    if mbps + 1e-6 >= minimum:
        return Verdict.PASS, [f"{shown} >= minimum {minimum:.0f} Mbps"]
    reasons = [f"{shown} < minimum {minimum:.0f} Mbps", f"Assessment: {assessment or bottleneck.value}"]
    if bottleneck in {Bottleneck.HOST_LIMIT, Bottleneck.USB_LIMIT, Bottleneck.NIC_LIMIT, Bottleneck.UNKNOWN}:
        reasons.append("DUT is not automatically failed when the limit is not attributed to the DUT")
        return Verdict.INCONCLUSIVE, reasons
    return Verdict.FAIL, reasons


def judge_dhcp(facts: DhcpFacts) -> tuple[Verdict, list[str]]:
    if facts.success and facts.ip:
        reasons = [f"DHCP {facts.ip}/{facts.prefixlen}" if facts.prefixlen is not None else f"DHCP {facts.ip}"]
        if facts.gateway:
            reasons.append(f"Gateway {facts.gateway}")
        if facts.dhcp_server:
            reasons.append(f"DHCP server {facts.dhcp_server}")
        if facts.acquisition_seconds is not None:
            reasons.append(f"Acquisition {facts.acquisition_seconds:.2f}s")
        return Verdict.PASS, reasons
    return Verdict.FAIL, [facts.message or "DHCP failed"]
