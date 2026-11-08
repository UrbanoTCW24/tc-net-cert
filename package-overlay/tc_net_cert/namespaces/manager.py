"""Create isolated routing tables by moving each test NIC into its own netns.

This is the mechanism that allows several DUT ports to hand out addresses in
the same IPv4 subnet without the host having to choose a single egress NIC.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from tc_net_cert.core.exceptions import ToolError
from tc_net_cert.core.executor import Executor
from tc_net_cert.core.models import ActiveSession, Endpoint, IfaceSnapshot


@dataclass
class NamespacePlan:
    endpoints: list[Endpoint]

    def namespace_names(self) -> list[str]:
        return [item.namespace for item in self.endpoints]


def namespace_name(logical: str) -> str:
    return f"ns-{logical.lower()}"


def build_plan(bindings: list[tuple[str, str, str]]) -> NamespacePlan:
    """Build one namespace per (logical name, interface, mac) tuple."""
    seen_mac: dict[str, str] = {}
    seen_iface: dict[str, str] = {}
    seen_logical: set[str] = set()
    endpoints: list[Endpoint] = []
    for logical, interface, mac in bindings:
        logical = logical.upper()
        mac = mac.lower()
        if logical in seen_logical:
            raise ToolError(f"Duplicate logical port {logical}")
        if mac in seen_mac:
            raise ToolError(f"Duplicate MAC {mac} on {seen_mac[mac]} and {logical}")
        if interface in seen_iface:
            raise ToolError(f"Duplicate interface {interface} on {seen_iface[interface]} and {logical}")
        seen_logical.add(logical)
        seen_mac[mac] = logical
        seen_iface[interface] = logical
        endpoints.append(
            Endpoint(
                logical_name=logical,
                interface=interface,
                mac=mac,
                namespace=namespace_name(logical),
            )
        )
    if not endpoints:
        raise ToolError("Namespace plan is empty")
    return NamespacePlan(endpoints=endpoints)


class NamespaceManager:
    def __init__(self, executor: Executor) -> None:
        self.executor = executor

    def list_namespaces(self) -> list[str]:
        result = self.executor.run(["ip", "netns", "list"], timeout=10)
        names: list[str] = []
        for line in result.stdout.splitlines():
            name = line.split()[0] if line.split() else ""
            if name:
                names.append(name)
        return names

    def snapshot(self, endpoint: Endpoint) -> IfaceSnapshot:
        result = self.executor.run(["ip", "-j", "addr", "show", "dev", endpoint.interface], timeout=10)
        addresses: list[str] = []
        operstate = ""
        mtu = None
        if result.ok and result.stdout.strip():
            data = json.loads(result.stdout)
            if data:
                item = data[0]
                operstate = str(item.get("operstate") or "")
                mtu = item.get("mtu")
                for addr in item.get("addr_info") or []:
                    local = addr.get("local")
                    prefix = addr.get("prefixlen")
                    if local and prefix is not None and addr.get("family") == "inet":
                        addresses.append(f"{local}/{prefix}")
        return IfaceSnapshot(
            name=endpoint.interface,
            mac=endpoint.mac,
            operstate=operstate,
            addresses=addresses,
            mtu=int(mtu) if mtu is not None else None,
            namespace=endpoint.namespace,
            logical_name=endpoint.logical_name,
        )

    def apply(self, plan: NamespacePlan) -> None:
        for endpoint in plan.endpoints:
            self.move_one(endpoint)

    def move_one(self, endpoint: Endpoint) -> None:
        existing = set(self.list_namespaces())
        if endpoint.namespace in existing:
            raise ToolError(
                f"Namespace {endpoint.namespace} already exists. Run 'tc-net-cert recover' first."
            )
        created = self.executor.run(["ip", "netns", "add", endpoint.namespace], timeout=10)
        if not created.ok:
            raise ToolError(created.stderr.strip() or f"Could not create {endpoint.namespace}")
        down = self.executor.run(["ip", "link", "set", endpoint.interface, "down"], timeout=10)
        if not down.ok:
            raise ToolError(down.stderr.strip() or f"Could not set {endpoint.interface} down")
        moved = self.executor.run(
            ["ip", "link", "set", endpoint.interface, "netns", endpoint.namespace],
            timeout=10,
            interface=endpoint.interface,
        )
        if not moved.ok:
            self.executor.run(["ip", "link", "set", endpoint.interface, "up"], timeout=10, interface=endpoint.interface)
            raise ToolError(moved.stderr.strip() or f"Could not move {endpoint.interface}")
        up = self.executor.run(
            ["ip", "link", "set", endpoint.interface, "up"],
            namespace=endpoint.namespace,
            interface=endpoint.interface,
            timeout=10,
        )
        if not up.ok:
            raise ToolError(up.stderr.strip() or f"Could not bring up {endpoint.interface}")
        self.executor.run(
            ["ip", "link", "set", "lo", "up"],
            namespace=endpoint.namespace,
            timeout=10,
        )

    def add_address(self, endpoint: Endpoint, cidr: str) -> None:
        result = self.executor.run(
            ["ip", "addr", "replace", cidr, "dev", endpoint.interface],
            namespace=endpoint.namespace,
            interface=endpoint.interface,
            timeout=10,
        )
        if not result.ok:
            raise ToolError(result.stderr.strip() or f"Could not assign {cidr}")

    def add_default_route(self, endpoint: Endpoint, gateway: str) -> None:
        result = self.executor.run(
            ["ip", "route", "replace", "default", "via", gateway, "dev", endpoint.interface],
            namespace=endpoint.namespace,
            interface=endpoint.interface,
            timeout=10,
        )
        if not result.ok:
            raise ToolError(result.stderr.strip() or f"Could not install default route via {gateway}")

    def restore_moved(self, session: ActiveSession, moved: set[str]) -> list[str]:
        """Return interfaces that are still missing from the root namespace."""
        missing: list[str] = []
        for snapshot in session.snapshots:
            if snapshot.logical_name not in moved and snapshot.name not in moved:
                continue
            self.executor.run(
                ["ip", "link", "set", snapshot.name, "netns", "1"],
                namespace=snapshot.namespace,
                interface=snapshot.name,
                timeout=10,
            )
            if not self._present_in_root(snapshot.name):
                self.executor.run(["ip", "netns", "del", snapshot.namespace], timeout=10)
            if not self._present_in_root(snapshot.name):
                missing.append(snapshot.name)
                continue
            self._restore_addresses(snapshot)
        for name in session.namespaces:
            self.executor.run(["ip", "netns", "del", name], timeout=10)
        return missing

    def _present_in_root(self, name: str) -> bool:
        result = self.executor.run(["ip", "link", "show", "dev", name], timeout=10, interface=name)
        return result.ok

    def _restore_addresses(self, snapshot: IfaceSnapshot) -> None:
        self.executor.run(["ip", "addr", "flush", "dev", snapshot.name], timeout=10, interface=snapshot.name)
        for address in snapshot.addresses:
            self.executor.run(
                ["ip", "addr", "replace", address, "dev", snapshot.name],
                timeout=10,
                interface=snapshot.name,
            )
        if snapshot.mtu:
            self.executor.run(
                ["ip", "link", "set", snapshot.name, "mtu", str(snapshot.mtu)],
                timeout=10,
                interface=snapshot.name,
            )
        if snapshot.operstate.upper() in {"UP", "UNKNOWN", ""}:
            self.executor.run(["ip", "link", "set", snapshot.name, "up"], timeout=10, interface=snapshot.name)
        else:
            self.executor.run(["ip", "link", "set", snapshot.name, "down"], timeout=10, interface=snapshot.name)
