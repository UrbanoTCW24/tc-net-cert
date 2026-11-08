"""Station checks that run before any NIC is moved."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from tc_net_cert.core.exceptions import MissingToolError
from tc_net_cert.core.executor import Executor, which
from tc_net_cert.core.models import AppConfig, InterfaceRecord, ManagementPlane, PreflightCheck, Verdict
from tc_net_cert.namespaces.manager import NamespaceManager, build_plan
from tc_net_cert.config.loader import normalize_mac
from tc_net_cert.network.discovery import index_by_mac


def run_preflight(
    config: AppConfig,
    executor: Executor,
    interfaces: list[InterfaceRecord],
    plane: ManagementPlane,
    *,
    stages: set[str],
    allow_partial: bool,
) -> list[PreflightCheck]:
    checks: list[PreflightCheck] = []
    checks.append(_root_check())
    checks.append(_tool("ip", "sudo apt-get install -y iproute2"))
    checks.append(_tool("ethtool", "sudo apt-get install -y ethtool"))
    checks.append(_tool("ping", "sudo apt-get install -y iputils-ping"))
    if stages & {"throughput", "simultaneous", "bidirectional", "all", "wifi"}:
        checks.append(_iperf())
    if config.network_mode == "dhcp" and config.dhcp.backend == "dhcpcd":
        checks.append(_tool("dhcpcd", "sudo apt-get install -y dhcpcd"))
    if config.network_mode == "dhcp" and config.dhcp.backend == "dhclient":
        checks.append(_tool("dhclient", "sudo apt-get install -y isc-dhcp-client"))
    checks.extend(_port_checks(config, interfaces, plane, allow_partial))
    checks.append(_namespace_conflicts(config, executor))
    checks.append(_runs_writable(config))
    if "simultaneous" in stages or "all" in stages:
        checks.extend(_pair_checks(config))
    return checks


def blocking(checks: list[PreflightCheck]) -> list[PreflightCheck]:
    return [item for item in checks if item.verdict == Verdict.FAIL]


def _root_check() -> PreflightCheck:
    if os.geteuid() == 0:
        return PreflightCheck(name="root", verdict=Verdict.PASS, detail="Running as root")
    return PreflightCheck(
        name="root",
        verdict=Verdict.FAIL,
        detail="Network namespaces require root. Re-run with sudo.",
    )


def _tool(name: str, hint: str) -> PreflightCheck:
    if which(name):
        return PreflightCheck(name=name, verdict=Verdict.PASS, detail=which(name) or name)
    return PreflightCheck(name=name, verdict=Verdict.FAIL, detail=hint)


def _iperf() -> PreflightCheck:
    path = which("iperf3")
    if not path:
        return PreflightCheck(
            name="iperf3",
            verdict=Verdict.FAIL,
            detail="iperf3 is not installed. Install it with: sudo apt-get install -y iperf3",
        )
    return PreflightCheck(name="iperf3", verdict=Verdict.PASS, detail=path)


def iperf_version(executor: Executor) -> str:
    if which("iperf3") is None:
        raise MissingToolError("iperf3", "Install it with: sudo apt-get install -y iperf3")
    result = executor.run(["iperf3", "--version"], timeout=10)
    line = (result.stdout or result.stderr).splitlines()
    return line[0] if line else "iperf3"


def _port_checks(
    config: AppConfig,
    interfaces: list[InterfaceRecord],
    plane: ManagementPlane,
    allow_partial: bool,
) -> list[PreflightCheck]:
    checks: list[PreflightCheck] = []
    if not config.ports:
        checks.append(PreflightCheck(name="ports", verdict=Verdict.FAIL, detail="No ports configured"))
        return checks
    by_mac = {}
    try:
        by_mac = index_by_mac(interfaces)
    except RuntimeError as exc:
        checks.append(PreflightCheck(name="duplicate-mac", verdict=Verdict.FAIL, detail=str(exc)))
        return checks
    missing = []
    protected = []
    excluded = []
    names = []
    ifaces = []
    for logical, port in config.ports.items():
        record = by_mac.get(port.mac)
        if record is None:
            missing.append(f"{logical} {port.mac}")
            continue
        names.append(logical)
        ifaces.append(record.name)
        if record.protected or record.name in plane.protected_interfaces:
            why = "; ".join(record.protection_reasons) or "management plane"
            protected.append(f"{logical} {record.name} ({why})")
        if record.excluded:
            excluded.append(f"{logical} {record.name} ({record.exclusion_reason})")
    if missing:
        checks.append(PreflightCheck(name="mac-present", verdict=Verdict.FAIL, detail="Missing " + ", ".join(missing)))
    else:
        checks.append(PreflightCheck(name="mac-present", verdict=Verdict.PASS, detail=f"{len(names)} NIC(s) matched by MAC"))
    try:
        mgmt = normalize_mac(config.management_mac or "")
    except Exception:
        mgmt = ""
    if mgmt:
        on_mgmt = []
        for logical, port in config.ports.items():
            try:
                if normalize_mac(port.mac) == mgmt:
                    record = by_mac.get(mgmt)
                    iface = record.name if record else "?"
                    on_mgmt.append(f"{logical}→{iface}")
            except Exception:
                continue
        if on_mgmt:
            checks.append(
                PreflightCheck(
                    name="management-on-test-port",
                    verdict=Verdict.FAIL,
                    detail=(
                        "La MAC de administración no puede ser puerto de prueba: "
                        + ", ".join(on_mgmt)
                        + ". En el panel: Reconfigurar LAN del banco."
                    ),
                )
            )
    if protected and config.safe_mode:
        checks.append(
            PreflightCheck(
                name="management-protection",
                verdict=Verdict.FAIL,
                detail="SAFE MODE blocks " + ", ".join(protected),
            )
        )
    elif protected:
        checks.append(
            PreflightCheck(
                name="management-protection",
                verdict=Verdict.WARNING,
                detail="Management NIC selected outside SAFE MODE: " + ", ".join(protected),
            )
        )
    else:
        detail = plane.default_route_interface or "no default route"
        checks.append(PreflightCheck(name="management-protection", verdict=Verdict.PASS, detail=f"Protected: {detail}"))
    if excluded:
        checks.append(PreflightCheck(name="physical-nic", verdict=Verdict.FAIL, detail=", ".join(excluded)))
    else:
        checks.append(PreflightCheck(name="physical-nic", verdict=Verdict.PASS, detail="All selected NICs are eligible"))
    if len(ifaces) != len(set(ifaces)):
        checks.append(PreflightCheck(name="unique-interface", verdict=Verdict.FAIL, detail="Two logical ports resolved to one NIC"))
    profile = config.dut_profile()
    if config.require_profile_port_count and not allow_partial and len(config.ports) < profile.ports:
        checks.append(
            PreflightCheck(
                name="port-count",
                verdict=Verdict.FAIL,
                detail=f"Profile {config.dut.type} expects {profile.ports} ports, {len(config.ports)} configured",
            )
        )
    else:
        checks.append(
            PreflightCheck(
                name="port-count",
                verdict=Verdict.PASS,
                detail=f"{len(config.ports)} configured, profile wants {profile.ports}",
            )
        )
    if config.network_mode == "static":
        missing_addr = [name for name in config.ports if name not in config.static_addresses]
        if missing_addr:
            checks.append(
                PreflightCheck(
                    name="static-addresses",
                    verdict=Verdict.FAIL,
                    detail="Missing static address for " + ", ".join(missing_addr),
                )
            )
        else:
            checks.append(PreflightCheck(name="static-addresses", verdict=Verdict.PASS, detail="Static map complete"))
    try:
        build_plan([(name, f"preview-{index}", config.ports[name].mac) for index, name in enumerate(config.ports)])
    except Exception as exc:  # noqa: BLE001
        checks.append(PreflightCheck(name="namespace-plan", verdict=Verdict.FAIL, detail=str(exc)))
    return checks


def _namespace_conflicts(config: AppConfig, executor: Executor) -> PreflightCheck:
    existing = set(NamespaceManager(executor).list_namespaces())
    wanted = {f"ns-{name.lower()}" for name in config.ports}
    clash = sorted(existing & wanted)
    if clash:
        return PreflightCheck(
            name="namespaces-clear",
            verdict=Verdict.FAIL,
            detail="Already present: " + ", ".join(clash) + ". Run tc-net-cert recover",
        )
    return PreflightCheck(name="namespaces-clear", verdict=Verdict.PASS, detail="No conflicting namespaces")


def _runs_writable(config: AppConfig) -> PreflightCheck:
    from tc_net_cert.config.loader import runs_directory

    path = runs_directory(config)
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return PreflightCheck(name="runs-dir", verdict=Verdict.FAIL, detail=str(exc))
    free = shutil.disk_usage(path).free
    if free < 200 * 1024 * 1024:
        return PreflightCheck(name="runs-dir", verdict=Verdict.WARNING, detail=f"Low disk space at {path}")
    return PreflightCheck(name="runs-dir", verdict=Verdict.PASS, detail=str(path))


def _pair_checks(config: AppConfig) -> list[PreflightCheck]:
    known = set(config.ports)
    bad = []
    for pair in config.simultaneous_pairs:
        if len(pair) != 2 or pair[0] not in known or pair[1] not in known:
            bad.append(str(pair))
    if bad:
        return [PreflightCheck(name="simultaneous-pairs", verdict=Verdict.FAIL, detail="Unknown pairs " + ", ".join(bad))]
    if not config.simultaneous_pairs:
        return [PreflightCheck(name="simultaneous-pairs", verdict=Verdict.WARNING, detail="No simultaneous pairs configured")]
    return [PreflightCheck(name="simultaneous-pairs", verdict=Verdict.PASS, detail=str(config.simultaneous_pairs))]


def estimate_seconds(config: AppConfig, stages: set[str]) -> int:
    ports = max(1, len(config.ports))
    directional = ports * (ports - 1)
    duration = config.iperf.duration_seconds
    total = 0
    if "throughput" in stages:
        runs = directional * (2 if config.iperf.run_single_stream else 1)
        total += runs * duration
    if "simultaneous" in stages:
        total += 2 * duration
    if "bidirectional" in stages:
        total += duration
    if stages & {"throughput", "simultaneous", "bidirectional", "wifi"}:
        window = duration if "wifi" in stages and not (stages & {"throughput", "simultaneous", "bidirectional"}) else 60
        total += 2 * window + 40
    if "cross" in stages:
        total += directional * 2
    return total


def format_duration(seconds: int) -> str:
    minutes, sec = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m {sec}s"
    if minutes:
        return f"{minutes}m {sec}s"
    return f"{sec}s"
