"""Collect ethtool, ip -s, and sysfs carrier data for one interface."""

from __future__ import annotations

import json
import re
import time

from tc_net_cert.core.executor import Executor
from tc_net_cert.core.models import ErrorDelta, LinkFacts, Verdict

_SPEED_RE = re.compile(r"(\d+)\s*base", re.IGNORECASE)
_SPEED_LINE = re.compile(r"(\d+)\s*Mb/s", re.IGNORECASE)


def parse_ethtool(text: str) -> dict[str, object]:
    speed = None
    duplex = ""
    autoneg = ""
    detected = False
    supported: list[int] = []
    for line in text.splitlines():
        stripped = line.strip()
        lower = stripped.lower()
        if lower.startswith("speed:"):
            match = _SPEED_LINE.search(stripped)
            if match:
                speed = int(match.group(1))
        elif lower.startswith("duplex:"):
            value = stripped.split(":", 1)[1].strip().lower()
            duplex = "" if value.startswith("unknown") else value
        elif lower.startswith("auto-negotiation:"):
            autoneg = stripped.split(":", 1)[1].strip().lower()
        elif lower.startswith("link detected:"):
            detected = stripped.split(":", 1)[1].strip().lower() == "yes"
        elif "baset/" in lower or "base-t/" in lower:
            for match in _SPEED_RE.finditer(stripped):
                value = int(match.group(1))
                if value not in supported:
                    supported.append(value)
    return {
        "speed_mbps": speed,
        "duplex": duplex,
        "autoneg": autoneg,
        "detected": detected,
        "supported_speeds_mbps": supported,
    }


def parse_ethtool_driver(text: str) -> dict[str, str]:
    fields = {"driver": "", "firmware": "", "bus_info": ""}
    mapping = {
        "driver:": "driver",
        "firmware-version:": "firmware",
        "bus-info:": "bus_info",
    }
    for line in text.splitlines():
        lower = line.lower()
        for prefix, key in mapping.items():
            if lower.startswith(prefix):
                fields[key] = line.split(":", 1)[1].strip()
    return fields


def parse_ip_stats(payload: str, interface: str) -> dict[str, int | str]:
    data = json.loads(payload or "[]")
    item = None
    for candidate in data:
        if candidate.get("ifname") == interface or len(data) == 1:
            item = candidate
            break
    if item is None:
        return {}
    stats = item.get("stats64") or item.get("stats") or {}
    rx = stats.get("rx") or {}
    tx = stats.get("tx") or {}
    return {
        "operstate": str(item.get("operstate") or ""),
        "rx_packets": int(rx.get("packets") or 0),
        "tx_packets": int(tx.get("packets") or 0),
        "rx_errors": int(rx.get("errors") or 0),
        "tx_errors": int(tx.get("errors") or 0),
        "rx_dropped": int(rx.get("dropped") or 0),
        "tx_dropped": int(tx.get("dropped") or 0),
        "collisions": int(tx.get("collisions") or rx.get("collisions") or 0),
    }


def explain_link_down(speed_mbps: int | None, duplex: str) -> str:
    """A down carrier with a leftover speed is not the same fault as no signal at all."""
    text = (duplex or "").lower()
    if speed_mbps and text in {"full", "half"}:
        return (
            f"Link detected: no. El adaptador aún muestra {speed_mbps} Mbps {text}, "
            "pero el carrier no estaba arriba."
        )
    return "Link detected: no"


def link_ready(link: LinkFacts) -> bool:
    """Carrier alone is not enough. e1000e often says link yes while speed is still Unknown."""
    duplex = (link.duplex or "").lower()
    return bool(link.detected and link.speed_mbps and duplex in {"full", "half"})


