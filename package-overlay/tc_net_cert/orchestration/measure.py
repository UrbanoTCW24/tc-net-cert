"""One directional iPerf3 measurement with before/after counters."""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import datetime, timezone

from tc_net_cert.assessment.bottleneck import assess_bottleneck
from tc_net_cert.assessment.verdict import judge_counters, judge_stability, judge_throughput
from tc_net_cert.audit.trail import AuditTrail
from tc_net_cert.core.executor import CommandResult, Executor
from tc_net_cert.dhcp.acquire import is_link_local
from tc_net_cert.core.models import AcceptanceProfile, Endpoint, ErrorDelta, FlowResult, HostSample, Verdict, worst_verdict
from tc_net_cert.ethtool.link import delta, dump_ethtool_statistics, read_link
from tc_net_cert.iperf.runner import run_client
from tc_net_cert.orchestration.guard import UnitWatch, classify_iperf_failure
from tc_net_cert.orchestration.interrupt import explain_cut, last_interval_second, port_down
from tc_net_cert.orchestration.monitor import HostSampler, LinkWatch


def measure_flow(
    *,
    executor: Executor,
    audit: AuditTrail,
    source: Endpoint,
    destination: Endpoint,
    profile: AcceptanceProfile,
    duration: int,
    streams: int,
    kind: str,
    case_id: str,
    direction: str,
    long_duration: bool,
    start_event: threading.Event | None = None,
    ready: threading.Barrier | None = None,
    on_carrier_down: Callable[[list[str]], None] | None = None,
) -> FlowResult:
    before_src = read_link(executor, source.interface, source.namespace)
    before_dst = read_link(executor, destination.interface, destination.namespace)
    audit.write_raw(
        f"ethtool/{case_id}.before.txt",
        dump_ethtool_statistics(executor, source.interface, source.namespace),
    )
    watch = LinkWatch(executor, source.interface, source.namespace)
    watch_dst = LinkWatch(executor, destination.interface, destination.namespace)
    sampler = HostSampler()
    watch.start()
    watch_dst.start()
    sampler.start()
    if ready is not None:
        ready.wait(timeout=60)
    if start_event is not None:
        start_event.wait(timeout=max(30, duration))
    watch_unit = UnitWatch(
        executor,
        [
            (source.logical_name, source.interface, source.namespace),
            (destination.logical_name, destination.interface, destination.namespace),
        ],
        on_down=on_carrier_down,
    )
    watch_unit.start()
    try:
        measurement, stdout, stderr, code = run_client(
            source,
            destination,
            duration=duration,
            streams=streams,
            timeout=duration + 45,
            cancel=watch_unit.cancel,
        )
    finally:
        unit_reason = watch_unit.stop()
    host: HostSample = sampler.stop()
    src_flaps = watch.stop()
    dst_flaps = watch_dst.stop()
    flaps = src_flaps + dst_flaps
    after_src = read_link(executor, source.interface, source.namespace)
    after_dst = read_link(executor, destination.interface, destination.namespace)
    audit.write_raw(f"iperf3/{case_id}.json", stdout or "{}\n")
    audit.write_raw(f"iperf3/{case_id}.stderr.txt", stderr)
    audit.write_raw(
        f"ethtool/{case_id}.after.txt",
        dump_ethtool_statistics(executor, source.interface, source.namespace),
    )
    _record_iperf(audit, source, destination, duration, streams, code, stdout, stderr, measurement.ok)
    combined = _combine(delta(before_src, after_src, flaps), delta(before_dst, after_dst, 0))
    counter_verdict, counter_reasons = judge_counters(combined, profile)
    stable_verdict, stable_reasons = judge_stability(combined, profile)
    combined.verdict = worst_verdict([counter_verdict, stable_verdict])
    combined.reasons = counter_reasons + stable_reasons
    nic_max = max(source.supported_speeds_mbps) if source.supported_speeds_mbps else None
    bottleneck, assessment = assess_bottleneck(
        throughput_mbps=measurement.mbps_avg,
        minimum_mbps=profile.minimum_throughput_mbps,
        link_mbps=source.link.speed_mbps,
        expected_link_mbps=profile.expected_link_speed,
        cpu_percent=host.cpu_percent_max,
        cpu_busy_percent=profile.cpu_busy_percent,
        usb=source.usb,
        nic_max_mbps=nic_max,
    )
    if host.iperf_cpu_host is None:
        host.iperf_cpu_host = measurement.cpu_host
    lost = [
        f"{name} ({address})"
        for name, address in (
            (source.logical_name, source.address),
            (destination.logical_name, destination.address),
        )
        if is_link_local(address or "")
    ]
    failure = classify_iperf_failure(f"{stderr}\n{measurement.error or ''}")
    if unit_reason:
        failure = {
            "status": "FAILED",
            "error": unit_reason.split()[0],
            "details": unit_reason,
        }
    cut = ""
    if failure is not None:
        cut = f"{failure['error']}: {failure['details']}"
    elif lost:
        cut = (
            "Se perdió la conexión con el equipo: "
            + ", ".join(lost)
            + " quedó en enlace local. El router no entregó DHCP. No es una medición de throughput."
        )
    else:
        cut = explain_cut(
            source.logical_name,
            destination.logical_name,
            source_down=port_down(after_src),
            destination_down=port_down(after_dst),
            source_flaps=src_flaps,
            destination_flaps=dst_flaps,
            timed_out="TIMEOUT" in (stderr or "") or code == 124,
            measured=measurement.ok and measurement.mbps_avg is not None,
            seconds=measurement.seconds if measurement.seconds is not None else last_interval_second(stdout or ""),
            requested=duration,
            exit_code=code,
            output_bytes=len(stdout or ""),
            timeout_s=float(duration + 45),
            iperf_error=(stderr or "") if measurement.ok else (measurement.error or stderr or ""),
        )
    if failure is not None or lost:
        throughput_verdict = Verdict.FAIL
        throughput_reasons = [cut]
    elif measurement.ok and measurement.mbps_avg is not None:
        throughput_verdict, throughput_reasons = judge_throughput(
            measurement.mbps_avg, profile, bottleneck, assessment
        )
    else:
        throughput_verdict = Verdict.FAIL
        throughput_reasons = [cut or f"No hubo medición en {source.logical_name} → {destination.logical_name}."]
    reasons = list(throughput_reasons)
    if long_duration:
        reasons.append("LONG DURATION TEST")
        reasons.append("Thermal/stability test enabled")
    reasons.extend(counter_reasons)
    verdict = worst_verdict([throughput_verdict, counter_verdict, stable_verdict])
    return FlowResult(
        case_id=case_id,
        kind=kind,
        source=source.logical_name,
        destination=destination.logical_name,
        direction=direction,
        streams=streams,
        requested_duration_s=duration,
        actual_duration_s=measurement.seconds,
        long_duration=long_duration,
        mbps_avg=measurement.mbps_avg,
        mbps_min=measurement.mbps_min,
        mbps_max=measurement.mbps_max,
        retransmits=measurement.retransmits,
        max_snd_cwnd=measurement.max_snd_cwnd,
        error_delta=combined,
        host=host,
        bottleneck=bottleneck,
        assessment=assessment,
        verdict=verdict,
        reasons=reasons,
        raw_json=f"raw/iperf3/{case_id}.json",
        source_ip=source.address,
        destination_ip=destination.address,
    )


