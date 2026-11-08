"""Roll raw flows into per-port and global verdicts."""

from __future__ import annotations

from tc_net_cert.core.models import (
    AcceptanceProfile,
    CertificationReport,
    Endpoint,
    FlowResult,
    PortReport,
    Verdict,
    worst_verdict,
)
from tc_net_cert.reporting.render import DISCLAIMER
from tc_net_cert.tests.matrix import build_matrix
from tc_net_cert.version import EMTA_SCOPE_EXCLUSIONS


def assemble_report(
    report: CertificationReport,
    endpoints: list[Endpoint],
    flows: list[FlowResult],
    profile: AcceptanceProfile,
) -> CertificationReport:
    names = [item.logical_name for item in endpoints]
    report.flows = flows
    report.matrix = build_matrix(names, flows)
    report.ports = [_port_report(endpoint, flows) for endpoint in endpoints]
    report.simultaneous_verdict = _kind_verdict(flows, "simultaneous")
    report.bidirectional_verdict = _kind_verdict(flows, "bidirectional")
    report.errors_verdict = _error_verdict(flows, profile)
    report.stability_verdict = _stability_verdict(flows, profile)
    exclusions = list(report.scope_exclusions)
    if report.dut.type == "emta":
        for item in EMTA_SCOPE_EXCLUSIONS:
            if item not in exclusions:
                exclusions.append(item)
    report.scope_exclusions = exclusions
    report.disclaimer = DISCLAIMER
    pieces = [port.verdict for port in report.ports]
    pieces.append(report.simultaneous_verdict)
    pieces.append(report.bidirectional_verdict)
    pieces.append(report.errors_verdict)
    pieces.append(report.stability_verdict)
    report.global_verdict = worst_verdict(pieces)
    report.global_reasons = _global_reasons(report)
    return report


def _port_report(endpoint: Endpoint, flows: list[FlowResult]) -> PortReport:
    involved = [
        flow
        for flow in flows
        if endpoint.logical_name in {flow.source, flow.destination} and flow.kind != "throughput-single"
    ]
    cross = [flow for flow in involved if flow.kind == "connectivity" and flow.source == endpoint.logical_name]
    throughput = [flow for flow in involved if flow.kind == "throughput" and flow.source == endpoint.logical_name]
    averages = [flow.mbps_avg for flow in throughput if flow.mbps_avg is not None]
    deltas = [flow.error_delta for flow in involved if flow.error_delta is not None]
    rx = sum(item.delta_rx_errors for item in deltas)
    tx = sum(item.delta_tx_errors for item in deltas)
    drops = sum(item.delta_rx_dropped + item.delta_tx_dropped for item in deltas)
    flaps = sum(item.link_flaps for item in deltas)
    reasons: list[str] = list(endpoint.physical_reasons)
    parts: list[Verdict] = [endpoint.physical_verdict]
    if endpoint.dhcp is not None:
        parts.append(endpoint.dhcp.verdict)
        reasons.extend(endpoint.dhcp.reasons)
    if endpoint.gateway_result is not None:
        parts.append(endpoint.gateway_result.verdict)
        reasons.extend(endpoint.gateway_result.reasons)
    if (
        endpoint.physical_verdict == Verdict.PASS
        and endpoint.dhcp is not None
        and endpoint.dhcp.verdict == Verdict.PASS
        and endpoint.iperf_state in {Verdict.SKIPPED, Verdict.NOT_RUN}
        and endpoint.tx_mbps is None
        and endpoint.rx_mbps is None
        and endpoint.failure_reason
    ):
        parts.append(Verdict.FAIL)
    if cross:
        parts.append(worst_verdict([flow.verdict for flow in cross]))
    if throughput:
        parts.append(worst_verdict([flow.verdict for flow in throughput]))
    receiving = [
        flow
        for flow in involved
        if flow.destination == endpoint.logical_name and flow.kind in {"connectivity", "throughput", "simultaneous", "bidirectional"}
    ]
    if receiving:
        parts.append(worst_verdict([flow.verdict for flow in receiving]))
    for flow in involved:
        if flow.verdict in {Verdict.FAIL, Verdict.INCONCLUSIVE, Verdict.WARNING}:
            reasons.append(f"{flow.case_id}: {flow.verdict.value} — {'; '.join(flow.reasons[:3])}")
    verdict = worst_verdict(parts) if parts else Verdict.NOT_RUN
    if endpoint.failure_reason and endpoint.failure_reason not in reasons:
        reasons.insert(0, endpoint.failure_reason)
    return PortReport(
        logical_name=endpoint.logical_name,
        interface=endpoint.interface,
        mac=endpoint.mac,
        namespace=endpoint.namespace,
        vendor=endpoint.identity_vendor,
        driver=endpoint.driver or endpoint.link.driver,
        firmware=endpoint.firmware or endpoint.link.firmware,
        usb=endpoint.usb,
        link=endpoint.link,
        physical_verdict=endpoint.physical_verdict,
        physical_reasons=list(endpoint.physical_reasons),
        dhcp=endpoint.dhcp,
        address=_address(endpoint),
        gateway=endpoint.gateway_result,
        cross_verdict=worst_verdict([flow.verdict for flow in cross]) if cross else Verdict.SKIPPED,
        throughput_avg_mbps=(sum(averages) / len(averages)) if averages else None,
        throughput_verdict=worst_verdict([flow.verdict for flow in throughput]) if throughput else Verdict.SKIPPED,
        tx_mbps=endpoint.tx_mbps,
        rx_mbps=endpoint.rx_mbps,
        iperf_state=endpoint.iperf_state,
        iperf_duration_s=endpoint.iperf_duration_s,
        failure_reason=endpoint.failure_reason,
        rx_errors=rx,
        tx_errors=tx,
        drops=drops,
        link_flaps=flaps,
        verdict=verdict,
        reasons=reasons,
    )