def settle_link(
    executor: Executor,
    interface: str,
    namespace: str | None = None,
    *,
    attempts: int = 20,
    pause_s: float = 0.5,
) -> LinkFacts:
    """Re-read until speed and duplex are negotiated. Moving the NIC drops carrier briefly."""
    last = read_link(executor, interface, namespace)
    for _ in range(max(0, attempts - 1)):
        if link_ready(last):
            return last
        time.sleep(pause_s)
        executor.run(
            ["ip", "link", "set", interface, "up"],
            namespace=namespace,
            interface=interface,
            timeout=10,
        )
        last = read_link(executor, interface, namespace)
    return last


def read_link(
    executor: Executor,
    interface: str,
    namespace: str | None = None,
) -> LinkFacts:
    ethtool = executor.run(["ethtool", interface], namespace=namespace, interface=interface, timeout=15)
    driver = executor.run(["ethtool", "-i", interface], namespace=namespace, interface=interface, timeout=15)
    stats = executor.run(
        ["ip", "-s", "-j", "link", "show", "dev", interface],
        namespace=namespace,
        interface=interface,
        timeout=10,
    )
    carrier = executor.run(
        ["cat", f"/sys/class/net/{interface}/carrier_changes"],
        namespace=namespace,
        interface=interface,
        timeout=10,
    )
    parsed = parse_ethtool(ethtool.stdout) if ethtool.ok else {
        "speed_mbps": None,
        "duplex": "",
        "autoneg": "",
        "detected": False,
        "supported_speeds_mbps": [],
    }
    identity = parse_ethtool_driver(driver.stdout) if driver.ok else {"driver": "", "firmware": "", "bus_info": ""}
    counters = parse_ip_stats(stats.stdout, interface) if stats.ok else {}
    changes = 0
    if carrier.ok and carrier.stdout.strip().isdigit():
        changes = int(carrier.stdout.strip())
    return LinkFacts(
        detected=bool(parsed["detected"]) or str(counters.get("operstate") or "").upper() == "UP",
        speed_mbps=parsed["speed_mbps"] if isinstance(parsed["speed_mbps"], int) else None,
        duplex=str(parsed["duplex"]),
        autoneg=str(parsed["autoneg"]),
        driver=str(identity["driver"]),
        firmware=str(identity["firmware"]),
        bus_info=str(identity["bus_info"]),
        rx_packets=int(counters.get("rx_packets") or 0),
        tx_packets=int(counters.get("tx_packets") or 0),
        rx_errors=int(counters.get("rx_errors") or 0),
        tx_errors=int(counters.get("tx_errors") or 0),
        rx_dropped=int(counters.get("rx_dropped") or 0),
        tx_dropped=int(counters.get("tx_dropped") or 0),
        collisions=int(counters.get("collisions") or 0),
        carrier_changes=changes,
        operstate=str(counters.get("operstate") or ""),
        supported_speeds_mbps=list(parsed["supported_speeds_mbps"]),
    )


def dump_ethtool_statistics(executor: Executor, interface: str, namespace: str | None = None) -> str:
    result = executor.run(["ethtool", "-S", interface], namespace=namespace, interface=interface, timeout=15)
    return result.stdout if result.ok else result.stderr


def delta(before: LinkFacts, after: LinkFacts, flaps: int = 0) -> ErrorDelta:
    return ErrorDelta(
        rx_errors_before=before.rx_errors,
        rx_errors_after=after.rx_errors,
        delta_rx_errors=after.rx_errors - before.rx_errors,
        tx_errors_before=before.tx_errors,
        tx_errors_after=after.tx_errors,
        delta_tx_errors=after.tx_errors - before.tx_errors,
        rx_dropped_before=before.rx_dropped,
        rx_dropped_after=after.rx_dropped,
        delta_rx_dropped=after.rx_dropped - before.rx_dropped,
        tx_dropped_before=before.tx_dropped,
        tx_dropped_after=after.tx_dropped,
        delta_tx_dropped=after.tx_dropped - before.tx_dropped,
        carrier_changes_before=before.carrier_changes,
        carrier_changes_after=after.carrier_changes,
        delta_carrier_changes=max(0, after.carrier_changes - before.carrier_changes),
        link_flaps=flaps,
        verdict=Verdict.NOT_RUN,
    )
