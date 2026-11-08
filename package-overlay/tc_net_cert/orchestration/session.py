"""Certification session. Cleanup runs on success, failure, SIGINT, and SIGTERM."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from tc_net_cert.assessment.verdict import judge_dhcp, judge_link, judge_ping
from tc_net_cert.audit.integrity import seal_run
from tc_net_cert.audit.trail import AuditTrail
from tc_net_cert.config.benches import bench_port_list, scope_config_ports
from tc_net_cert.config.loader import config_hash, runs_directory
from tc_net_cert.core.cleanup import CleanupController
from tc_net_cert.core.exceptions import ConfigError, PreflightError, ToolError
from tc_net_cert.core.executor import CommandResult, Executor, current_audit
from tc_net_cert.core.hostinfo import collect_host_info
from tc_net_cert.core.models import (
    ActiveSession,
    AppConfig,
    CertificationReport,
    DhcpFacts,
    DutIdentity,
    Endpoint,
    FlowResult,
    NetworkMode,
    PortConfig,
    RfChannel,
    WanService,
    StationCleanup,
    Verdict,
    worst_verdict,
)
from tc_net_cert.orchestration.guard import validateInterface
from tc_net_cert.orchestration.lanes import failure_text, iperf_allowed
from tc_net_cert.dhcp.acquire import acquire_lease, is_link_local, release_lease, stop_dhcpcd_on_host
from tc_net_cert.dut.huawei import (
    BRAND_ENV,
    HOST_ENV,
    JOB_ENV,
    MAC_ENV,
    MODEL_ENV,
    PASSWORD_ENV,
    PORTALS_ENV,
    SERIAL_ENV,
    USER_ENV,
    allowed_host,
    portal_retryable,
    valid_job,
)
from tc_net_cert.dut.portals import load_portals
from tc_net_cert.ethtool.link import explain_link_down, settle_link
from tc_net_cert.iperf.runner import IperfPool
from tc_net_cert.namespaces.manager import NamespaceManager, build_plan
from tc_net_cert.network.discovery import discover_interfaces, index_by_mac
from tc_net_cert.network.management import (
    assert_safe_to_move,
    detect_management,
    mark_protected,
    pin_management_mac,
    restore_management_routing,
    scrub_lab_port_default_routes,
)
from tc_net_cert.network.usb import capture_usb_inventory, usb_info_for
from tc_net_cert.orchestration.measure import measure_flow
from tc_net_cert.orchestration.preflight import (
    blocking,
    estimate_seconds,
    format_duration,
    run_preflight,
)
from tc_net_cert.orchestration.report_build import assemble_report
from tc_net_cert.reporting.render import render_reports
from tc_net_cert.storage.db import upsert_run
from tc_net_cert.storage.runs import allocate_run, resolve_order_for_serial, session_state_path
from tc_net_cert.tests.connectivity import connectivity_flow, directional_pairs, ping_target
from tc_net_cert.version import SCHEMA_VERSION, __version__

Notify = Callable[[str], None]

BENCH_PORTS_ENV = "TC_NET_CERT_BENCH_PORTS"
BENCH_ID_ENV = "TC_NET_CERT_BENCH_ID"
BENCH_CREDENTIALS_ENV = "TC_NET_CERT_BENCH_CREDENTIALS"


def _ports_lost_to_carrier(flows: list[FlowResult]) -> set[str]:
    """Ports whose own window died. The other pair still crosses."""
    lost: set[str] = set()
    for flow in flows:
        text = " ".join(flow.reasons)
        if "UNIT_POWER_OFF_OR_REBOOT" not in text and "INTERFACE_DOWN" not in text and "TEST_ABORTED_BY_CARRIER" not in text:
            continue
        lost.add(flow.source)
        lost.add(flow.destination)
    return lost


class CertificationSession:
    def __init__(
        self,
        config: AppConfig,
        executor: Executor,
        stages: set[str],
        *,
        allow_partial: bool = False,
        override_token: str = "",
        notify: Notify | None = None,
        job: str = "",
    ) -> None:
        self.config = config
        self.executor = executor
        self.stages = _expand_stages(stages, config)
        self.allow_partial = allow_partial
        self.override_token = override_token
        self.notify = notify or (lambda _message: None)
        self.cleanup = CleanupController()
        self.manager = NamespaceManager(executor)
        self.endpoints: list[Endpoint] = []
        self.flows: list[FlowResult] = []
        self.active = ActiveSession()
        self.moved: set[str] = set()
        self.pool = IperfPool()
        self.abort = ""
        self._wrote_state = False
        self.run_dir = None
        self.audit: AuditTrail | None = None
        self.wifi_interface = ""
        self.wifi_address = ""
        self.wifi_state = ""
        self._iperf_noted: set[str] = set()
        self._ds_uncorrectable_before: int | None = None
        self.job = (job or os.environ.get(JOB_ENV, "")).strip()
        if self.job.lower().startswith("job-"):
            self.job = "JOB-" + self.job[4:]
        if self.job and not valid_job(self.job):
            raise ConfigError("El JOB tiene que verse como JOB-1042")
        self.bench_id = os.environ.get(BENCH_ID_ENV, "").strip().upper()
        self.bench_run_ids: dict[str, str] = {}
        self.bench_run_dirs: dict[str, Path] = {}

    def _active_ports(self) -> dict[str, PortConfig]:
        scoped = os.environ.get(BENCH_PORTS_ENV, "").strip()
        if scoped:
            wanted = {item.strip().upper() for item in scoped.split(",") if item.strip()}
            return {name: port for name, port in self.config.ports.items() if name.upper() in wanted}
        if self.bench_id:
            names = set(bench_port_list(self.config, self.bench_id))
            return {name: port for name, port in self.config.ports.items() if name in names}
        return dict(self.config.ports)

    def _report_bench_scope(self) -> str:
        if self.bench_id in {"A", "B"}:
            return self.bench_id
        names = {name.upper() for name in self._active_ports()}
        bank_a = names & {"LAN1", "LAN2", "LAN3", "LAN4"}
        bank_b = names & {"LAN5", "LAN6", "LAN7", "LAN8"}
        if bank_a and bank_b:
            return "A+B"
        if bank_b and not bank_a:
            return "B"
        if bank_a:
            return "A"
        return ""

    def _opening_bench_scope(self) -> str:
        if self.bench_id in {"A", "B"}:
            return self.bench_id
        names = {name.upper() for name in self._active_ports()}
        bank_a = names & {"LAN1", "LAN2", "LAN3", "LAN4"}
        bank_b = names & {"LAN5", "LAN6", "LAN7", "LAN8"}
        if bank_a and bank_b:
            return "A+B"
        if bank_b and not bank_a:
            return "B"
        return "A"

    def _bench_serial(self, bench_id: str) -> str:
        from tc_net_cert.storage.bench_identity import load_bench_inventory

        if bench_id == "A":
            serial = os.environ.get(SERIAL_ENV, "").strip()
            if serial:
                return serial
        inv = load_bench_inventory(bench_id)
        return str(inv.get("serial") or "").strip()

    def _bind_bench_order(self, bench_id: str, serial: str) -> None:
        if not serial:
            return
        runs_root = runs_directory(self.config)
        run_id, run_dir, reused = resolve_order_for_serial(runs_root, serial)
        self.bench_run_ids[bench_id] = run_id
        self.bench_run_dirs[bench_id] = run_dir
        if reused:
            self.notify(f"ORDEN {run_id} sigue — banco {bench_id} (SN {serial}).")
        else:
            self.notify(f"ORDEN {run_id} nueva — banco {bench_id}.")

    def execute(self) -> CertificationReport:
        started_utc, started_local = _stamps()
        from tc_net_cert.storage.dut_identity import cached_serial

        scope = self._opening_bench_scope()
        serial_a = self._bench_serial("A") or cached_serial()
        serial_b = self._bench_serial("B")
        if scope == "A+B":
            if serial_a:
                self._bind_bench_order("A", serial_a)
            if serial_b:
                self._bind_bench_order("B", serial_b)
            if "A" in self.bench_run_dirs:
                run_id, run_dir = self.bench_run_ids["A"], self.bench_run_dirs["A"]
            elif "B" in self.bench_run_dirs:
                run_id, run_dir = self.bench_run_ids["B"], self.bench_run_dirs["B"]
            else:
                run_id, run_dir = allocate_run(runs_directory(self.config))
                self.bench_run_ids["A"] = run_id
                self.bench_run_dirs["A"] = run_dir
        elif scope == "B":
            serial = serial_b or serial_a
            if serial:
                self._bind_bench_order("B", serial)
                run_id, run_dir = self.bench_run_ids["B"], self.bench_run_dirs["B"]
            else:
                run_id, run_dir = allocate_run(runs_directory(self.config))
        else:
            serial = serial_a
            if serial:
                self._bind_bench_order("A", serial)
                run_id, run_dir = self.bench_run_ids["A"], self.bench_run_dirs["A"]
            else:
                run_id, run_dir = allocate_run(runs_directory(self.config))
        if scope == "A+B" and not serial_b:
            self.notify("Banco B: sin SN previo — se asignará orden al leer el portal.")
        self.run_dir = run_dir
        self.audit = AuditTrail(run_dir)
        self.active = ActiveSession(run_id=run_id, started_at_utc=started_utc)
        report = CertificationReport(
            title=self.config.report_title,
            run_id=run_id,
            tool_version=__version__,
            schema_version=SCHEMA_VERSION,
            config_hash=config_hash(self.config),
            network_mode=NetworkMode.DHCP if self.config.network_mode == "dhcp" else NetworkMode.STATIC,
            profile=self.config.active_profile,
            speed_profile=self.config.active_speed_profile,
            dut_profile=self.config.dut.type,
            dut=self.config.dut,
            host=collect_host_info(),
            operator=self.config.operator,
            station=self.config.station,
            started_at_utc=started_utc,
            started_at_local=started_local,
            stages=sorted(self.stages),
            job=self.job,
        )
        token = current_audit.set(self.audit)
        self.cleanup.add("restore-management-route", self._restore_management_route)
        self.cleanup.add("release-dhcp", self._release_dhcp)
        self.cleanup.add("restore-namespaces", self._restore)
        self.cleanup.add("stop-iperf", self._stop_iperf)
        self.cleanup.install_signals()
        try:
            self.audit.write_raw("system/config.snapshot.json", self.config.model_dump_json(indent=2))
            self._announce()
            discovered = discover_interfaces(self.executor)
            for note in scrub_lab_port_default_routes(
                self.executor,
                discovered,
                port_macs={name: port.mac for name, port in self.config.ports.items()},
                management_mac=self.config.management_mac,
            ):
                self.notify(f"ROUTING {note}")
            interfaces = mark_protected(
                discovered,
                detect_management(self.executor, discovered),
            )
            plane = detect_management(self.executor, interfaces)
            interfaces = mark_protected(interfaces, plane)
            interfaces = pin_management_mac(interfaces, self.config.management_mac)
            self._remember_wifi(interfaces)
            scoped_config = scope_config_ports(self.config, self._active_ports())
            checks = run_preflight(
                scoped_config,
                self.executor,
                interfaces,
                plane,
                stages=self.stages,
                allow_partial=self.allow_partial,
            )
            failed = blocking(checks)
            for check in checks:
                self.notify(f"PREFLIGHT {check.verdict.value} {check.name}: {check.detail}")
            if failed:
                self.abort = "; ".join(f"{item.name}: {item.detail}" for item in failed)
                raise PreflightError(self.abort)
            capture_usb_inventory(self.executor, run_dir / "raw" / "system")
            self._prepare(interfaces)
            if "physical" in self.stages:
                self._physical()
            if "addressing" in self.stages:
                self._address()
            if "gateway" in self.stages:
                self._gateway()
            if "cross" in self.stages:
                self.flows.extend(self._cross())
            if self.stages & {"throughput", "simultaneous", "bidirectional"}:
                if self._hold_iperf():
                    self._start_wifi_server()
                else:
                    self._snapshot_ds_errors()
                    self._start_iperf()
                    if "throughput" in self.stages:
                        self.flows.extend(self._throughput(single=False))
                        if self.config.iperf.run_single_stream:
                            self.flows.extend(self._throughput(single=True))
                    if "simultaneous" in self.stages:
                        self.flows.extend(self._simultaneous())
                    if "bidirectional" in self.stages:
                        self.flows.extend(self._bidirectional())
                    self.flows.extend(self._qos())
                self.flows.extend(self._wifi_bands())
            elif "wifi" in self.stages:
                self._start_iperf()
                self.flows.extend(self._wifi_bands())
        except (PreflightError, ToolError) as exc:
            self.abort = str(exc)
        except Exception as exc:
            self.abort = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            current_audit.reset(token)
            self._finish(report)
        return report

    def _announce(self) -> None:
        seconds = estimate_seconds(self.config, self.stages)
        self.notify(f"Estimated measurement time: {format_duration(seconds)}")
        if self.config.iperf.duration_seconds > self.config.long_duration_seconds:
            self.notify("LONG DURATION TEST")
            self.notify("Thermal/stability test enabled")
        mode = "DHCP" if self.config.network_mode == "dhcp" else "STATIC ENGINEERING"
        self.notify(f"NETWORK MODE: {mode}")

    def _prepare(self, interfaces) -> None:
        by_mac = index_by_mac(interfaces)
        bindings = []
        selected = []
        for logical, port in self._active_ports().items():
            record = by_mac.get(port.mac)
            if record is None:
                raise ToolError(f"{logical} MAC {port.mac} is not present")
            assert_safe_to_move(record, safe_mode=self.config.safe_mode, override_token=self.override_token)
            bindings.append((logical, record.name, record.mac))
            selected.append(record)
        plan = build_plan(bindings)
        for endpoint, record in zip(plan.endpoints, selected, strict=True):
            endpoint.usb = usb_info_for(record.name)
            endpoint.identity_vendor = record.display_vendor
            endpoint.driver = record.driver
            endpoint.iperf_port = self.config.iperf.base_port + len(self.endpoints)
            self.endpoints.append(endpoint)
        for endpoint in self.endpoints:
            self._handoff_interface_to_namespace(endpoint)
            snapshot = self.manager.snapshot(endpoint)
            self.active.snapshots.append(snapshot)
            self.active.namespaces.append(endpoint.namespace)
            self._persist_active()
            self.manager.move_one(endpoint)
            self.moved.add(endpoint.logical_name)
            self._persist_active()

    def _handoff_interface_to_namespace(self, endpoint: Endpoint) -> None:
        """Drop root-namespace DHCP/IP so the ONT can answer again inside ns-lan*."""
        iface = endpoint.interface
        stop_dhcpcd_on_host(self.executor, iface)
        self.executor.run(["ip", "addr", "flush", "dev", iface], timeout=10, interface=iface)
        self.executor.run(["ip", "-4", "route", "flush", "dev", iface], timeout=10, interface=iface)

    def _physical(self) -> None:
        import time

        profile = self.config.acceptance()
        if self.endpoints:
            time.sleep(2.0)
        for endpoint in self.endpoints:
            link = settle_link(
                self.executor,
                endpoint.interface,
                endpoint.namespace,
                attempts=36,
            )
            endpoint.supported_speeds_mbps = list(link.supported_speeds_mbps)
            endpoint.driver = link.driver or endpoint.driver
            endpoint.firmware = link.firmware or endpoint.firmware
            if not link.detected:
                reason = explain_link_down(link.speed_mbps, link.duplex)
                link.speed_mbps = None
                link.duplex = ""
                endpoint.link = link
                verdict, reasons = Verdict.FAIL, [reason]
            else:
                endpoint.link = link
                verdict, reasons = judge_link(link, profile)
            endpoint.physical_verdict = verdict
            endpoint.physical_reasons = reasons
            endpoint.stage = "LINK_CHECK"
            if verdict == Verdict.PASS:
                self.notify(f"STATE {endpoint.logical_name} LINK_CHECK PASS")
            else:
                reason = next(
                    (item for item in reasons if item.strip() and item.strip() != "Link detected: yes"),
                    failure_text(reasons, "Link check failed"),
                )
                endpoint.failure_reason = reason
                self.notify(f"STATE {endpoint.logical_name} LINK_CHECK FAIL {reason}")
            if link.detected:
                self.notify(
                    f"PORT {endpoint.logical_name} enlace si {link.speed_mbps or 'n/a'} {link.duplex or ''}".strip()
                )
            else:
                self.notify(f"PORT {endpoint.logical_name} enlace no")
            raw = self.executor.run(
                ["ethtool", endpoint.interface],
                namespace=endpoint.namespace,
                interface=endpoint.interface,
                timeout=15,
            )
            assert self.audit is not None
            self.audit.write_raw(f"ethtool/{endpoint.logical_name}.txt", raw.stdout + raw.stderr)
            stats = self.executor.run(
                ["ip", "-s", "-j", "link", "show", "dev", endpoint.interface],
                namespace=endpoint.namespace,
                interface=endpoint.interface,
                timeout=10,
            )
            self.audit.write_raw(f"ip/{endpoint.logical_name}.json", stats.stdout or "{}\n")

    def _address(self) -> None:
        if self.config.network_mode == "static":
            self._static()
            return
        for endpoint in self.endpoints:
            if endpoint.physical_verdict != Verdict.PASS:
                self._skip_dhcp(endpoint)
                continue
            endpoint.stage = "DHCP"
            self.notify(f"STATE {endpoint.logical_name} DHCP REQUESTING")
            self.notify(f"PORT {endpoint.logical_name} dhcp pidiendo")
            facts = acquire_lease(
                self.executor,
                endpoint,
                self.config.dhcp.backend,
                self.config.dhcp.timeout_seconds,
            )
            verdict, reasons = judge_dhcp(facts)
            facts.verdict = verdict
            facts.reasons = reasons
            endpoint.dhcp = facts
            if facts.success:
                endpoint.address = facts.ip
                endpoint.prefixlen = facts.prefixlen
                endpoint.gateway = facts.gateway
                endpoint.stage = "CONNECTIVITY"
                self.notify(f"PORT {endpoint.logical_name} dhcp ok {facts.ip}")
                self.notify(f"STATE {endpoint.logical_name} DHCP PASS {facts.ip}")
            else:
                reason = facts.message or "DHCP timeout"
                endpoint.failure_reason = reason
                endpoint.iperf_state = Verdict.SKIPPED
                endpoint.stage = "RESULT"
                self.notify(f"PORT {endpoint.logical_name} dhcp fail {reason}")
                self.notify(f"STATE {endpoint.logical_name} DHCP FAIL {reason}")
                self.notify(f"STATE {endpoint.logical_name} IPERF3 NOT_RUN {reason}")
            assert self.audit is not None
            self.audit.write_raw(f"dhcp/{endpoint.logical_name}.json", facts.model_dump_json(indent=2))

    def _static(self) -> None:
        import ipaddress

        for endpoint in self.endpoints:
            if endpoint.physical_verdict != Verdict.PASS:
                self._skip_dhcp(endpoint)
                continue
            cidr = self.config.static_addresses[endpoint.logical_name]
            interface = ipaddress.ip_interface(cidr)
            self.manager.add_address(endpoint, cidr)
            endpoint.address = str(interface.ip)
            endpoint.prefixlen = interface.network.prefixlen
            gateway = self.config.static_gateways.get(endpoint.logical_name, "")
            if gateway:
                self.manager.add_default_route(endpoint, gateway)
                endpoint.gateway = gateway
            from tc_net_cert.core.models import DhcpFacts

            endpoint.dhcp = DhcpFacts(
                success=False,
                verdict=Verdict.SKIPPED,
                backend="static",
                message="STATIC ENGINEERING",
                reasons=["STATIC ENGINEERING — DHCP was not used"],
            )

    def _gateway(self) -> None:
        profile = self.config.acceptance()
        for endpoint in self.endpoints:
            if not self._can_iperf(endpoint):
                continue
            if self.config.network_mode != "dhcp" and not endpoint.gateway:
                continue
            if not endpoint.gateway:
                from tc_net_cert.core.models import PingFacts

                endpoint.gateway_result = PingFacts(
                    verdict=Verdict.FAIL,
                    reasons=["No gateway was learned from DHCP"],
                )
                continue
            ping = ping_target(
                self.executor,
                endpoint,
                endpoint.gateway,
                self.config.ping.count,
                self.config.ping.timeout_seconds,
            )
            verdict, reasons = judge_ping(ping, profile)
            ping.verdict = verdict
            ping.reasons = [f"{endpoint.logical_name} → Gateway", *reasons]
            endpoint.gateway_result = ping
            assert self.audit is not None
            self.audit.write_raw(
                f"ping/{endpoint.logical_name}-gateway.txt",
                "\n".join(ping.reasons) + "\n",
            )

    def _cross(self) -> list[FlowResult]:
        if len(self.endpoints) < 2:
            self.notify("Cross-port connectivity skipped: fewer than two ports")
            return []
        profile = self.config.acceptance()
        by_name = {item.logical_name: item for item in self.endpoints}
        flows: list[FlowResult] = []
        for source_name, dest_name in directional_pairs(list(by_name)):
            source = by_name[source_name]
            dest = by_name[dest_name]
            if not self._can_iperf(source) or not self._can_iperf(dest):
                reason = self._blocked_reason(source) or self._blocked_reason(dest)
                flows.append(
                    self._not_run_flow(
                        source,
                        dest,
                        kind="connectivity",
                        case_id=f"PING-{source_name}-{dest_name}",
                        direction="forward",
                        reason=reason,
                    )
                )
                continue
            ping = ping_target(
                self.executor,
                source,
                dest.address,
                self.config.ping.count,
                self.config.ping.timeout_seconds,
            )
            verdict, reasons = judge_ping(ping, profile)
            ping.verdict = verdict
            ping.reasons = reasons
            flows.append(connectivity_flow(source, dest, ping, verdict, reasons))
        return flows

    def _hold_iperf(self) -> bool:
        ready = [item for item in self.endpoints if self._can_iperf(item)]
        blocked = [item for item in self.endpoints if not self._can_iperf(item)]
        if ready:
            if blocked:
                detail = "; ".join(f"{item.logical_name}: {self._blocked_reason(item)}" for item in blocked)
                self.notify(
                    f"IPERF3 parcial: {len(ready)} LAN con DHCP; se omiten "
                    + detail
                )
            return False
        detail = "; ".join(f"{item.logical_name}: {self._blocked_reason(item)}" for item in blocked)
        reason = f"IPERF3 NOT RUN. {detail}"
        self.notify(reason)
        self.notify("JITTER y PACKET LOSS no inician: iperf3 de las LAN no corre si una LAN no tiene dirección")
        self.notify("WIFI sigue: usa la clave leída y mide contra LAN2, o contra LAN3 si LAN2 no tiene IP")
        for endpoint in self.endpoints:
            endpoint.iperf_state = Verdict.SKIPPED
            endpoint.stage = "RESULT"
            if not endpoint.failure_reason:
                endpoint.failure_reason = reason
            self.notify(f"STATE {endpoint.logical_name} IPERF3 NOT_RUN {reason}")
        return True

    def _start_iperf(self) -> None:
        ready = [item for item in self.endpoints if self._can_iperf(item)]
        for endpoint in self.endpoints:
            if endpoint in ready:
                continue
            reason = self._blocked_reason(endpoint)
            endpoint.iperf_state = Verdict.SKIPPED
            if not endpoint.failure_reason:
                endpoint.failure_reason = reason
            self._note_iperf_skipped(endpoint.logical_name, reason)
        if not ready:
            self.notify("IPERF3 NOT RUN")
            return
        assert self.audit is not None
        for endpoint in ready:
            endpoint.stage = "IPERF3"
            self.notify(f"STATE {endpoint.logical_name} IPERF3 STARTING")
            self.pool.start(endpoint)
            self.audit.record_command(
                CommandResult(
                    argv=["iperf3", "-s", "-B", endpoint.address, "-p", str(endpoint.iperf_port)],
                    exit_code=0,
                    stdout="",
                    stderr="",
                    duration_s=0,
                    namespace=endpoint.namespace,
                    interface=endpoint.interface,
                    command=f"iperf3 -s -B {endpoint.address} -p {endpoint.iperf_port}",
                    started_at=datetime.now(timezone.utc).isoformat(),
                )
            )

    def _throughput(self, *, single: bool) -> list[FlowResult]:
        if len(self.endpoints) < 2:
            self.notify("Throughput matrix skipped: fewer than two ports")
            return []
        by_name = {item.logical_name: item for item in self.endpoints}
        flows: list[FlowResult] = []
        streams = 1 if single else self.config.iperf.streams
        kind = "throughput-single" if single else "throughput"
        prefix = "ONE" if single else "THR"
        for source_name, dest_name in directional_pairs(list(by_name)):
            source = by_name[source_name]
            destination = by_name[dest_name]
            if not self._can_iperf(source) or not self._can_iperf(destination):
                reason = self._blocked_reason(source) or self._blocked_reason(destination)
                flows.append(
                    self._not_run_flow(
                        source,
                        destination,
                        kind=kind,
                        case_id=f"{prefix}-{source_name}-{dest_name}",
                        direction="forward",
                        reason=reason,
                    )
                )
                continue
            self.notify(f"{source_name} → {dest_name} streams={streams}")
            flows.append(
                self._flow(
                    by_name[source_name],
                    by_name[dest_name],
                    kind=kind,
                    case_id=f"{prefix}-{source_name}-{dest_name}",
                    direction="forward",
                    streams=streams,
                )
            )
        return flows

    def _simultaneous(self) -> list[FlowResult]:
        pairs = self._configured_pairs()
        if not pairs:
            return []
        forward = [pair for pair in pairs if self._can_iperf(pair[0]) and self._can_iperf(pair[1])]
        if forward:
            self._announce_roles(forward, "1")
        flows = self._window(pairs, "SIM-FWD", "forward", "simultaneous")
        reverse = [(dst, src) for src, dst in pairs]
        lost = _ports_lost_to_carrier(flows)
        if lost:
            self.notify("TEST_ABORTED_BY_CARRIER")
            kept = []
            for source, destination in reverse:
                if source.logical_name in lost or destination.logical_name in lost:
                    self.notify(
                        f"STATE {source.logical_name} TEST_ABORTED_BY_CARRIER FAIL "
                        "FALLA: Caída de Enlace / Reinicio del DUT"
                    )
                    flows.append(
                        self._not_run_flow(
                            source,
                            destination,
                            kind="simultaneous",
                            case_id=f"SIM-REV-{source.logical_name}-{destination.logical_name}",
                            direction="reverse",
                            reason="TEST_ABORTED_BY_CARRIER",
                        )
                    )
                    continue
                kept.append((source, destination))
            reverse = kept
        if not reverse:
            self._stop_iperf()
            return flows
        time.sleep(0.5)
        backward = [pair for pair in reverse if self._can_iperf(pair[0]) and self._can_iperf(pair[1])]
        if backward:
            self._announce_roles(backward, "cruzada")
        flows.extend(self._window(reverse, "SIM-REV", "reverse", "simultaneous"))
        return flows

    def _announce_roles(self, pairs: list[tuple[Endpoint, Endpoint]], window: str) -> None:
        transmitters = ",".join(source.logical_name for source, _destination in pairs)
        clients = ",".join(destination.logical_name for _source, destination in pairs)
        self.notify(f"ROLES TX {transmitters} RX {clients} WINDOW {window}")

    def _bidirectional(self) -> list[FlowResult]:
        pairs = self._configured_pairs()
        if not pairs:
            return []
        both = list(pairs) + [(dst, src) for src, dst in pairs]
        return self._window(both, "BIDIR", "both", "bidirectional")

    def _configured_pairs(self) -> list[tuple[Endpoint, Endpoint]]:
        by_name = {item.logical_name: item for item in self.endpoints}
        active = set(self._active_ports().keys())
        pairs = []
        for left, right in self.config.simultaneous_pairs:
            if left in by_name and right in by_name and left in active and right in active:
                pairs.append((by_name[left], by_name[right]))
        if pairs:
            return pairs
        ordered = sorted(by_name)
        if len(ordered) >= 4:
            return [
                (by_name[ordered[0]], by_name[ordered[2]]),
                (by_name[ordered[1]], by_name[ordered[3]]),
            ]
        return pairs

    def _window(
        self,
        pairs: list[tuple[Endpoint, Endpoint]],
        prefix: str,
        direction: str,
        kind: str,
    ) -> list[FlowResult]:
        runnable = [
            (source, destination)
            for source, destination in pairs
            if self._can_iperf(source) and self._can_iperf(destination)
        ]
        skipped = [
            self._not_run_flow(
                source,
                destination,
                kind=kind,
                case_id=f"{prefix}-{source.logical_name}-{destination.logical_name}",
                direction=direction,
                reason=self._blocked_reason(source) or self._blocked_reason(destination),
            )
            for source, destination in pairs
            if not (self._can_iperf(source) and self._can_iperf(destination))
        ]
        if not runnable:
            return skipped
        guarded: list[FlowResult] = []
        open_pairs: list[tuple[Endpoint, Endpoint]] = []
        for source, destination in runnable:
            checks = (
                (
                    source,
                    validateInterface(
                        source.interface,
                        namespace=source.namespace,
                        executor=self.executor,
                        address=source.address,
                    ),
                ),
                (
                    destination,
                    validateInterface(
                        destination.interface,
                        namespace=destination.namespace,
                        executor=self.executor,
                        address=destination.address,
                    ),
                ),
            )
            blocks = [(endpoint, check) for endpoint, check in checks if not check["ok"]]
            if blocks:
                for endpoint, check in blocks:
                    self.notify(f"STATE {endpoint.logical_name} {check['status']} FAIL {check['details']}")
                guarded.append(
                    self._not_run_flow(
                        source,
                        destination,
                        kind=kind,
                        case_id=f"{prefix}-{source.logical_name}-{destination.logical_name}",
                        direction=direction,
                        reason="; ".join(str(check["details"]) for _endpoint, check in blocks),
                    )
                )
                continue
            open_pairs.append((source, destination))
        if not open_pairs:
            return skipped + guarded
        pairs = open_pairs
        ready = threading.Barrier(len(pairs) + 1)
        start = threading.Event()
        found: list[FlowResult] = []
        errors: list[BaseException] = []
        lock = threading.Lock()

        def worker(source: Endpoint, destination: Endpoint) -> None:
            try:
                flow = self._flow(
                    source,
                    destination,
                    kind=kind,
                    case_id=f"{prefix}-{source.logical_name}-{destination.logical_name}",
                    direction=direction,
                    streams=self.config.iperf.streams,
                    start_event=start,
                    ready=ready,
                )
                with lock:
                    found.append(flow)
            except BaseException as exc:  # noqa: BLE001 - collected and re-raised
                errors.append(exc)
                try:
                    ready.abort()
                except threading.BrokenBarrierError:
                    pass

        threads = [
            threading.Thread(target=worker, args=(source, destination), name=f"{prefix}-{source.logical_name}")
            for source, destination in pairs
        ]
        for thread in threads:
            thread.start()
        try:
            ready.wait(timeout=90)
        except threading.BrokenBarrierError as exc:
            start.set()
            for thread in threads:
                thread.join(timeout=5)
            raise ToolError("Simultaneous start barrier failed") from exc
        self.notify("STARTING...")
        for count in (3, 2, 1):
            self.notify(str(count))
            time.sleep(1)
        self.notify("GO")
        start.set()
        for thread in threads:
            thread.join()
        if errors:
            raise ToolError(str(errors[0]))
        for flow in found:
            if flow.mbps_avg is None and flow.reasons:
                self.notify(f"SIN MEDICION {flow.source} → {flow.destination}: {flow.reasons[0]}")
                code = str(flow.reasons[0]).split(":", 1)[0].split()[0]
                if code in {
                    "BIND_ERROR",
                    "FAIL_NO_TRAFFIC",
                    "UNIT_POWER_OFF_OR_REBOOT",
                    "EHOSTUNREACH",
                    "ECONNREFUSED",
                    "ECONNRESET",
                }:
                    self.notify(f"STATE {flow.source} {code} FAIL {flow.reasons[0]}")
        return skipped + guarded + found

    def _carrier_down(self, names: list[str]) -> None:
        self.notify("TEST_ABORTED_BY_CARRIER")
        for name in names:
            self.notify(
                f"STATE {name} TEST_ABORTED_BY_CARRIER FAIL "
                "FALLA: Caída de Enlace / Reinicio del DUT"
            )

    def _flow(
        self,
        source: Endpoint,
        destination: Endpoint,
        *,
        kind: str,
        case_id: str,
        direction: str,
        streams: int,
        start_event: threading.Event | None = None,
        ready: threading.Barrier | None = None,
    ) -> FlowResult:
        assert self.audit is not None
        return measure_flow(
            executor=self.executor,
            audit=self.audit,
            source=source,
            destination=destination,
            profile=self.config.acceptance(),
            duration=self.config.iperf.duration_seconds,
            streams=streams,
            kind=kind,
            case_id=case_id,
            direction=direction,
            long_duration=self.config.iperf.duration_seconds > self.config.long_duration_seconds,
            start_event=start_event,
            ready=ready,
            on_carrier_down=self._carrier_down,
        )

    def _can_iperf(self, endpoint: Endpoint) -> bool:
        return iperf_allowed(
            physical=endpoint.physical_verdict,
            address=endpoint.address,
            dhcp_verdict=endpoint.dhcp.verdict if endpoint.dhcp else None,
            static=self.config.network_mode == "static",
        )

    def _blocked_reason(self, endpoint: Endpoint) -> str:
        if endpoint.failure_reason:
            return endpoint.failure_reason
        if endpoint.physical_verdict == Verdict.FAIL:
            return failure_text(endpoint.physical_reasons, "Link check failed")
        if endpoint.dhcp is not None and endpoint.dhcp.verdict == Verdict.SKIPPED:
            return failure_text(endpoint.dhcp.reasons, "DHCP skipped")
        if endpoint.dhcp is not None and endpoint.dhcp.verdict == Verdict.FAIL:
            return endpoint.dhcp.message or "DHCP failed"
        if not endpoint.address:
            return "DHCP failed"
        return "iperf3 skipped"

    def _skip_dhcp(self, endpoint: Endpoint) -> None:
        reason = failure_text(endpoint.physical_reasons, endpoint.failure_reason or "Link check failed")
        endpoint.failure_reason = reason
        endpoint.stage = "RESULT"
        endpoint.iperf_state = Verdict.SKIPPED
        endpoint.dhcp = DhcpFacts(
            success=False,
            verdict=Verdict.SKIPPED,
            message=f"SKIPPED: {reason}",
            reasons=[f"DHCP SKIPPED: {reason}"],
        )
        self.notify(f"STATE {endpoint.logical_name} DHCP SKIPPED {reason}")
        self.notify(f"STATE {endpoint.logical_name} IPERF3 NOT_RUN {reason}")
        self.notify(f"PORT {endpoint.logical_name} dhcp skipped")
        if self.audit is not None:
            self.audit.write_raw(f"dhcp/{endpoint.logical_name}.json", endpoint.dhcp.model_dump_json(indent=2))

    def _note_iperf_skipped(self, name: str, reason: str) -> None:
        if name in self._iperf_noted:
            return
        self._iperf_noted.add(name)
        self.notify(f"STATE {name} IPERF3 NOT_RUN {reason}")

    def _not_run_flow(
        self,
        source: Endpoint,
        destination: Endpoint,
        *,
        kind: str,
        case_id: str,
        direction: str,
        reason: str,
    ) -> FlowResult:
        if not self._can_iperf(source):
            self._note_iperf_skipped(source.logical_name, self._blocked_reason(source))
        if not self._can_iperf(destination):
            self._note_iperf_skipped(destination.logical_name, self._blocked_reason(destination))
        return FlowResult(
            case_id=case_id,
            kind=kind,
            source=source.logical_name,
            destination=destination.logical_name,
            direction=direction,
            requested_duration_s=float(self.config.iperf.duration_seconds),
            mbps_avg=None,
            verdict=Verdict.SKIPPED,
            reasons=[f"iperf3 NOT RUN: {reason}"],
            source_ip=source.address,
            destination_ip=destination.address,
        )

    def _snapshot_ds_errors(self) -> None:
        """Read the DS uncorrectable sum before traffic. The later portal read is the second sample."""
        if self.config.dut.type != "emta":
            return
        username = os.environ.get(USER_ENV, "").strip()
        password = os.environ.get(PASSWORD_ENV, "")
        if not username or not password:
            self.notify("DS UNCORRECTABLES BEFORE omitido: falta la clave del portal")
            return
        endpoint = next(
            (
                item
                for item in self.endpoints
                if item.address and item.namespace and not is_link_local(item.address) and item.gateway
            ),
            None,
        )
        if endpoint is None or not allowed_host(endpoint.gateway or ""):
            self.notify("DS UNCORRECTABLES BEFORE omitido: ningún LAN tiene IP del equipo")
            return
        try:
            result = self.executor.run(
                [sys.executable, "-m", "tc_net_cert.dut.huawei"],
                namespace=endpoint.namespace,
                input_text=json.dumps(
                    {
                        "host": endpoint.gateway,
                        "username": username,
                        "password": password,
                        "counters": True,
                    }
                ),
                timeout=20,
            )
            payload = json.loads(result.stdout or "{}")
        except Exception:
            self.notify("DS UNCORRECTABLES BEFORE no se pudo leer")
            return
        total = payload.get("ds_uncorrectables") if isinstance(payload, dict) else None
        if isinstance(total, int) and total >= 0:
            self._ds_uncorrectable_before = total
            self.notify(f"DS UNCORRECTABLES BEFORE {total}")
        else:
            self.notify("DS UNCORRECTABLES BEFORE sin contador")

    def _apply_uncorrectable_delta(self, report: CertificationReport) -> None:
        from tc_net_cert.dut.broadcom import uncorrectable_total

        before = self._ds_uncorrectable_before
        after = uncorrectable_total(report.dut.ds_rows)
        if before is None or after is None or after < before:
            return
        report.dut.ds_uncorrectable_delta = str(after - before)
        self.notify(f"DS UNCORRECTABLES DELTA {report.dut.ds_uncorrectable_delta}")

    def _qos(self) -> list[FlowResult]:
        """Jitter is the ping mdev. Packet loss is the lost percent. Neither is invented."""
        ready = [item for item in self.endpoints if self._can_iperf(item) and item.address]
        if len(ready) < 2:
            self.notify("JITTER y PACKET LOSS no iniciaron: hacen falta dos LAN con IP")
            return []
        self.notify("JITTER y PACKET LOSS: ping entre los LAN")
        flows: list[FlowResult] = []
        for index, source in enumerate(ready):
            dest = ready[(index + 1) % len(ready)]
            self.notify(f"QOS {source.logical_name} → {dest.logical_name}")
            ping = ping_target(self.executor, source, dest.address, 20, 1, interval=0.2)
            ping.reasons = [f"{source.logical_name} → {dest.logical_name}"]
            if ping.rtt_mdev_ms is None or ping.loss_percent is None:
                ping.verdict = Verdict.SKIPPED
                self.notify(f"QOS {source.logical_name} sin respuesta")
            else:
                ping.verdict = Verdict.PASS
                self.notify(
                    f"QOS {source.logical_name} jitter {ping.rtt_mdev_ms:.2f} ms, pérdida {ping.loss_percent:.2f}%"
                )
            flows.append(
                FlowResult(
                    case_id=f"QOS-{source.logical_name}-{dest.logical_name}",
                    kind="qos",
                    source=source.logical_name,
                    destination=dest.logical_name,
                    direction="forward",
                    streams=0,
                    ping=ping,
                    verdict=ping.verdict,
                    reasons=list(ping.reasons),
                    source_ip=source.address,
                    destination_ip=dest.address,
                )
            )
        measured = [item.ping for item in flows if item.ping and item.ping.rtt_mdev_ms is not None and item.ping.loss_percent is not None]
        if measured:
            jitter = max(item.rtt_mdev_ms for item in measured if item.rtt_mdev_ms is not None)
            loss = max(item.loss_percent for item in measured if item.loss_percent is not None)
            self.notify(f"JITTER {jitter:.2f} ms")
            self.notify(f"PACKET LOSS {loss:.2f}%")
        return flows

    def _start_wifi_server(self) -> None:
        """Keep one LAN iperf server up so the station radio can measure the DUT Wi-Fi."""
        from tc_net_cert.wifi.throughput import choose_lan

        server = choose_lan(self.endpoints)
        if server is None or not self._can_iperf(server) or self.audit is None:
            self.notify("WIFI servidor no iniciado: falta LAN2 o LAN3 con IP del equipo")
            return
        server.stage = "IPERF3"
        self.notify(f"WIFI servidor en {server.logical_name} {server.address}")
        self.pool.start(server)
        self.audit.record_command(
            CommandResult(
                argv=["iperf3", "-s", "-B", server.address, "-p", str(server.iperf_port)],
                exit_code=0,
                stdout="",
                stderr="",
                duration_s=0,
                namespace=server.namespace,
                interface=server.interface,
                command=f"iperf3 -s -B {server.address} -p {server.iperf_port}",
                started_at=datetime.now(timezone.utc).isoformat(),
            )
        )

    def _wifi_bands(self) -> list[FlowResult]:
        from tc_net_cert.wifi.throughput import PSK_24, PSK_50, READING_ENV, measure_bands, stored_env

        reading = os.environ.get(READING_ENV, "").strip()
        stored = stored_env(Path(reading) if reading else None)
        self._wifi_passwords = {
            "wifi_password_24": os.environ.get(PSK_24, "") or stored.get(PSK_24, ""),
            "wifi_password_50": os.environ.get(PSK_50, "") or stored.get(PSK_50, ""),
        }
        try:
            seconds = int(self.config.iperf.duration_seconds)
            scope = self._report_bench_scope()
            if scope == "A+B":
                bench = "A+B"
                lan_hint = "LAN2/LAN3 (A) y LAN6/LAN7 (B)"
            else:
                bench = self.bench_id or "A"
                lan_hint = "LAN6 o LAN7" if bench == "B" else "LAN2 o LAN3"
            self.notify(
                f"WIFI banco {bench}: un solo radio, primero 5 GHz y después 2.4, "
                f"{seconds} s contra {lan_hint}"
            )
            return measure_bands(self.endpoints, self.notify, audit=self.audit, seconds=seconds)
        except Exception:
            self.notify("WIFI NOT RUN: la medición Wi-Fi se interrumpió")
            return []

    def _remember_wifi(self, interfaces) -> None:
        for record in interfaces:
            if record.is_wireless or record.exclusion_reason == "wireless":
                self.wifi_interface = record.name
                self.wifi_address = record.addresses[0] if record.addresses else ""
                self.wifi_state = record.operstate or ""
                self.notify(
                    f"WIFI {record.name} EXCLUDED state={self.wifi_state or 'unknown'} address={self.wifi_address or 'none'}"
                )
                return

    def _summarize_directions(self) -> None:
        kinds = {"throughput", "throughput-single", "simultaneous", "bidirectional"}
        for endpoint in self.endpoints:
            measured = [
                flow
                for flow in self.flows
                if endpoint.logical_name in {flow.source, flow.destination}
                and flow.kind in kinds
                and flow.mbps_avg is not None
                and flow.verdict not in {Verdict.SKIPPED, Verdict.NOT_RUN}
            ]
            tx = [flow.mbps_avg for flow in measured if flow.source == endpoint.logical_name and flow.mbps_avg is not None]
            rx = [flow.mbps_avg for flow in measured if flow.destination == endpoint.logical_name and flow.mbps_avg is not None]
            endpoint.tx_mbps = (sum(tx) / len(tx)) if tx else None
            endpoint.rx_mbps = (sum(rx) / len(rx)) if rx else None
            if not measured:
                failed = [
                    flow
                    for flow in self.flows
                    if endpoint.logical_name in {flow.source, flow.destination}
                    and flow.kind in kinds
                    and flow.verdict == Verdict.FAIL
                ]
                if failed:
                    endpoint.iperf_state = Verdict.FAIL
                    if not endpoint.failure_reason:
                        endpoint.failure_reason = failure_text(failed[0].reasons, "iperf3 failed")
                elif endpoint.iperf_state == Verdict.NOT_RUN:
                    endpoint.iperf_state = Verdict.NOT_RUN
                endpoint.iperf_duration_s = None
                continue
            endpoint.iperf_state = worst_verdict([flow.verdict for flow in measured])
            durations = [flow.actual_duration_s for flow in measured if flow.actual_duration_s is not None]
            endpoint.iperf_duration_s = max(durations) if durations else float(self.config.iperf.duration_seconds)
            if endpoint.iperf_state != Verdict.PASS and not endpoint.failure_reason:
                failed = [flow for flow in measured if flow.verdict == Verdict.FAIL]
                endpoint.failure_reason = failure_text(
                    (failed or measured)[0].reasons,
                    "iperf3 failed",
                )
            endpoint.stage = "RESULT"
        wants_iperf = bool(self.stages & {"throughput", "simultaneous", "bidirectional"})
        for endpoint in self.endpoints:
            dhcp_failed = endpoint.dhcp is not None and endpoint.dhcp.verdict == Verdict.FAIL
            if endpoint.physical_verdict == Verdict.FAIL or dhcp_failed:
                self.notify(f"STATE {endpoint.logical_name} RESULT FAIL {endpoint.failure_reason}")
            elif wants_iperf and endpoint.tx_mbps is None and endpoint.rx_mbps is None:
                state = endpoint.iperf_state if endpoint.iperf_state == Verdict.FAIL else Verdict.FAIL
                if not endpoint.failure_reason:
                    endpoint.failure_reason = "iperf3 NOT RUN"
                    endpoint.iperf_state = Verdict.SKIPPED
                    state = Verdict.FAIL
                self.notify(
                    f"STATE {endpoint.logical_name} RESULT {state.value} {endpoint.failure_reason}"
                )
            elif endpoint.iperf_state == Verdict.PASS or (
                not wants_iperf and endpoint.physical_verdict == Verdict.PASS
            ):
                self.notify(f"STATE {endpoint.logical_name} RESULT PASS")
            else:
                self.notify(
                    f"STATE {endpoint.logical_name} RESULT {endpoint.iperf_state.value} {endpoint.failure_reason}".rstrip()
                )

    def _collect_dut_identity(self, report: CertificationReport) -> None:
        """After throughput, read SN and MAC from the ONT through a test namespace."""
        report.job = self.job
        _apply_known_identity(report)
        _seed_bench_duts(report)
        default_user = os.environ.get(USER_ENV, "").strip()
        default_password = os.environ.pop(PASSWORD_ENV, "")
        default_host = os.environ.pop(HOST_ENV, "").strip()
        portals_file = os.environ.pop(PORTALS_ENV, "").strip()
        profiles = load_portals(Path(portals_file)) if portals_file else {}
        bench_cred = _load_bench_credentials_env()
        scope = self._report_bench_scope()
        bench_ids: list[str]
        if scope == "A+B":
            bench_ids = ["A", "B"]
        elif self.bench_id in {"A", "B"}:
            bench_ids = [self.bench_id]
        else:
            bench_ids = ["A"]

        if not bench_cred and (not default_user or not default_password) and not profiles:
            report.dut_identity_note = "No se leyó el SN ni la MAC: falta el usuario o la contraseña del ONT."
            self.notify("DUT IDENTITY SKIPPED missing credentials")
            return

        failures: list[str] = []
        for bench_id in bench_ids:
            endpoint = self._identity_endpoint_for_bench(bench_id)
            if endpoint is None:
                failures.append(f"Banco {bench_id}: sin IP en el portal de prueba.")
                self.notify(f"DUT IDENTITY BANCO {bench_id} SKIPPED no address")
                continue
            user, password, host = _bench_portal_credentials(
                bench_id,
                bench_cred,
                default_user=default_user,
                default_password=default_password,
                default_host=default_host,
            )
            if (not user or not password) and not profiles:
                failures.append(f"Banco {bench_id}: falta usuario o contraseña del ONT.")
                self.notify(f"DUT IDENTITY BANCO {bench_id} SKIPPED missing credentials")
                continue
            dut = report.bench_duts.setdefault(bench_id, DutIdentity(type=report.dut.type))
            kept = bool(dut.serial)
            secret_file = Path(tempfile.mkstemp(prefix=f"tc-wifi-{bench_id}-")[1])
            secret_file.chmod(0o600)
            portal_host = host if allowed_host(host) else (
                endpoint.gateway if allowed_host(endpoint.gateway or "") else "192.168.1.1"
            )
            self.notify(f"DUT IDENTITY BANCO {bench_id} REQUEST {endpoint.logical_name} {portal_host}")
            payload = self._query_dut_portal(
                endpoint,
                username=user,
                password=password,
                host=portal_host,
                profiles=profiles,
                secret_file=secret_file,
            )
            detected = str(payload.get("model") or "")
            if payload.get("brand"):
                dut.brand = str(payload["brand"])
            if detected:
                dut.model = detected
            if payload.get("ok") is True and payload.get("serial"):
                dut.serial = str(payload["serial"])
                if payload.get("mac"):
                    dut.mac = str(payload["mac"])
                if payload.get("software"):
                    dut.firmware = str(payload["software"])
                if payload.get("hardware"):
                    dut.hardware = str(payload["hardware"])
                _copy_inventory_into(dut, payload)
                wifi_keys = _wifi_keys(secret_file)
                if wifi_keys.get("wifi_password_24"):
                    dut.wifi_password_24 = wifi_keys["wifi_password_24"]
                if wifi_keys.get("wifi_password_50"):
                    dut.wifi_password_50 = wifi_keys["wifi_password_50"]
                mac_note = f" MAC={dut.mac}" if dut.mac else ""
                model_note = f" MODEL={detected}" if detected else ""
                software_note = f" SW={dut.firmware}" if dut.firmware else ""
                hardware_note = f" HW={payload.get('hardware')}" if payload.get("hardware") else ""
                self.notify(
                    f"DUT IDENTITY BANCO {bench_id} PASS SN={dut.serial}{mac_note}{model_note}{software_note}{hardware_note}"
                )
            elif kept:
                self.notify(
                    f"DUT IDENTITY BANCO {bench_id} KEPT SN={dut.serial} MAC={dut.mac} "
                    "el portal no respondió al terminar"
                )
            else:
                note = str(payload.get("error") or "No se leyó el SN ni la MAC.")
                if password and password in note:
                    note = "No se leyó el SN ni la MAC."
                failures.append(f"Banco {bench_id}: {note[:120]}")
                model_note = f" MODEL={detected}" if detected else ""
                self.notify(f"DUT IDENTITY BANCO {bench_id} FAIL{model_note} {note[:180]}")
            secret_file.unlink(missing_ok=True)

        if "A" in report.bench_duts:
            report.dut = report.bench_duts["A"].model_copy(deep=True)
        elif bench_ids == ["B"] and "B" in report.bench_duts:
            report.dut = report.bench_duts["B"].model_copy(deep=True)

        self._apply_uncorrectable_delta(report)
        if report.dut.wifi_password_24 or report.dut.wifi_password_50:
            pass
        elif "A" in report.bench_duts:
            _apply_wifi_passwords(report, {
                "wifi_password_24": report.bench_duts["A"].wifi_password_24,
                "wifi_password_50": report.bench_duts["A"].wifi_password_50,
            })

        if report.dut.serial:
            report.dut_identity_note = ""
            if failures:
                report.dut_identity_note = " ".join(failures)[:240]
            return
        report.dut_identity_note = (" ".join(failures) or "No se leyó el SN ni la MAC.")[:240]

    def _identity_endpoint_for_bench(self, bench_id: str) -> Endpoint | None:
        names = set(bench_port_list(self.config, bench_id))
        ordered = bench_port_list(self.config, bench_id)
        for logical in ordered:
            for item in self.endpoints:
                if item.logical_name != logical or logical not in names:
                    continue
                if (
                    item.address
                    and item.namespace
                    and not is_link_local(item.address)
                    and (item.gateway or item.address.startswith("192.168.1."))
                ):
                    return item
        return None

    def _query_dut_portal(
        self,
        endpoint: Endpoint,
        *,
        username: str,
        password: str,
        host: str,
        profiles: dict,
        secret_file: Path,
    ) -> dict:
        try:
            result = self.executor.run(
                [sys.executable, "-m", "tc_net_cert.dut.huawei"],
                namespace=endpoint.namespace,
                input_text=json.dumps(
                    {
                        "host": host,
                        "username": username,
                        "password": password,
                        "profiles": profiles,
                        "inventory": True,
                        "secret_path": str(secret_file),
                    }
                ),
                timeout=60,
            )
        except Exception:
            return {"ok": False, "error": "No se leyó el SN ni la MAC: no se pudo consultar el ONT."}
        payload = _identity_payload(result.stdout, password)
        error = str(payload.get("error") or "")
        for saved in profiles.values():
            secret = saved.get("password") or ""
            if secret and secret in error:
                payload["error"] = "No se leyó el SN ni la MAC."
                break
        return payload

    def _finalize_bench_orders(self, report: CertificationReport) -> dict[str, str]:
        runs_root = runs_directory(self.config)
        for bench_id in ("A", "B"):
            dut = report.bench_duts.get(bench_id)
            if dut is None or not dut.serial:
                continue
            if bench_id in self.bench_run_ids:
                continue
            run_id, run_dir, reused = resolve_order_for_serial(runs_root, dut.serial)
            self.bench_run_ids[bench_id] = run_id
            self.bench_run_dirs[bench_id] = run_dir
            if reused:
                self.notify(f"ORDEN {run_id} sigue — banco {bench_id} (SN {dut.serial}).")
            else:
                self.notify(f"ORDEN {run_id} nueva — banco {bench_id}.")
        return dict(self.bench_run_ids)

    def _mirror_bench_reports(self, report: CertificationReport) -> None:
        if self.run_dir is None:
            return
        primary = self.run_dir.resolve()
        session_id = report.run_id
        for bench_id in ("B", "A"):
            target_dir = self.bench_run_dirs.get(bench_id)
            if target_dir is None:
                continue
            if target_dir.resolve() == primary:
                continue
            dut = report.bench_duts.get(bench_id)
            if dut is None or not dut.serial:
                continue
            order_id = self.bench_run_ids.get(bench_id)
            if not order_id:
                continue
            from tc_net_cert.reporting.bench_slice import narrow_report_to_bench

            slice_report = narrow_report_to_bench(report, bench_id)
            slice_report.run_id = order_id
            slice_report.dual_session_run = session_id
            slice_report.bench_orders = dict(report.bench_orders)
            render_reports(slice_report, target_dir)
            from tc_net_cert.storage.dut_identity import store_identity

            store_identity(
                serial=dut.serial,
                mac=dut.mac,
                brand=dut.brand,
                model=dut.model,
            )

    def _stop_iperf(self) -> None:
        self.pool.stop()

    def _release_dhcp(self) -> None:
        if self.config.network_mode != "dhcp":
            return
        for endpoint in self.endpoints:
            release_lease(self.executor, endpoint, self.config.dhcp.backend)

    def _restore(self) -> None:
        missing = self.manager.restore_moved(self.active, self.moved)
        if missing:
            raise ToolError("Interfaces still missing after cleanup: " + ", ".join(missing))

    def _restore_management_route(self) -> None:
        ifaces = [endpoint.interface for endpoint in self.endpoints]
        restore_management_routing(
            self.executor,
            wifi_iface=self.wifi_interface,
            ethernet_ifaces=ifaces,
        )
        if self.wifi_interface:
            self.notify(f"MANAGEMENT ROUTE restored via {self.wifi_interface}")

    def _persist_active(self) -> None:
        path = session_state_path()
        path.write_text(self.active.model_dump_json(indent=2), encoding="utf-8")
        self._wrote_state = True

    def _finish(self, report: CertificationReport) -> None:
        assert self.audit is not None and self.run_dir is not None
        token = current_audit.set(self.audit)
        try:
            self._collect_dut_identity(report)
            errors = self.cleanup.run()
            remaining = [name for name in self.manager.list_namespaces() if name in self.active.namespaces]
            reasons = list(errors)
            if remaining:
                reasons.append("Namespaces still present: " + ", ".join(remaining))
            report.cleanup = StationCleanup(
                verdict=Verdict.FAIL if reasons else Verdict.PASS,
                namespaces_after=remaining,
                reasons=reasons or ["Namespaces removed and NICs returned to the root namespace"],
            )
            if report.cleanup.verdict == Verdict.PASS and self._wrote_state:
                path = session_state_path()
                if path.exists():
                    path.unlink()
            listing = self.executor.run(["ip", "netns", "list"], timeout=10)
            self.audit.write_raw("system/netns-after.txt", listing.stdout)
        finally:
            self.audit.close()
            current_audit.reset(token)
        self._summarize_directions()
        report.wifi_interface = self.wifi_interface
        report.wifi_address = self.wifi_address
        report.wifi_state = self.wifi_state
        report.wifi_excluded = True
        report.bench_scope = self._report_bench_scope()
        finished_utc, finished_local = _stamps()
        report.finished_at_utc = finished_utc
        report.finished_at_local = finished_local
        remembered = getattr(self, "_wifi_passwords", {})
        if remembered.get("wifi_password_24") and not report.dut.wifi_password_24:
            report.dut.wifi_password_24 = remembered["wifi_password_24"]
        if remembered.get("wifi_password_50") and not report.dut.wifi_password_50:
            report.dut.wifi_password_50 = remembered["wifi_password_50"]
        assemble_report(report, self.endpoints, self.flows, self.config.acceptance())
        if self.abort:
            report.global_reasons.insert(0, self.abort)
            report.global_verdict = worst_verdict([report.global_verdict, Verdict.FAIL])
        report.bench_orders = self._finalize_bench_orders(report)
        report.dual_session_run = self.bench_run_ids.get("A") or report.run_id
        from tc_net_cert.reporting.bench_slice import narrow_report_to_bench

        if report.bench_scope == "A+B":
            scoped_a = narrow_report_to_bench(report, "A")
            scoped_a.run_id = self.bench_run_ids.get("A", report.run_id)
            scoped_a.bench_orders = dict(report.bench_orders)
            scoped_a.dual_session_run = ""
            render_reports(scoped_a, self.run_dir)
            self._mirror_bench_reports(report)
        else:
            render_reports(report, self.run_dir)
            self._mirror_bench_reports(report)
        if report.dut.serial:
            from tc_net_cert.storage.dut_identity import store_identity

            store_identity(
                serial=report.dut.serial,
                mac=report.dut.mac,
                brand=report.dut.brand,
                model=report.dut.model,
            )
        stored = _store_run(
            report,
            {
                "wifi_password_24": report.dut.wifi_password_24,
                "wifi_password_50": report.dut.wifi_password_50,
            },
        )
        if stored:
            self.notify(f"SUPABASE FAIL {stored}")
        elif _supabase_configured():
            self.notify(f"SUPABASE OK {report.run_id}")
        seal_run(
            self.run_dir,
            {
                "run_id": report.run_id,
                "tool_version": report.tool_version,
                "schema_version": report.schema_version,
                "config_hash": report.config_hash,
                "network_mode": report.network_mode.value,
                "global_verdict": report.global_verdict.value,
                "started_at_utc": report.started_at_utc,
                "finished_at_utc": report.finished_at_utc,
                "operator": report.operator,
                "station": report.station,
                "job": report.job,
                "dut": report.dut.model_dump(mode="json"),
                "host": report.host.model_dump(mode="json"),
            },
        )
        try:
            upsert_run(
                runs_directory(self.config),
                run_id=report.run_id,
                started_at=report.started_at_utc,
                finished_at=report.finished_at_utc,
                verdict=report.global_verdict.value,
                dut_type=report.dut.type,
                dut_model=report.dut.model,
                dut_serial=report.dut.serial,
                station=report.station,
                operator=report.operator,
                path=str(self.run_dir),
            )
        except OSError as exc:
            self.notify(f"SQLite index was not updated: {exc}")
        self.notify(f"Run folder: {self.run_dir}")
        self.notify(f"FINAL RESULT: {report.global_verdict.value}")


def _channel_rows(value: object) -> list[dict[str, str]]:
    """Portal channel tables only. Each cell stays a short string."""
    if not isinstance(value, list):
        return []
    keys = (
        "channel",
        "state",
        "modulation",
        "channel_id",
        "frequency",
        "power",
        "snr",
        "correctable",
        "uncorrectable",
        "symbol_rate",
        "bandwidth",
    )
    rows = []
    for item in value[:64]:
        if not isinstance(item, dict):
            continue
        rows.append({key: str(item.get(key) or "")[:40] for key in keys})
    return rows


def _wan_rows(value: object) -> list[dict[str, str]]:
    """Provisioned WAN rows. Password fields are not in this allowlist."""
    if not isinstance(value, list):
        return []
    keys = ("name", "service", "status", "mode", "vlan", "address", "gateway", "nat", "uptime")
    rows = []
    for item in value[:8]:
        if not isinstance(item, dict):
            continue
        rows.append({key: str(item.get(key) or "")[:40] for key in keys})
    return rows


def _load_bench_credentials_env() -> dict[str, dict]:
    raw = os.environ.get(BENCH_CREDENTIALS_ENV, "").strip()
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(payload, dict):
        return {}
    result: dict[str, dict] = {}
    for key, value in payload.items():
        if isinstance(value, dict):
            result[str(key).upper()] = value
    return result


def _bench_portal_credentials(
    bench_id: str,
    bench_cred: dict[str, dict],
    *,
    default_user: str,
    default_password: str,
    default_host: str,
) -> tuple[str, str, str]:
    block = bench_cred.get(bench_id.upper(), {})
    user = str(block.get("dut_user") or default_user).strip()
    password = block.get("dut_password") if isinstance(block.get("dut_password"), str) else default_password
    host = str(block.get("portal_host") or default_host).strip()
    return user, password, host


def _seed_bench_duts(report: CertificationReport) -> None:
    from tc_net_cert.storage.bench_identity import load_bench_inventory

    for bench_id in ("A", "B"):
        inv = load_bench_inventory(bench_id)
        if not str(inv.get("serial") or "").strip():
            continue
        dut = DutIdentity(type=report.dut.type or str(inv.get("family") or "ont"))
        if inv.get("brand"):
            dut.brand = str(inv["brand"])
        if inv.get("model"):
            dut.model = str(inv["model"])
        dut.serial = str(inv.get("serial") or "")
        dut.mac = str(inv.get("mac") or "")
        if inv.get("software"):
            dut.firmware = str(inv["software"])
        if inv.get("hardware"):
            dut.hardware = str(inv["hardware"])
        if inv.get("device_type"):
            dut.device_type = str(inv["device_type"])
        report.bench_duts[bench_id] = dut
    if "A" in report.bench_duts and not report.dut.serial:
        report.dut = report.bench_duts["A"].model_copy(deep=True)


def _apply_known_identity(report: CertificationReport) -> None:
    """Keep the SN and MAC already read on the panel if the portal is down at the end."""
    serial = os.environ.get(SERIAL_ENV, "").strip()
    mac = os.environ.get(MAC_ENV, "").strip()
    brand = os.environ.get(BRAND_ENV, "").strip()
    model = os.environ.get(MODEL_ENV, "").strip()
    if serial and len(serial) <= 40 and " " not in serial:
        report.dut.serial = serial
    if mac and len(mac) <= 17:
        report.dut.mac = mac
    if brand and len(brand) <= 40:
        report.dut.brand = brand
    if model and len(model) <= 40:
        report.dut.model = model


def _identity_payload(stdout: str, password: str) -> dict:
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return {"ok": False, "error": "No se leyó el SN ni la MAC: el ONT no respondió."}
    if not isinstance(payload, dict):
        return {"ok": False, "error": "No se leyó el SN ni la MAC: el ONT no respondió."}
    error = str(payload.get("error") or "")
    if password and password in error:
        error = "No se leyó el SN ni la MAC."
    return {
        "ok": payload.get("ok") is True,
        "serial": str(payload.get("serial") or ""),
        "mac": str(payload.get("mac") or ""),
        "model": str(payload.get("model") or ""),
        "brand": str(payload.get("brand") or ""),
        "software": str(payload.get("software") or ""),
        "hardware": str(payload.get("hardware") or ""),
        "device_type": str(payload.get("device_type") or ""),
        "tx_power": str(payload.get("tx_power") or ""),
        "rx_power": str(payload.get("rx_power") or ""),
        "voltage": str(payload.get("voltage") or ""),
        "bias": str(payload.get("bias") or ""),
        "temperature": str(payload.get("temperature") or ""),
        "ont_state": str(payload.get("ont_state") or ""),
        "ont_id": str(payload.get("ont_id") or "")[:16],
        "pon_mode": str(payload.get("pon_mode") or "")[:16],
        "optic_link": str(payload.get("optic_link") or "")[:16],
        "los_status": str(payload.get("los_status") or "")[:16],
        "tx_wavelength": str(payload.get("tx_wavelength") or "")[:16],
        "rx_wavelength": str(payload.get("rx_wavelength") or "")[:16],
        "optic_vendor": str(payload.get("optic_vendor") or "")[:24],
        "optic_module": str(payload.get("optic_module") or "")[:32],
        "optic_distance": str(payload.get("optic_distance") or "")[:16],
        "vendor_id": str(payload.get("vendor_id") or "")[:16],
        "release_time": str(payload.get("release_time") or "")[:32],
        "description": str(payload.get("description") or "")[:120],
        "registration": str(payload.get("registration") or "")[:80],
        "olt_service": str(payload.get("olt_service") or "")[:80],
        "ems_status": str(payload.get("ems_status") or "")[:80],
        "acs_status": str(payload.get("acs_status") or "")[:80],
        "wan_rows": _wan_rows(payload.get("wan_rows")),
        "wlan_status": str(payload.get("wlan_status") or ""),
        "wlan_channel": str(payload.get("wlan_channel") or ""),
        "ssid_24": str(payload.get("ssid_24") or ""),
        "ssid_50": str(payload.get("ssid_50") or ""),
        "docsis_status": str(payload.get("docsis_status") or ""),
        "ds_channels": str(payload.get("ds_channels") or ""),
        "ds_power": str(payload.get("ds_power") or ""),
        "us_channels": str(payload.get("us_channels") or ""),
        "us_power": str(payload.get("us_power") or ""),
        "mer": str(payload.get("mer") or ""),
        "ds_correctable_total": str(payload.get("ds_correctable_total") or "")[:16],
        "ds_uncorrectable_total": str(payload.get("ds_uncorrectable_total") or "")[:16],
        "uptime": str(payload.get("uptime") or "")[:40],
        "cm_ip": str(payload.get("cm_ip") or "")[:40],
        "mta_dhcp": str(payload.get("mta_dhcp") or "")[:40],
        "wan_dhcp": str(payload.get("wan_dhcp") or "")[:40],
        "ds_rows": _channel_rows(payload.get("ds_rows")),
        "us_rows": _channel_rows(payload.get("us_rows")),
        "error": error,
    }


def _copy_inventory_into(dut: DutIdentity, payload: dict) -> None:
    dut.device_type = str(payload.get("device_type") or dut.model)
    dut.optical_tx = str(payload.get("tx_power") or "")
    dut.optical_rx = str(payload.get("rx_power") or "")
    dut.optical_voltage = str(payload.get("voltage") or "")
    dut.optical_bias = str(payload.get("bias") or "")
    dut.optical_temperature = str(payload.get("temperature") or "")
    dut.ont_state = str(payload.get("ont_state") or "")
    dut.ont_id = str(payload.get("ont_id") or "")
    dut.pon_mode = str(payload.get("pon_mode") or "")
    dut.optic_link = str(payload.get("optic_link") or "")
    dut.los_status = str(payload.get("los_status") or "")
    dut.tx_wavelength = str(payload.get("tx_wavelength") or "")
    dut.rx_wavelength = str(payload.get("rx_wavelength") or "")
    dut.optic_vendor = str(payload.get("optic_vendor") or "")
    dut.optic_module = str(payload.get("optic_module") or "")
    dut.optic_distance = str(payload.get("optic_distance") or "")
    dut.vendor_id = str(payload.get("vendor_id") or "")
    dut.release_time = str(payload.get("release_time") or "")
    dut.description = str(payload.get("description") or "")
    dut.registration = str(payload.get("registration") or "")
    dut.olt_service = str(payload.get("olt_service") or "")
    dut.ems_status = str(payload.get("ems_status") or "")
    dut.acs_status = str(payload.get("acs_status") or "")
    dut.wan_rows = [WanService.model_validate(item) for item in _wan_rows(payload.get("wan_rows"))]
    dut.wlan_status = str(payload.get("wlan_status") or "")
    dut.wlan_channel = str(payload.get("wlan_channel") or "")
    dut.ssid_24 = str(payload.get("ssid_24") or "")
    dut.ssid_50 = str(payload.get("ssid_50") or "")
    dut.docsis_status = str(payload.get("docsis_status") or "")
    dut.ds_channels = str(payload.get("ds_channels") or "")
    dut.ds_power = str(payload.get("ds_power") or "")
    dut.us_channels = str(payload.get("us_channels") or "")
    dut.us_power = str(payload.get("us_power") or "")
    dut.mer = str(payload.get("mer") or "")
    dut.ds_correctable_total = str(payload.get("ds_correctable_total") or "")
    dut.ds_uncorrectable_total = str(payload.get("ds_uncorrectable_total") or "")
    dut.uptime = str(payload.get("uptime") or "")
    dut.cm_ip = str(payload.get("cm_ip") or "")
    dut.mta_dhcp = str(payload.get("mta_dhcp") or "")
    dut.wan_dhcp = str(payload.get("wan_dhcp") or "")
    dut.ds_rows = [RfChannel.model_validate(item) for item in _channel_rows(payload.get("ds_rows"))]
    dut.us_rows = [RfChannel.model_validate(item) for item in _channel_rows(payload.get("us_rows"))]


def _copy_inventory(report: CertificationReport, payload: dict) -> None:
    _copy_inventory_into(report.dut, payload)


def _apply_wifi_passwords(report: CertificationReport, keys: dict[str, str]) -> None:
    """The portal key is part of the certificate. It is not written into the command log."""
    if keys.get("wifi_password_24"):
        report.dut.wifi_password_24 = keys["wifi_password_24"]
    if keys.get("wifi_password_50"):
        report.dut.wifi_password_50 = keys["wifi_password_50"]


def _wifi_keys(path: Path) -> dict[str, str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"wifi_password_24": "", "wifi_password_50": ""}
    if not isinstance(payload, dict):
        return {"wifi_password_24": "", "wifi_password_50": ""}
    return {
        "wifi_password_24": str(payload.get("wifi_password_24") or ""),
        "wifi_password_50": str(payload.get("wifi_password_50") or ""),
    }


def _supabase_configured() -> bool:
    from tc_net_cert.storage.supabase_runs import config_path as supabase_config_path

    explicit = os.environ.get("TC_NET_CERT_SUPABASE", "").strip()
    path = Path(explicit) if explicit else supabase_config_path()
    return path.is_file()


def _store_run(report: CertificationReport, keys: dict[str, str]) -> str:
    from tc_net_cert.storage.supabase_runs import config_path as supabase_config_path, forget_listed_run_ids, save_run

    explicit = os.environ.get("TC_NET_CERT_SUPABASE", "").strip()
    path = Path(explicit) if explicit else supabase_config_path()
    if not path.is_file():
        return ""

    flows = [
        {
            "source": flow.source,
            "destination": flow.destination,
            "kind": flow.kind,
            "mbps": flow.mbps_avg,
            "verdict": flow.verdict.value,
        }
        for flow in report.flows
        if flow.mbps_avg is not None or flow.kind == "wifi"
    ]
    error = save_run(
        path,
        {
            "run_id": report.run_id,
            "station": report.station,
            "operator": report.operator,
            "verdict": report.global_verdict.value,
            "model": report.dut.model,
            "device_type": report.dut.device_type or report.dut.type,
            "serial": report.dut.serial,
            "mac": report.dut.mac,
            "software_version": report.dut.firmware,
            "hardware_version": report.dut.hardware,
            "tx_optical_power": report.dut.optical_tx,
            "rx_optical_power": report.dut.optical_rx,
            "working_voltage": report.dut.optical_voltage,
            "bias_current": report.dut.optical_bias,
            "working_temperature": report.dut.optical_temperature,
            "wlan_status": report.dut.wlan_status,
            "wlan_channel": report.dut.wlan_channel,
            "ssid_24": report.dut.ssid_24,
            "ssid_50": report.dut.ssid_50,
            "wifi_password_24": keys.get("wifi_password_24") or "",
            "wifi_password_50": keys.get("wifi_password_50") or "",
            "throughput": flows,
        },
    )
    if not error:
        forget_listed_run_ids()
    return error


def exit_code(report: CertificationReport) -> int:
    if report.global_verdict == Verdict.PASS and report.cleanup.verdict == Verdict.FAIL:
        return 2
    if report.global_verdict == Verdict.PASS:
        return 0
    if report.global_verdict == Verdict.INCONCLUSIVE:
        return 3
    return 1


def _expand_stages(requested: set[str], config: AppConfig) -> set[str]:
    profile = config.dut_profile()
    if "all" in requested:
        stages = {"physical", "addressing"}
        if profile.test_gateway and (config.network_mode == "dhcp" or config.static_gateways):
            stages.add("gateway")
        if profile.test_cross_ports:
            stages.add("cross")
        if profile.throughput and profile.test_cross_ports:
            stages.add("throughput")
        if profile.simultaneous:
            stages.add("simultaneous")
        if profile.bidirectional:
            stages.add("bidirectional")
        return stages
    stages = set(requested)
    if "dhcp" in stages:
        stages.add("addressing")
    if "addressing" in stages:
        stages.add("physical")
    if stages & {"cross", "throughput", "simultaneous", "bidirectional", "gateway", "wifi"}:
        stages.add("addressing")
        stages.add("physical")
    return stages


def _stamps() -> tuple[str, str]:
    local = datetime.now().astimezone()
    return local.astimezone(timezone.utc).isoformat(), local.isoformat()
