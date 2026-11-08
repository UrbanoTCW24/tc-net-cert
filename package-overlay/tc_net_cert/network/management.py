"""Identify the administration plane and refuse to move it in SAFE MODE."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable

from tc_net_cert.core.exceptions import SafetyError
from tc_net_cert.core.executor import Executor
from tc_net_cert.core.models import InterfaceRecord, ManagementPlane

_DEFAULT_DEV_RE = re.compile(r"\bdev (\S+)")


def _default_route_entries(executor: Executor) -> list[dict]:
    route = executor.run(["ip", "-j", "route", "show", "default"], timeout=10)
    if not route.ok or not route.stdout.strip():
        return []
    try:
        parsed = json.loads(route.stdout)
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []


def _primary_default_route(entries: list[dict]) -> tuple[str, str]:
    """Pick the default route with the lowest kernel metric (not JSON array order)."""
    best_dev = ""
    best_gw = ""
    best_metric: int | None = None
    for entry in entries:
        dev = str(entry.get("dev") or "").strip()
        if not dev:
            continue
        gw = str(entry.get("gateway") or "")
        metric_raw = entry.get("metric")
        if metric_raw is None:
            metric = 0
        else:
            try:
                metric = int(metric_raw)
            except (TypeError, ValueError):
                metric = 0
        if best_metric is None or metric < best_metric:
            best_metric = metric
            best_dev = dev
            best_gw = gw
    return best_dev, best_gw


def scrub_lab_port_default_routes(
    executor: Executor,
    interfaces: list[InterfaceRecord],
    *,
    port_macs: dict[str, str],
    management_mac: str,
) -> list[str]:
    """Strip default routes on DUT-facing lab NICs so SAFE MODE does not treat them as admin."""
    from tc_net_cert.config.loader import normalize_mac
    from tc_net_cert.network.discovery import index_by_mac

    notes: list[str] = []
    try:
        by_mac = index_by_mac(interfaces)
    except RuntimeError:
        return notes
    mgmt_dev = ""
    if management_mac.strip():
        try:
            rec = by_mac.get(normalize_mac(management_mac))
            if rec is not None:
                mgmt_dev = rec.name
        except Exception:
            pass
    targets: set[str] = set()
    for mac in port_macs.values():
        if not str(mac or "").strip():
            continue
        try:
            rec = by_mac.get(normalize_mac(str(mac)))
        except Exception:
            rec = None
        if rec is not None and rec.name != mgmt_dev:
            targets.add(rec.name)
    for dev in sorted(targets):
        removed = False
        while True:
            result = executor.run(["ip", "-4", "route", "del", "default", "dev", dev], timeout=5)
            if result.exit_code != 0:
                break
            removed = True
        if removed:
            notes.append(f"Removed default route(s) on {dev}")
    return notes


def detect_management(executor: Executor, interfaces: list[InterfaceRecord]) -> ManagementPlane:
    entries = _default_route_entries(executor)
    default_dev, default_gw = _primary_default_route(entries)
    ssh_ip = _ssh_server_ip()
    ssh_iface = ""
    reasons: dict[str, list[str]] = {}
    if default_dev:
        reasons.setdefault(default_dev, []).append("default route")
    if ssh_ip:
        for record in interfaces:
            for address in record.addresses:
                host = address.split("/", 1)[0]
                if host == ssh_ip:
                    ssh_iface = record.name
                    reasons.setdefault(record.name, []).append(f"SSH server address {ssh_ip}")
    protected = sorted(reasons)
    return ManagementPlane(
        default_route_interface=default_dev,
        default_route_gateway=default_gw,
        ssh_server_ip=ssh_ip,
        ssh_interface=ssh_iface,
        protected_interfaces=protected,
        reasons=reasons,
    )


def mark_protected(interfaces: list[InterfaceRecord], plane: ManagementPlane) -> list[InterfaceRecord]:
    marked: list[InterfaceRecord] = []
    for record in interfaces:
        updated = record.model_copy(deep=True)
        if record.name in plane.reasons:
            updated.protected = True
            updated.protection_reasons = list(plane.reasons[record.name])
        marked.append(updated)
    return marked


def pin_management_mac(interfaces: list[InterfaceRecord], mac: str) -> list[InterfaceRecord]:
    """Keep one NIC on the host for Internet/sync; never move it into ns-lan*."""
    from tc_net_cert.config.loader import normalize_mac

    want = str(mac or "").strip()
    if not want:
        return interfaces
    try:
        pinned = normalize_mac(want)
    except Exception:
        return interfaces
    marked: list[InterfaceRecord] = []
    label = "LAN administración (config)"
    for record in interfaces:
        updated = record.model_copy(deep=True)
        try:
            if normalize_mac(record.mac) == pinned:
                updated.protected = True
                reasons = list(updated.protection_reasons)
                if label not in reasons:
                    reasons.append(label)
                updated.protection_reasons = reasons
        except Exception:
            pass
        marked.append(updated)
    return marked


def assert_safe_to_move(
    record: InterfaceRecord,
    *,
    safe_mode: bool,
    override_token: str = "",
) -> None:
    """Block management, wireless, and excluded interfaces.

    SAFE MODE never accepts an override. Outside SAFE MODE the operator must
    pass the interface name as an explicit confirmation token.
    """
    if record.is_wireless or record.exclusion_reason == "wireless":
        raise SafetyError(f"Refusing to move wireless interface {record.name}")
    if record.excluded:
        raise SafetyError(
            f"Refusing to move {record.name}: excluded as {record.exclusion_reason}"
        )
    if not record.protected:
        return
    why = ", ".join(record.protection_reasons) or "management"
    if safe_mode:
        raise SafetyError(
            f"SAFE MODE: refusing to move management interface {record.name} ({why})"
        )
    if override_token != record.name:
        raise SafetyError(
            f"Refusing to move management interface {record.name} ({why}). "
            "SAFE MODE is off; repeat with confirmation token equal to the interface name."
        )


def restore_management_routing(
    executor: Executor,
    *,
    wifi_iface: str = "",
    ethernet_ifaces: Iterable[str] = (),
) -> None:
    """Prefer Wi-Fi for Internet after a run. Does not disconnect cables or bounce Wi-Fi if already up."""
    ethernet = {name.strip() for name in ethernet_ifaces if name.strip()}
    if wifi_iface:
        ethernet.discard(wifi_iface)
    for dev in sorted(ethernet):
        executor.run(["ip", "-4", "route", "del", "default", "dev", dev], timeout=5)
    route = executor.run(["ip", "-4", "route", "show", "default"], timeout=10)
    for line in (route.stdout or "").splitlines():
        match = _DEFAULT_DEV_RE.search(line)
        if not match:
            continue
        dev = match.group(1)
        if wifi_iface and dev == wifi_iface:
            continue
        if dev in ethernet or not wifi_iface:
            executor.run(["ip", "-4", "route", "del", "default", "dev", dev], timeout=5)
    if not wifi_iface:
        return
    route_after = executor.run(["ip", "-4", "route", "show", "default"], timeout=10)
    for line in (route_after.stdout or "").splitlines():
        if wifi_iface in line and "default" in line:
            return
    gateway = executor.run(
        ["nmcli", "-g", "IP4.GATEWAY", "device", "show", wifi_iface],
        timeout=10,
    )
    gw = (gateway.stdout or "").strip()
    if gw and gw != "--":
        executor.run(
            ["ip", "-4", "route", "replace", "default", "via", gw, "dev", wifi_iface, "metric", "50"],
            timeout=10,
        )
        return
    state = executor.run(
        ["nmcli", "-g", "GENERAL.STATE", "device", "show", wifi_iface],
        timeout=10,
    )
    lowered = (state.stdout or "").lower()
    if "connected" in lowered:
        executor.run(["nmcli", "device", "reapply", wifi_iface], timeout=30)
        return
    show = executor.run(
        ["nmcli", "-g", "GENERAL.CONNECTION", "device", "show", wifi_iface],
        timeout=10,
    )
    connection = (show.stdout or "").strip()
    if connection and connection != "--":
        executor.run(
            ["nmcli", "connection", "up", connection, "ifname", wifi_iface],
            timeout=45,
        )
    else:
        executor.run(["nmcli", "device", "connect", wifi_iface], timeout=45)


def refresh_portal_links(executor: Executor, port_macs: dict[str, str]) -> list[str]:
    """Renew DHCP only on lab ports that lost an address. Skips links that already have IP (no NM bounce)."""
    from pathlib import Path

    wanted = {mac.strip().lower() for mac in port_macs.values() if mac.strip()}
    if not wanted:
        return []
    notes: list[str] = []
    root = Path("/sys/class/net")
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        iface = entry.name
        if (entry / "wireless").exists() or (entry / "phy80211").exists():
            continue
        try:
            mac = (entry / "address").read_text(encoding="utf-8").strip().lower()
            carrier = (entry / "carrier").read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if mac not in wanted:
            continue
        if carrier != "1":
            notes.append(f"{iface}: sin enlace físico (conecta el cable al módem)")
            continue
        address = executor.run(
            ["nmcli", "-g", "IP4.ADDRESS", "device", "show", iface],
            timeout=10,
        )
        has_ip = bool((address.stdout or "").strip()) and (address.stdout or "").strip() != "--"
        state = executor.run(
            ["nmcli", "-g", "GENERAL.STATE", "device", "show", iface],
            timeout=10,
        )
        connected = "connected" in (state.stdout or "").lower()
        if has_ip and connected:
            notes.append(f"{iface}: enlace listo (sin reiniciar NM)")
            continue
        if connected:
            executor.run(["nmcli", "device", "reapply", iface], timeout=45)
        else:
            executor.run(["nmcli", "device", "connect", iface], timeout=45)
        notes.append(f"{iface}: DHCP renovado")
    return notes


def persist_lab_routing(executor: Executor, *, wifi_connection: str = "") -> list[str]:
    """Tell NetworkManager: Wi-Fi may be default; lab Ethernet must not steal it after DHCP."""
    notes: list[str] = []
    listed = executor.run(
        ["nmcli", "-t", "-f", "NAME,TYPE", "connection", "show"],
        timeout=15,
    )
    wifi_names: list[str] = []
    ethernet_names: list[str] = []
    for line in (listed.stdout or "").splitlines():
        if ":" not in line:
            continue
        name, kind = line.split(":", 1)
        if kind == "802-11-wireless":
            wifi_names.append(name)
        elif kind == "802-3-ethernet":
            ethernet_names.append(name)
    targets = [wifi_connection] if wifi_connection else wifi_names
    for name in targets:
        if not name:
            continue
        result = executor.run(
            [
                "nmcli",
                "connection",
                "modify",
                name,
                "ipv4.never-default",
                "no",
                "ipv4.route-metric",
                "50",
            ],
            timeout=15,
        )
        if result.ok:
            notes.append(f"Wi-Fi profile {name}: default allowed, metric 50")
    for name in ethernet_names:
        result = executor.run(
            [
                "nmcli",
                "connection",
                "modify",
                name,
                "ipv4.never-default",
                "yes",
                "ipv4.route-metric",
                "900",
            ],
            timeout=15,
        )
        if result.ok:
            notes.append(f"Ethernet profile {name}: never-default, metric 900")
    wifi_iface = ""
    devices = executor.run(["nmcli", "-t", "-f", "DEVICE,TYPE", "device"], timeout=10)
    for line in (devices.stdout or "").splitlines():
        if line.endswith(":wifi"):
            wifi_iface = line.split(":", 1)[0]
            break
    restore_management_routing(executor, wifi_iface=wifi_iface, ethernet_ifaces=())
    return notes


def _ssh_server_ip() -> str:
    raw = os.environ.get("SSH_CONNECTION", "").strip()
    if raw:
        parts = raw.split()
        if len(parts) >= 3:
            return parts[2]
    client = os.environ.get("SSH_CLIENT", "").strip()
    if client:
        return ""
    return ""
