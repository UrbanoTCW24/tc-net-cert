"""Choose a DHCP backend and normalize its result for the certification report."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from tc_net_cert.core.exceptions import MissingToolError
from tc_net_cert.core.executor import Executor, which
from tc_net_cert.core.models import DhcpFacts, Endpoint, Verdict


def is_link_local(ip: str) -> bool:
    return (ip or "").startswith("169.254.")


def lease_result(ip: str, port: str) -> tuple[bool, str]:
    """169.254 means the DUT did not answer DHCP. That is a lost connection, not a lease."""
    if not ip:
        return False, f"Se perdió la conexión en {port}: el equipo no entregó DHCP."
    if is_link_local(ip):
        return (
            False,
            f"Se perdió la conexión en {port}: quedó en {ip} (enlace local). El router no entregó DHCP.",
        )
    return True, "lease applied"


def acquire_lease(executor: Executor, endpoint: Endpoint, backend: str, timeout: int) -> DhcpFacts:
    if backend == "internal":
        return _internal(executor, endpoint, timeout)
    if backend == "dhcpcd":
        return _dhcpcd(executor, endpoint, timeout)
    if backend == "dhclient":
        return _dhclient(executor, endpoint, timeout)
    return DhcpFacts(success=False, backend=backend, message=f"Unknown DHCP backend {backend}", verdict=Verdict.FAIL)


def release_lease(executor: Executor, endpoint: Endpoint, backend: str) -> None:
    if backend == "dhcpcd" and which("dhcpcd"):
        _stop_dhcpcd(executor, endpoint)
        return
    if not endpoint.dhcp or not endpoint.dhcp.success:
        return
    if backend == "internal" and endpoint.dhcp.ip and endpoint.dhcp.dhcp_server:
        executor.run(
            [
                sys.executable,
                "-m",
                "tc_net_cert.dhcp.client",
                "--iface",
                endpoint.interface,
                "--release",
                "--ip",
                endpoint.dhcp.ip,
                "--server",
                endpoint.dhcp.dhcp_server,
            ],
            namespace=endpoint.namespace,
            interface=endpoint.interface,
            timeout=15,
        )
        return
    if backend == "dhclient" and which("dhclient"):
        executor.run(
            ["dhclient", "-r", endpoint.interface],
            namespace=endpoint.namespace,
            interface=endpoint.interface,
            timeout=20,
        )


def _internal(executor: Executor, endpoint: Endpoint, timeout: int) -> DhcpFacts:
    result = executor.run(
        [sys.executable, "-m", "tc_net_cert.dhcp.client", "--iface", endpoint.interface, "--timeout", str(timeout)],
        namespace=endpoint.namespace,
        interface=endpoint.interface,
        timeout=timeout + 15,
    )
    return _facts_from_json(result.stdout, result.stderr, "internal", result.ok)


def stop_dhcpcd_on_host(executor: Executor, interface: str) -> None:
    """Stop dhcpcd while the NIC is still in the root namespace (before ns-lan* move)."""
    if which("dhcpcd"):
        executor.run(["dhcpcd", "-x", interface], timeout=10, interface=interface)
    pid_path = Path(f"/run/dhcpcd/{interface}-4.pid")
    pid = ""
    try:
        pid = pid_path.read_text(encoding="utf-8").strip()
    except OSError:
        pid = ""
    if pid.isdigit() and int(pid) > 1:
        executor.run(["kill", "-TERM", pid], timeout=5, interface=interface)
    executor.run(
        [
            "rm",
            "-f",
            str(pid_path),
            f"/run/dhcpcd/{interface}-4.sock",
            f"/run/dhcpcd/{interface}-4.unpriv.sock",
        ],
        timeout=5,
        interface=interface,
    )


def _stop_dhcpcd(executor: Executor, endpoint: Endpoint) -> None:
    """Kill the master named in the pid file. dhcpcd -x cannot see one left outside the namespace."""
    pid_path = Path(f"/run/dhcpcd/{endpoint.interface}-4.pid")
    pid = ""
    try:
        pid = pid_path.read_text(encoding="utf-8").strip()
    except OSError:
        pid = ""
    if pid.isdigit() and int(pid) > 1:
        executor.run(
            ["kill", "-TERM", pid],
            namespace=endpoint.namespace,
            interface=endpoint.interface,
            timeout=5,
        )
        for _ in range(20):
            try:
                os.kill(int(pid), 0)
            except OSError:
                break
            time.sleep(0.1)
    executor.run(
        [
            "rm",
            "-f",
            str(pid_path),
            f"/run/dhcpcd/{endpoint.interface}-4.sock",
            f"/run/dhcpcd/{endpoint.interface}-4.unpriv.sock",
        ],
        namespace=endpoint.namespace,
        interface=endpoint.interface,
        timeout=5,
    )


def _dhcpcd(executor: Executor, endpoint: Endpoint, timeout: int) -> DhcpFacts:
    if which("dhcpcd") is None:
        raise MissingToolError("dhcpcd", "Install it with: sudo apt-get install -y dhcpcd")
    # A previous certification leaves dhcpcd on the host. Its socket is shared,
    # so a new client only sends a command and returns in milliseconds with no
    # address. Stop that master, then start one inside this namespace.
    # It stays up to renew a short lease, and -G avoids a default route.
    _stop_dhcpcd(executor, endpoint)
    argv = [
        "dhcpcd",
        "-4",
        "-G",
        "-t",
        str(timeout),
        "-C",
        "resolv.conf",
        "-C",
        "hostname",
        "-C",
        "ntp.conf",
        endpoint.interface,
    ]
    result = executor.run(
        argv,
        namespace=endpoint.namespace,
        interface=endpoint.interface,
        timeout=timeout + 20,
    )
    if "sending commands" in result.stderr or result.duration_s < 0.05:
        _stop_dhcpcd(executor, endpoint)
        result = executor.run(
            argv,
            namespace=endpoint.namespace,
            interface=endpoint.interface,
            timeout=timeout + 20,
        )
    if not result.ok or "sending commands" in result.stderr or result.duration_s < 0.05:
        return DhcpFacts(
            success=False,
            backend="dhcpcd",
            message="El cliente DHCP regresó sin esperar la concesión del EMTA.",
            verdict=Verdict.FAIL,
            reasons=["DHCP acquisition failed"],
        )
    dumped = executor.run(
        ["dhcpcd", "-U", endpoint.interface],
        namespace=endpoint.namespace,
        interface=endpoint.interface,
        timeout=10,
    )
    values = _dhcpcd_dump(dumped.stdout)
    address = executor.run(
        ["ip", "-j", "addr", "show", "dev", endpoint.interface],
        namespace=endpoint.namespace,
        interface=endpoint.interface,
        timeout=10,
    )
    ipaddr, prefix = _first_ipv4(address.stdout)
    gateway = values.get("routers", "").split()[0] if values.get("routers") else ""
    success, message = lease_result(ipaddr, endpoint.logical_name)
    return DhcpFacts(
        success=success,
        ip=ipaddr,
        prefixlen=prefix,
        mask=values.get("subnet_mask", ""),
        gateway=gateway,
        lease_seconds=_maybe_int(values.get("dhcp_lease_time")),
        dhcp_server=values.get("dhcp_server_identifier", ""),
        acquisition_seconds=round(result.duration_s, 3),
        backend="dhcpcd",
        message=message,
        verdict=Verdict.PASS if success else Verdict.FAIL,
        reasons=["DHCP lease acquired"] if success else [message],
    )


def _dhclient(executor: Executor, endpoint: Endpoint, timeout: int) -> DhcpFacts:
    if which("dhclient") is None:
        raise MissingToolError("dhclient", "Install it with: sudo apt-get install -y isc-dhcp-client")
    result = executor.run(
        ["dhclient", "-4", "-1", "-v", "-timeout", str(timeout), endpoint.interface],
        namespace=endpoint.namespace,
        interface=endpoint.interface,
        timeout=timeout + 20,
    )
    address = executor.run(
        ["ip", "-j", "addr", "show", "dev", endpoint.interface],
        namespace=endpoint.namespace,
        interface=endpoint.interface,
        timeout=10,
    )
    ipaddr, prefix = _first_ipv4(address.stdout)
    route = executor.run(["ip", "-j", "route", "show", "default"], namespace=endpoint.namespace, timeout=10)
    gateway = ""
    if route.ok and route.stdout.strip():
        try:
            entries = json.loads(route.stdout)
        except json.JSONDecodeError:
            entries = []
        if entries:
            gateway = str(entries[0].get("gateway") or "")
    success, message = lease_result(ipaddr, endpoint.logical_name)
    success = success and result.ok
    if not success and not is_link_local(ipaddr):
        message = result.stderr.strip() or "dhclient failed"
    return DhcpFacts(
        success=success,
        ip=ipaddr,
        prefixlen=prefix,
        gateway=gateway,
        acquisition_seconds=round(result.duration_s, 3),
        backend="dhclient",
        message=message,
        verdict=Verdict.PASS if success else Verdict.FAIL,
        reasons=["DHCP lease acquired"] if success else [message],
    )


def _facts_from_json(stdout: str, stderr: str, backend: str, ok: bool) -> DhcpFacts:
    payload = _load_json_object(stdout)
    if payload is None:
        return DhcpFacts(
            success=False,
            backend=backend,
            message=stderr.strip() or "DHCP client did not return JSON",
            verdict=Verdict.FAIL,
            reasons=["DHCP acquisition failed"],
        )
    ip = str(payload.get("ip") or "")
    success, message = lease_result(ip, "el puerto")
    success = success and bool(payload.get("success")) and ok
    if not success and not is_link_local(ip):
        message = str(payload.get("message") or "DHCP acquisition failed")
    reasons = ["DHCP lease acquired"] if success else [message]
    return DhcpFacts(
        success=success,
        ip=str(payload.get("ip") or ""),
        prefixlen=payload.get("prefixlen") if isinstance(payload.get("prefixlen"), int) else None,
        mask=str(payload.get("mask") or ""),
        gateway=str(payload.get("gateway") or ""),
        dns=[str(item) for item in payload.get("dns") or []],
        lease_seconds=payload.get("lease_seconds") if isinstance(payload.get("lease_seconds"), int) else None,
        dhcp_server=str(payload.get("dhcp_server") or ""),
        acquisition_seconds=payload.get("acquisition_seconds") if isinstance(payload.get("acquisition_seconds"), (int, float)) else None,
        backend=str(payload.get("backend") or backend),
        message=message,
        verdict=Verdict.PASS if success else Verdict.FAIL,
        reasons=reasons,
    )


def _load_json_object(text: str) -> dict | None:
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        return None
    try:
        payload = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _dhcpcd_dump(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("'\"")
    return values


def _first_ipv4(payload: str) -> tuple[str, int | None]:
    try:
        data = json.loads(payload or "[]")
    except json.JSONDecodeError:
        return "", None
    if not data:
        return "", None
    for addr in data[0].get("addr_info") or []:
        if addr.get("family") == "inet":
            return str(addr.get("local") or ""), addr.get("prefixlen")
    return "", None


def _maybe_int(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None
