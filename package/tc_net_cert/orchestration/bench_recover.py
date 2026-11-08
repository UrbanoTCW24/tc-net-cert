"""Restore one bench (LAN1-4 or LAN5-8) without a full global recover."""

from __future__ import annotations

from tc_net_cert.config.benches import bench_port_list
from tc_net_cert.config.loader import load_config
from tc_net_cert.core.executor import Executor
from tc_net_cert.namespaces.manager import namespace_name
from tc_net_cert.network.discovery import discover_interfaces, index_by_mac


def recover_bench(executor: Executor, bench_id: str, *, config_path=None) -> list[str]:
    config, _path = load_config(config_path)
    logical_names = bench_port_list(config, bench_id)
    if not logical_names:
        return [f"Banco {bench_id}: sin puertos configurados"]
    actions: list[str] = []
    by_mac = index_by_mac(discover_interfaces(executor))
    for logical in logical_names:
        port = config.ports.get(logical)
        if port is None:
            continue
        ns = namespace_name(logical)
        record = by_mac.get(port.mac.lower())
        iface = record.name if record else ""
        if iface and _iface_in_netns(executor, iface, ns):
            moved = executor.run(
                ["ip", "link", "set", iface, "netns", "1"],
                namespace=ns,
                interface=iface,
                timeout=15,
            )
            if moved.ok:
                actions.append(f"{logical}: {iface} devuelta a la raíz")
        if ns in _list_netns(executor):
            deleted = executor.run(["ip", "netns", "del", ns], timeout=10)
            if deleted.ok:
                actions.append(f"namespace {ns} eliminado")
    if not actions:
        actions.append(f"Banco {bench_id.upper()}: sin namespaces colgados")
    return actions


def _list_netns(executor: Executor) -> set[str]:
    result = executor.run(["ip", "netns", "list"], timeout=10)
    names: set[str] = set()
    for line in result.stdout.splitlines():
        part = line.split()[0] if line.split() else ""
        if part:
            names.add(part)
    return names


def _iface_in_netns(executor: Executor, iface: str, namespace: str) -> bool:
    result = executor.run(
        ["ip", "netns", "exec", namespace, "ip", "link", "show", "dev", iface],
        timeout=10,
    )
    return result.ok and iface in result.stdout
