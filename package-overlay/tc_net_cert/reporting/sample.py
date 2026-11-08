"""A complete sample report so the HTML/CSV/JSON path can be reviewed without a DUT."""

from __future__ import annotations

from tc_net_cert.core.models import (
    Bottleneck,
    CertificationReport,
    DhcpFacts,
    DutIdentity,
    Endpoint,
    ErrorDelta,
    FlowResult,
    HostInfo,
    HostSample,
    LinkFacts,
    MatrixCell,
    NetworkMode,
    PingFacts,
    PortReport,
    StationCleanup,
    UsbInfo,
    Verdict,
)
from tc_net_cert.reporting.render import DISCLAIMER
from tc_net_cert.version import CERTIFICATION_TITLE, SCHEMA_VERSION, __version__


def sample_report() -> CertificationReport:
    link = LinkFacts(
        detected=True,
        speed_mbps=1000,
        duplex="full",
        autoneg="on",
        driver="r8152",
        firmware="rtl8153b-2",
        bus_info="usb-0000:00:14.0-3",
        carrier_changes=0,
    )
    ports = []
    for index, name in enumerate(("LAN1", "LAN2", "LAN3", "LAN4"), start=1):
        ports.append(
            PortReport(
                logical_name=name,
                interface=f"enp{index}s0",
                mac=f"02:00:00:00:00:0{index}",
                namespace=f"ns-{name.lower()}",
                vendor="Realtek USB GbE",
                driver="r8152",
                firmware="rtl8153b-2",
                usb=UsbInfo(is_usb=True, usb_generation="USB 3.x", usb_speed_mbps=5000, manufacturer="Realtek", product="RTL8153"),
                link=link,
                physical_verdict=Verdict.PASS,
                physical_reasons=["Link detected: yes", "Link 1000 Mbps", "Duplex FULL"],
                dhcp=DhcpFacts(
                    success=True,
                    ip=f"192.168.1.{20+index}",
                    prefixlen=24,
                    mask="255.255.255.0",
                    gateway="192.168.1.1",
                    dhcp_server="192.168.1.1",
                    lease_seconds=3600,
                    acquisition_seconds=1.2,
                    backend="internal",
                    verdict=Verdict.PASS,
                    reasons=["DHCP lease acquired"],
                ),
                address=f"192.168.1.{20+index}/24",
                gateway=PingFacts(target="192.168.1.1", transmitted=5, received=5, loss_percent=0, rtt_avg_ms=1.2, verdict=Verdict.PASS, reasons=["0% loss", "avg 1.2 ms"]),
                cross_verdict=Verdict.PASS,
                throughput_avg_mbps=940,
                throughput_verdict=Verdict.PASS,
                verdict=Verdict.PASS,
                reasons=["Throughput 941 Mbps >= minimum 900 Mbps", "Link 1000 Mbps Full Duplex", "RX errors 0", "TX errors 0", "Drops 0", "Carrier changes 0"],
            )
        )
    names = [port.logical_name for port in ports]
    matrix = {
        src: {
            dst: MatrixCell(verdict=Verdict.SKIPPED, note="--")
            if src == dst
            else MatrixCell(mbps=940, verdict=Verdict.PASS, retransmits=0)
            for dst in names
        }
        for src in names
    }
    flows = []
    for src in names:
        for dst in names:
            if src == dst:
                continue
            flows.append(
                FlowResult(
                    case_id=f"THR-{src}-{dst}",
                    kind="throughput",
                    source=src,
                    destination=dst,
                    streams=4,
                    requested_duration_s=120,
                    actual_duration_s=120,
                    mbps_avg=940,
                    mbps_min=920,
                    mbps_max=960,
                    retransmits=0,
                    max_snd_cwnd=262144,
                    error_delta=ErrorDelta(verdict=Verdict.PASS, reasons=["Delta: 0"]),
                    host=HostSample(cpu_percent_max=22, cpu_percent_avg=18, memory_percent=30, load_average=[0.4, 0.3, 0.2]),
                    bottleneck=Bottleneck.NONE,
                    assessment="Throughput meets the configured minimum",
                    verdict=Verdict.PASS,
                    reasons=["Throughput 940 Mbps >= minimum 900 Mbps"],
                    source_ip="192.168.1.21",
                    destination_ip="192.168.1.22",
                )
            )
    return CertificationReport(
        title=CERTIFICATION_TITLE,
        run_id="TCNC-20260922-001",
        tool_version=__version__,
        schema_version=SCHEMA_VERSION,
        config_hash="0" * 64,
        network_mode=NetworkMode.DHCP,
        profile="gigabit",
        speed_profile="1G",
        dut_profile="ont",
        dut=DutIdentity(type="ont", model="HG8145X6-10", serial="EXAMPLE", mac="02:00:00:00:00:10"),
        host=HostInfo(hostname="lab-station", os="Ubuntu", kernel="6.8.0", cpu="Example CPU", memory_mb=16384, python="3.12", tool_version=__version__),
        operator="example",
        station="LAB-01",
        started_at_utc="2026-09-22T19:00:00+00:00",
        started_at_local="2026-09-22T13:00:00-06:00",
        finished_at_utc="2026-09-22T19:40:00+00:00",
        finished_at_local="2026-09-22T13:40:00-06:00",
        ports=ports,
        flows=flows,
        matrix=matrix,
        simultaneous_verdict=Verdict.PASS,
        bidirectional_verdict=Verdict.PASS,
        errors_verdict=Verdict.PASS,
        stability_verdict=Verdict.PASS,
        global_verdict=Verdict.PASS,
        global_reasons=["Every applicable port and traffic stage met the configured profile"],
        disclaimer=DISCLAIMER + " This file is an example, not a measured DUT result.",
        cleanup=StationCleanup(verdict=Verdict.PASS, reasons=["Example cleanup"]),
    )


def sample_endpoints() -> list[Endpoint]:
    return [
        Endpoint(logical_name="LAN1", interface="enp1s0", mac="74:78:27:81:60:44", namespace="ns-lan1"),
        Endpoint(logical_name="LAN2", interface="enp2s0", mac="c8:4d:44:27:16:8f", namespace="ns-lan2"),
    ]
