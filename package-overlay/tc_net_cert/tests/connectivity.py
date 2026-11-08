"""Directional connectivity. Every ordered pair is its own case."""

from __future__ import annotations

import re

from tc_net_cert.core.executor import Executor
from tc_net_cert.core.models import Endpoint, FlowResult, PingFacts, Verdict

_LOSS = re.compile(r"(\d+(?:\.\d+)?)%\s+packet loss")
_COUNTS = re.compile(r"(\d+)\s+packets transmitted,\s+(\d+)\s+received", re.IGNORECASE)
_RTT = re.compile(r"=\s*([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+)")


def directional_pairs(names: list[str]) -> list[tuple[str, str]]:
    ordered = sorted(names)
    return [(src, dst) for src in ordered for dst in ordered if src != dst]


def parse_ping(text: str, target: str) -> PingFacts:
    loss = _LOSS.search(text)
    counts = _COUNTS.search(text)
    rtt = _RTT.search(text)
    facts = PingFacts(target=target)
    if counts:
        facts.transmitted = int(counts.group(1))
        facts.received = int(counts.group(2))
    if loss:
        facts.loss_percent = float(loss.group(1))
    elif counts and facts.transmitted:
        facts.loss_percent = (1 - facts.received / facts.transmitted) * 100
    if rtt:
        facts.rtt_min_ms = float(rtt.group(1))
        facts.rtt_avg_ms = float(rtt.group(2))
        facts.rtt_max_ms = float(rtt.group(3))
        facts.rtt_mdev_ms = float(rtt.group(4))
    return facts


def ping_target(
    executor: Executor,
    source: Endpoint,
    target: str,
    count: int,
    wait: int,
    interval: float | None = None,
) -> PingFacts:
    command = ["ping", "-n", "-c", str(count), "-W", str(wait)]
    if interval is not None and interval > 0:
        command.extend(["-i", f"{interval:g}"])
    command.extend(["-I", source.address, target])
    spacing = interval if interval is not None and interval > 0 else float(wait)
    result = executor.run(
        command,
        namespace=source.namespace,
        interface=source.interface,
        timeout=int(count * spacing + wait + 8),
    )
    facts = parse_ping(result.stdout + "\n" + result.stderr, target)
    if facts.loss_percent is None:
        facts.loss_percent = 100.0
        facts.transmitted = count
        facts.received = 0
    return facts


def connectivity_flow(source: Endpoint, destination: Endpoint, ping: PingFacts, verdict: Verdict, reasons: list[str]) -> FlowResult:
    return FlowResult(
        case_id=f"CON-{source.logical_name}-{destination.logical_name}",
        kind="connectivity",
        source=source.logical_name,
        destination=destination.logical_name,
        direction="forward",
        streams=0,
        ping=ping,
        verdict=verdict,
        reasons=reasons,
        source_ip=source.address,
        destination_ip=destination.address,
    )
