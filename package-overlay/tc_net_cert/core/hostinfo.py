"""Host facts stored with every run so the result can be reproduced."""

from __future__ import annotations

import os
import platform
import socket
import sys
from pathlib import Path

from tc_net_cert.core.models import HostInfo
from tc_net_cert.version import __version__


def collect_host_info() -> HostInfo:
    cpu = ""
    cpuinfo = Path("/proc/cpuinfo")
    if cpuinfo.exists():
        for line in cpuinfo.read_text(errors="replace").splitlines():
            if line.lower().startswith("model name"):
                cpu = line.split(":", 1)[1].strip()
                break
    memory_mb = None
    meminfo = Path("/proc/meminfo")
    if meminfo.exists():
        for line in meminfo.read_text(errors="replace").splitlines():
            if line.startswith("MemTotal:"):
                memory_mb = int(line.split()[1]) // 1024
                break
    os_name = ""
    os_release = Path("/etc/os-release")
    if os_release.exists():
        data: dict[str, str] = {}
        for line in os_release.read_text(errors="replace").splitlines():
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            data[key] = value.strip().strip('"')
        os_name = data.get("PRETTY_NAME") or data.get("NAME") or ""
    return HostInfo(
        hostname=socket.gethostname(),
        os=os_name or platform.platform(),
        kernel=platform.release(),
        cpu=cpu,
        memory_mb=memory_mb,
        python=sys.version.split()[0],
        tool_version=__version__,
    )


def read_proc_stat() -> tuple[int, int]:
    parts = Path("/proc/stat").read_text().splitlines()[0].split()[1:]
    nums = [int(item) for item in parts[:8]]
    idle = nums[3] + (nums[4] if len(nums) > 4 else 0)
    return sum(nums), idle


def memory_percent() -> float | None:
    text = Path("/proc/meminfo").read_text(errors="replace")
    values: dict[str, int] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, rest = line.split(":", 1)
        values[key] = int(rest.split()[0])
    total = values.get("MemTotal")
    available = values.get("MemAvailable")
    if not total or available is None:
        return None
    return round((1 - available / total) * 100, 1)


def load_average() -> list[float]:
    if not hasattr(os, "getloadavg"):
        return []
    return [round(value, 2) for value in os.getloadavg()]


def temperature_c() -> float | None:
    temps: list[float] = []
    root = Path("/sys/class/thermal")
    if not root.exists():
        return None
    for zone in root.glob("thermal_zone*/temp"):
        try:
            temps.append(int(zone.read_text().strip()) / 1000)
        except (OSError, ValueError):
            continue
    if not temps:
        return None
    return round(max(temps), 1)
