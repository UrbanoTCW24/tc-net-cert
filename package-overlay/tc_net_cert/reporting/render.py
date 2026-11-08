"""Write JSON, HTML, and CSV from one certification model."""

from __future__ import annotations

import csv
import json
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from tc_net_cert.core.models import CertificationReport, FlowResult
from tc_net_cert.reporting.certificate import certificate_sheet

_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
DISCLAIMER = (
    "This result is an Ethernet Port Performance & Functional Certification "
    "using the laboratory's configured acceptance profile. "
    "It is not an ITU-T conformance claim."
)


def render_reports(report: CertificationReport, run_dir: Path) -> None:
    payload = json.loads(report.model_dump_json())
    (run_dir / "report.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    (run_dir / "report.html").write_text(render_html(report), encoding="utf-8")
    _summary_csv(report, run_dir / "summary.csv")
    _matrix_csv(report, run_dir / "test-matrix.csv")


def render_html(report: CertificationReport) -> str:
    environment = Environment(
        loader=FileSystemLoader(_TEMPLATE_DIR),
        autoescape=True,
        undefined=StrictUndefined,
    )
    template = environment.get_template("report.html.j2")
    return template.render(report=report, sheet=certificate_sheet(report))


def printable_html(folder: Path) -> str | None:
    payload = folder / "report.json"
    if not payload.is_file():
        return None
    try:
        report = CertificationReport.model_validate_json(payload.read_text(encoding="utf-8"))
    except Exception:
        return None
    return render_html(report)


def _summary_csv(report: CertificationReport, path: Path) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "run_id",
                "port",
                "interface",
                "mac",
                "physical",
                "speed_mbps",
                "duplex",
                "dhcp",
                "address",
                "gateway",
                "throughput_avg_mbps",
                "rx_errors",
                "tx_errors",
                "drops",
                "link_flaps",
                "verdict",
            ]
        )
        for port in report.ports:
            writer.writerow(
                [
                    report.run_id,
                    port.logical_name,
                    port.interface,
                    port.mac,
                    port.physical_verdict.value,
                    port.link.speed_mbps if port.link.speed_mbps is not None else "",
                    port.link.duplex,
                    port.dhcp.verdict.value if port.dhcp else "",
                    port.address,
                    port.gateway.verdict.value if port.gateway else "",
                    "" if port.throughput_avg_mbps is None else f"{port.throughput_avg_mbps:.2f}",
                    port.rx_errors,
                    port.tx_errors,
                    port.drops,
                    port.link_flaps,
                    port.verdict.value,
                ]
            )


def _matrix_csv(report: CertificationReport, path: Path) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "run_id",
                "case_id",
                "kind",
                "source",
                "destination",
                "streams",
                "mbps_avg",
                "retransmits",
                "loss_percent",
                "bottleneck",
                "verdict",
                "reasons",
            ]
        )
        for flow in report.flows:
            writer.writerow(_flow_row(report.run_id, flow))


def _flow_row(run_id: str, flow: FlowResult) -> list[object]:
    loss = ""
    if flow.ping and flow.ping.loss_percent is not None:
        loss = f"{flow.ping.loss_percent:.2f}"
    return [
        run_id,
        flow.case_id,
        flow.kind,
        flow.source,
        flow.destination,
        flow.streams,
        "" if flow.mbps_avg is None else f"{flow.mbps_avg:.2f}",
        "" if flow.retransmits is None else flow.retransmits,
        loss,
        flow.bottleneck.value,
        flow.verdict.value,
        " | ".join(flow.reasons),
    ]