def _combine(left: ErrorDelta, right: ErrorDelta) -> ErrorDelta:
    return ErrorDelta(
        rx_errors_before=left.rx_errors_before + right.rx_errors_before,
        rx_errors_after=left.rx_errors_after + right.rx_errors_after,
        delta_rx_errors=left.delta_rx_errors + right.delta_rx_errors,
        tx_errors_before=left.tx_errors_before + right.tx_errors_before,
        tx_errors_after=left.tx_errors_after + right.tx_errors_after,
        delta_tx_errors=left.delta_tx_errors + right.delta_tx_errors,
        rx_dropped_before=left.rx_dropped_before + right.rx_dropped_before,
        rx_dropped_after=left.rx_dropped_after + right.rx_dropped_after,
        delta_rx_dropped=left.delta_rx_dropped + right.delta_rx_dropped,
        tx_dropped_before=left.tx_dropped_before + right.tx_dropped_before,
        tx_dropped_after=left.tx_dropped_after + right.tx_dropped_after,
        delta_tx_dropped=left.delta_tx_dropped + right.delta_tx_dropped,
        carrier_changes_before=left.carrier_changes_before + right.carrier_changes_before,
        carrier_changes_after=left.carrier_changes_after + right.carrier_changes_after,
        delta_carrier_changes=left.delta_carrier_changes + right.delta_carrier_changes,
        link_flaps=left.link_flaps + right.link_flaps,
    )


def _record_iperf(audit, source, destination, duration, streams, code, stdout, stderr, ok) -> None:
    command = (
        f"iperf3 -c {destination.address} -B {source.address} "
        f"-p {destination.iperf_port} -P {streams} -t {duration} -J"
    )
    audit.record_command(
        CommandResult(
            argv=["iperf3", "-c", destination.address, "-J"],
            exit_code=code,
            stdout=stdout,
            stderr=stderr,
            duration_s=duration,
            namespace=source.namespace,
            interface=source.interface,
            command=command,
            started_at=datetime.now(timezone.utc).isoformat(),
        )
    )
    if not ok and not stderr and not stdout:
        return
