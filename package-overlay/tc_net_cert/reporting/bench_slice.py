"""Per-bench certificates from one dual-bank measurement session."""

from __future__ import annotations

from tc_net_cert.core.models import CertificationReport, FlowResult, Verdict, worst_verdict
from tc_net_cert.orchestration.report_build import _global_reasons, _kind_verdict

_BENCH_PORTS: dict[str, frozenset[str]] = {
    "A": frozenset({"LAN1", "LAN2", "LAN3", "LAN4"}),
    "B": frozenset({"LAN5", "LAN6", "LAN7", "LAN8"}),
}


def narrow_report_to_bench(report: CertificationReport, bench_id: str) -> CertificationReport:
    bench = bench_id.strip().upper()
    allowed = _BENCH_PORTS.get(bench)
    if allowed is None:
        return report.model_copy(deep=True)

    sliced = report.model_copy(deep=True)
    dut = sliced.bench_duts.get(bench) if sliced.bench_duts else None
    if dut is not None:
        sliced.dut = dut.model_copy(deep=True)
    sliced.bench_scope = bench
    sliced.bench_duts = {bench: sliced.dut.model_copy(deep=True)}

    sliced.ports = [port for port in sliced.ports if port.logical_name in allowed]
    sliced.flows = [flow for flow in sliced.flows if _flow_in_bench(flow, allowed)]
    if sliced.matrix:
        sliced.matrix = {
            source: {
                destination: cell
                for destination, cell in destinations.items()
                if source in allowed and destination in allowed
            }
            for source, destinations in sliced.matrix.items()
            if source in allowed
        }

    pieces = [port.verdict for port in sliced.ports]
    flows = sliced.flows
    pieces.append(_kind_verdict(flows, "simultaneous"))
    pieces.append(_kind_verdict(flows, "bidirectional"))
    pieces.append(_kind_verdict(flows, "connectivity"))
    sliced.simultaneous_verdict = _kind_verdict(flows, "simultaneous")
    sliced.bidirectional_verdict = _kind_verdict(flows, "bidirectional")
    sliced.global_verdict = worst_verdict(pieces) if pieces else Verdict.NOT_RUN
    sliced.global_reasons = _global_reasons(sliced)
    return sliced


def _flow_in_bench(flow: FlowResult, allowed: frozenset[str]) -> bool:
    if flow.kind == "wifi":
        return flow.destination in allowed
    source = flow.source
    dest = flow.destination
    if flow.kind == "qos":
        return source in allowed and (not dest or dest in allowed)
    if dest:
        return source in allowed and dest in allowed
    return source in allowed


def report_for_order_view(report: CertificationReport, order_id: str) -> CertificationReport:
    """Certificate/PDF for TC-0003 must show only banco A even from archived dual JSON."""
    order = str(order_id or "").strip().upper()
    if not order:
        return report
    orders = report.bench_orders or {}
    if orders.get("A") == order:
        return narrow_report_to_bench(report, "A")
    if orders.get("B") == order:
        return narrow_report_to_bench(report, "B")
    scope = str(report.bench_scope or "").strip().upper()
    if scope in {"A", "B"}:
        return narrow_report_to_bench(report, scope)
    inferred = _scope_from_ports(report)
    if inferred == "A+B":
        if str(report.run_id or "").upper() == order:
            return narrow_report_to_bench(report, "A")
    return report


def _scope_from_ports(report: CertificationReport) -> str:
    names = {port.logical_name for port in report.ports}
    bank_a = names & set(_BENCH_PORTS["A"])
    bank_b = names & set(_BENCH_PORTS["B"])
    if bank_a and bank_b:
        return "A+B"
    if bank_b and not bank_a:
        return "B"
    if bank_a:
        return "A"
    return ""
