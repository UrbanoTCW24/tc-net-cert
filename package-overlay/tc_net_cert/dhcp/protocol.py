"""Minimal DHCPv4 DISCOVER/REQUEST/ACK codec (RFC 2131)."""

from __future__ import annotations

import socket
import struct

MAGIC = b"\x63\x82\x53\x63"
DISCOVER = 1
OFFER = 2
REQUEST = 3
ACK = 5
NAK = 6
RELEASE = 7


def build_packet(
    *,
    mac: bytes,
    xid: int,
    message_type: int,
    requested_ip: str = "",
    server_id: str = "",
    ciaddr: str = "",
) -> bytes:
    if len(mac) != 6:
        raise ValueError("MAC must be 6 bytes")
    chaddr = mac + b"\x00" * 10
    header = struct.pack(
        "!BBBBIHH4s4s4s4s16s64s128s",
        1,
        1,
        6,
        0,
        xid,
        0,
        0x8000,
        socket.inet_aton(ciaddr or "0.0.0.0"),
        b"\x00\x00\x00\x00",
        b"\x00\x00\x00\x00",
        b"\x00\x00\x00\x00",
        chaddr,
        b"\x00" * 64,
        b"\x00" * 128,
    )
    options = bytearray()
    options += bytes([53, 1, message_type])
    options += bytes([61, 7, 1, *mac])
    if requested_ip:
        options += bytes([50, 4, *socket.inet_aton(requested_ip)])
    if server_id:
        options += bytes([54, 4, *socket.inet_aton(server_id)])
    if message_type in {DISCOVER, REQUEST}:
        options += bytes([55, 4, 1, 3, 6, 51])
    options += bytes([255])
    packet = header + MAGIC + bytes(options)
    if len(packet) < 300:
        packet += b"\x00" * (300 - len(packet))
    return packet


def parse_packet(packet: bytes) -> dict[str, object]:
    if len(packet) < 240 or packet[236:240] != MAGIC:
        raise ValueError("Not a DHCP packet")
    xid = struct.unpack_from("!I", packet, 4)[0]
    yiaddr = socket.inet_ntoa(packet[16:20])
    options = _parse_options(packet[240:])
    message_type = options.get(53, b"\x00")[0]
    return {
        "xid": xid,
        "yiaddr": yiaddr,
        "message_type": message_type,
        "subnet_mask": _opt_ip(options, 1),
        "gateway": _opt_ip(options, 3),
        "dns": _opt_ip_list(options, 6),
        "lease_seconds": _opt_u32(options, 51),
        "server_id": _opt_ip(options, 54),
    }


def prefix_from_mask(mask: str) -> int:
    import ipaddress

    return ipaddress.IPv4Network(f"0.0.0.0/{mask}").prefixlen


def _parse_options(data: bytes) -> dict[int, bytes]:
    options: dict[int, bytes] = {}
    index = 0
    while index < len(data):
        code = data[index]
        if code == 255:
            break
        if code == 0:
            index += 1
            continue
        if index + 1 >= len(data):
            break
        length = data[index + 1]
        start = index + 2
        options[code] = data[start : start + length]
        index = start + length
    return options


def _opt_ip(options: dict[int, bytes], code: int) -> str:
    raw = options.get(code, b"")
    if len(raw) < 4:
        return ""
    return socket.inet_ntoa(raw[:4])


def _opt_ip_list(options: dict[int, bytes], code: int) -> list[str]:
    raw = options.get(code, b"")
    return [socket.inet_ntoa(raw[offset : offset + 4]) for offset in range(0, len(raw) - 3, 4)]


def _opt_u32(options: dict[int, bytes], code: int) -> int | None:
    raw = options.get(code, b"")
    if len(raw) < 4:
        return None
    return struct.unpack("!I", raw[:4])[0]