def _address(endpoint: Endpoint) -> str:
    if not endpoint.address:
        return ""
    if endpoint.prefixlen is None:
        return endpoint.address
    return f"{endpoint.address}/{endpoint.prefixlen}"


def _kind_verdict(flows: list[FlowResult], kind: str) -> Verdict:
    selected = [flow.verdict for flow in flows if flow.kind == kind]
    if not selected:
        return Verdict.SKIPPED
    return worst_verdict(selected)


def _error_verdict(flows: list[FlowResult], profile: AcceptanceProfile) -> Verdict:
    selected = []
    for flow in flows:
        delta = flow.error_delta
        if delta is None:
            continue
        drops = delta.delta_rx_dropped + delta.delta_tx_dropped
        if (
            delta.delta_rx_errors > profile.max_rx_errors
            or delta.delta_tx_errors > profile.max_tx_errors
            or drops > profile.max_drops
        ):
            selected.append(Verdict.FAIL)
        else:
            selected.append(Verdict.PASS)
    if not selected:
        return Verdict.SKIPPED
    return worst_verdict(selected)


def _stability_verdict(flows: list[FlowResult], profile: AcceptanceProfile) -> Verdict:
    selected = []
    flap = Verdict.FAIL if profile.link_flap_result == "FAIL" else Verdict.WARNING
    for flow in flows:
        delta = flow.error_delta
        if delta is None:
            continue
        if delta.link_flaps > 0 or delta.delta_carrier_changes > profile.max_carrier_changes:
            selected.append(flap)
        else:
            selected.append(Verdict.PASS)
    if not selected:
        return Verdict.SKIPPED
    return worst_verdict(selected)


def _global_reasons(report: CertificationReport) -> list[str]:
    reasons = []
    for port in report.ports:
        if port.verdict != Verdict.PASS:
            summary = port.failure_reason or (port.reasons[0] if port.reasons else port.verdict.value)
            reasons.append(f"{port.logical_name}: {port.verdict.value} — {summary}")
    if report.global_verdict == Verdict.PASS:
        reasons.append("Every applicable port and traffic stage met the configured profile")
    return reasons
