"""Run a DHCPv4 exchange inside the caller's network namespace and print JSON."""

from __future__ import annotations

import argparse
import json
import random
import socket
import subprocess
import sys
import time
from pathlib import Path

from tc_net_cert.dhcp.protocol import ACK, DISCOVER, NAK, OFFER, RELEASE, REQUEST, build_packet, parse_packet, prefix_from_mask


def mac_bytes(interface: str) -> bytes:
    text = Path(f"/sys/class/net/{interface}/address").read_text(encoding="utf-8").strip()
    return bytes.fromhex(text.replace(":", ""))


def acquire(interface: str, timeout: float) -> dict[str, object]:
    started = time.monotonic()
    mac = mac_bytes(interface)
    subprocess.run(["ip", "link", "set", interface, "up"], check=False, capture_output=True, text=True)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        if hasattr(socket, "SO_BINDTODEVICE"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, interface.encode() + b"\x00")
        sock.bind(("0.0.0.0", 68))
        sock.settimeout(1.0)
        deadline = time.monotonic() + timeout
        xid = random.SystemRandom().randint(1, 0xFFFFFFFF)
        offer = _exchange(sock, mac, xid, DISCOVER, deadline, OFFER)
        ack = _exchange(
            sock,
            mac,
            xid,
            REQUEST,
            deadline,
            ACK,
            requested_ip=str(offer["yiaddr"]),
            server_id=str(offer["server_id"]),
        )
    finally:
        sock.close()
    ipaddr = str(ack["yiaddr"] or offer["yiaddr"])
    mask = str(ack["subnet_mask"] or offer["subnet_mask"] or "")
    if mask:
        prefix = prefix_from_mask(mask)
        mask_source = "dhcp"
    else:
        prefix = _classful_prefix(ipaddr)
        mask = _prefix_to_mask(prefix)
        mask_source = "classful-fallback"
    gateway = str(ack["gateway"] or offer["gateway"] or "")
    configured = _configure(interface, ipaddr, prefix, gateway)
    return {
        "success": configured["ok"],
        "ip": ipaddr,
        "prefixlen": prefix,
        "mask": mask,
        "mask_source": mask_source,
        "gateway": gateway,
        "dns": ack["dns"] or offer["dns"],
        "lease_seconds": ack["lease_seconds"] if ack["lease_seconds"] is not None else offer["lease_seconds"],
        "dhcp_server": ack["server_id"] or offer["server_id"],
        "acquisition_seconds": round(time.monotonic() - started, 3),
        "backend": "internal",
        "message": configured["message"],
        "commands": configured["commands"],
    }


def release(interface: str, ipaddr: str, server_id: str) -> dict[str, object]:
    mac = mac_bytes(interface)
    xid = random.SystemRandom().randint(1, 0xFFFFFFFF)
    packet = build_packet(
        mac=mac,
        xid=xid,
        message_type=RELEASE,
        server_id=server_id,
        ciaddr=ipaddr,
    )
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_BINDTODEVICE"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, interface.encode() + b"\x00")
        sock.bind((ipaddr, 68))
        sock.sendto(packet, (server_id, 67))
    finally:
        sock.close()
    subprocess.run(["ip", "addr", "flush", "dev", interface], check=False, capture_output=True, text=True)
    return {"success": True, "backend": "internal", "message": "DHCPRELEASE sent"}


def _exchange(
    sock: socket.socket,
    mac: bytes,
    xid: int,
    message_type: int,
    deadline: float,
    expected: int,
    requested_ip: str = "",
    server_id: str = "",
) -> dict[str, object]:
    packet = build_packet(
        mac=mac,
        xid=xid,
        message_type=message_type,
        requested_ip=requested_ip,
        server_id=server_id,
    )
    while time.monotonic() < deadline:
        sock.sendto(packet, ("255.255.255.255", 67))
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        sock.settimeout(min(2.0, remaining))
        try:
            data, _addr = sock.recvfrom(2048)
        except TimeoutError:
            continue
        except socket.timeout:
            continue
        try:
            parsed = parse_packet(data)
        except ValueError:
            continue
        if parsed["xid"] != xid:
            continue
        if parsed["message_type"] == NAK:
            raise RuntimeError("DHCP NAK from server")
        if parsed["message_type"] == expected:
            return parsed
    raise TimeoutError("DHCP timed out waiting for a server response")


def _configure(interface: str, ipaddr: str, prefix: int, gateway: str) -> dict[str, object]:
    commands: list[str] = []
    addr = ["ip", "addr", "replace", f"{ipaddr}/{prefix}", "dev", interface]
    commands.append(" ".join(addr))
    result = subprocess.run(addr, capture_output=True, text=True)
    if result.returncode != 0:
        return {"ok": False, "message": result.stderr.strip() or "address configuration failed", "commands": commands}
    if gateway:
        route = ["ip", "route", "replace", "default", "via", gateway, "dev", interface]
        commands.append(" ".join(route))
        routed = subprocess.run(route, capture_output=True, text=True)
        if routed.returncode != 0:
            return {"ok": False, "message": routed.stderr.strip() or "route configuration failed", "commands": commands}
    return {"ok": True, "message": "lease applied", "commands": commands}


def _classful_prefix(ipaddr: str) -> int:
    first = int(ipaddr.split(".", 1)[0])
    if first < 128:
        return 8
    if first < 192:
        return 16
    return 24


def _prefix_to_mask(prefix: int) -> str:
    import ipaddress

    return str(ipaddress.IPv4Network(f"0.0.0.0/{prefix}").netmask)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Namespace-local DHCPv4 client")
    parser.add_argument("--iface", required=True)
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--release", action="store_true")
    parser.add_argument("--ip", default="")
    parser.add_argument("--server", default="")
    args = parser.parse_args(argv)
    try:
        if args.release:
            payload = release(args.iface, args.ip, args.server)
        else:
            payload = acquire(args.iface, args.timeout)
    except Exception as exc:  # noqa: BLE001 - process boundary reports JSON
        payload = {"success": False, "backend": "internal", "message": str(exc), "commands": []}
        json.dump(payload, sys.stdout)
        sys.stdout.write("\n")
        return 1
    json.dump(payload, sys.stdout)
    sys.stdout.write("\n")
    return 0 if payload.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(main())
