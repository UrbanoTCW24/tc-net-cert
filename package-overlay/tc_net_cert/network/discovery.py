"""Discover physical Ethernet NICs and reject virtual, wireless, and tunnel devices.

Interface names are never assumed. Identity for later runs is the MAC address.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from tc_net_cert.core.executor import Executor
from tc_net_cert.core.models import InterfaceRecord

_VIRTUAL_TYPES = {
    "loopback",
    "bridge",
    "tun",
    "tap",
    "veth",
    "dummy",
    "bond",
    "team",
    "vlan",
    "vxlan",
    "macvlan",
    "macvtap",
    "ipvlan",
    "ipip",
    "sit",
    "gre",
    "gretap",
    "erspan",
    "wireguard",
    "can",
    "ipoib",
    "ifb",
}
_NAME_PREFIXES = (
    "veth",
    "br-",
    "docker",
    "virbr",
    "vnet",
    "tun",
    "tap",
    "wg",
    "tailscale",
    "zt",
    "ifb",
    "dummy",
    "bond",
    "team",
    "vlan",
    "vxlan",
    "gre",
    "gretap",
    "erspan",
    "macvlan",
    "macvtap",
    "ipvlan",
    "vboxnet",
    "vmnet",
    "cni",
    "flannel",
    "cali",
    "lxc",
    "kube",
)
_HYPERV_DRIVERS = {"hv_netvsc"}
_VIRTUAL_DRIVERS = {"veth", "bridge", "dummy", "tun", "tap", "wireguard", "vxlan", "macvlan"}


def classify_interface(record: InterfaceRecord) -> InterfaceRecord:
    """Return a copy with exclusion_reason set when the NIC cannot be a test port."""
    reason = _exclusion_reason(record)
    updated = record.model_copy(deep=True)
    if reason:
        updated.excluded = True
        updated.exclusion_reason = reason
    return updated


def _exclusion_reason(record: InterfaceRecord) -> str:
    name = record.name.lower()
    link_type = record.link_type.lower()
    driver = record.driver.lower()
    if name == "lo" or link_type == "loopback":
        return "loopback"
    if record.is_wireless or name.startswith("wl") or name.startswith("wlan"):
        return "wireless"
    if name.startswith("tailscale") or driver == "tailscale":
        return "tailscale"
    if driver in _HYPERV_DRIVERS or "hyperv" in name or "hyper-v" in name:
        return "hyper-v"
    if name == "docker0" or name.startswith("docker") or driver == "docker":
        return "docker"
    if link_type == "bridge" or name.startswith("br-") or name.startswith("virbr"):
        return "bridge"
    if link_type in _VIRTUAL_TYPES or driver in _VIRTUAL_DRIVERS:
        return "virtual" if link_type not in {"tun", "tap", "gre", "sit", "ipip", "wireguard", "vxlan"} else "tunnel"
    if any(name.startswith(prefix) for prefix in _NAME_PREFIXES):
        if name.startswith("tailscale"):
            return "tailscale"
        if name.startswith(("tun", "tap", "wg", "gre", "sit")):
            return "tunnel"
        return "virtual"
    if link_type and link_type != "ether":
        return "virtual"
    if record.master:
        return "bridge"
    return ""


def parse_ip_addr_json(payload: str) -> list[InterfaceRecord]:
    data = json.loads(payload or "[]")
    records: list[InterfaceRecord] = []
    for item in data:
        addresses = []
        for addr in item.get("addr_info") or []:
            local = addr.get("local")
            prefix = addr.get("prefixlen")
            if local:
                addresses.append(f"{local}/{prefix}" if prefix is not None else str(local))
        records.append(
            InterfaceRecord(
                name=str(item.get("ifname") or ""),
                mac=_normalize_link_mac(str(item.get("address") or "")),
                operstate=str(item.get("operstate") or ""),
                link_type=str(item.get("link_type") or item.get("linkinfo", {}).get("info_kind") or ""),
                master=str(item.get("master") or ""),
                addresses=addresses,
            )
        )
    return records


def discover_interfaces(executor: Executor, sysfs_root: Path | None = None) -> list[InterfaceRecord]:
    result = executor.run(["ip", "-j", "addr"], timeout=15)
    if not result.ok:
        raise RuntimeError(result.stderr.strip() or "ip -j addr failed")
    records = parse_ip_addr_json(result.stdout)
    root = sysfs_root or Path("/sys/class/net")
    enriched: list[InterfaceRecord] = []
    for record in records:
        record.is_wireless = (root / record.name / "wireless").exists() or (
            root / record.name / "phy80211"
        ).exists()
        record.driver = _driver_name(root / record.name)
        vendor, model = _udev_vendor(executor, record.name)
        if vendor:
            record.vendor = vendor
        if model:
            record.model = model
        if not record.driver:
            driver = _ethtool_driver(executor, record.name)
            if driver:
                record.driver = driver
        enriched.append(classify_interface(record))
    return enriched


def candidates(records: list[InterfaceRecord]) -> list[InterfaceRecord]:
    return [item for item in records if not item.excluded and not item.protected]


def index_by_mac(records: list[InterfaceRecord]) -> dict[str, InterfaceRecord]:
    found: dict[str, InterfaceRecord] = {}
    for record in records:
        if not record.mac or record.mac == "00:00:00:00:00:00":
            continue
        if record.mac in found and found[record.mac].name != record.name:
            raise RuntimeError(f"Duplicate MAC {record.mac} on {found[record.mac].name} and {record.name}")
        found[record.mac] = record
    return found


def _normalize_link_mac(value: str) -> str:
    text = value.strip().lower()
    if re.fullmatch(r"[0-9a-f]{2}(:[0-9a-f]{2}){5}", text):
        return text
    return text


def _driver_name(nic_sysfs: Path) -> str:
    driver = nic_sysfs / "device" / "driver"
    try:
        if driver.exists():
            return driver.resolve().name
    except OSError:
        return ""
    return ""


def _udev_vendor(executor: Executor, name: str) -> tuple[str, str]:
    result = executor.run(
        ["udevadm", "info", "-q", "property", "-p", f"/sys/class/net/{name}"],
        timeout=10,
        interface=name,
    )
    if not result.ok:
        return "", ""
    props: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        props[key] = value.strip()
    vendor = props.get("ID_VENDOR_FROM_DATABASE") or props.get("ID_VENDOR") or ""
    model = props.get("ID_MODEL_FROM_DATABASE") or props.get("ID_MODEL") or ""
    return vendor, model


def _ethtool_driver(executor: Executor, name: str) -> str:
    result = executor.run(["ethtool", "-i", name], timeout=10, interface=name)
    if not result.ok:
        return ""
    for line in result.stdout.splitlines():
        if line.lower().startswith("driver:"):
            return line.split(":", 1)[1].strip()
    return ""
