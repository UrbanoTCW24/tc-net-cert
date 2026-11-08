"""Record USB Ethernet adapter identity so a bus limit is not blamed on the DUT."""

from __future__ import annotations

from pathlib import Path

from tc_net_cert.core.executor import Executor
from tc_net_cert.core.models import UsbInfo


def capture_usb_inventory(executor: Executor, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    listing = executor.run(["lsusb"], timeout=15)
    (destination / "lsusb.txt").write_text(listing.stdout + listing.stderr, encoding="utf-8")
    tree = executor.run(["lsusb", "-t"], timeout=15)
    (destination / "lsusb-t.txt").write_text(tree.stdout + tree.stderr, encoding="utf-8")


def usb_info_for(interface: str, sysfs_root: Path | None = None) -> UsbInfo:
    root = sysfs_root or Path("/sys/class/net")
    device = root / interface / "device"
    if not device.exists():
        return UsbInfo(is_usb=False, note="No sysfs device; treated as non-USB")
    try:
        current = device.resolve()
    except OSError:
        return UsbInfo(is_usb=False, note="sysfs device could not be resolved")
    for candidate in [current, *current.parents]:
        if (candidate / "idVendor").exists() and (candidate / "speed").exists():
            speed = _read_int(candidate / "speed")
            return UsbInfo(
                is_usb=True,
                vendor_id=_read_text(candidate / "idVendor"),
                product_id=_read_text(candidate / "idProduct"),
                manufacturer=_read_text(candidate / "manufacturer"),
                product=_read_text(candidate / "product"),
                bus=candidate.name,
                usb_speed_mbps=speed,
                usb_generation=_generation(speed),
                driver=_driver(current),
                note=_note(speed),
            )
    return UsbInfo(is_usb=False, note="Parent USB device not found")


def _generation(speed: int | None) -> str:
    if speed is None:
        return ""
    if speed >= 5000:
        return "USB 3.x"
    if speed >= 480:
        return "USB 2.0"
    if speed > 0:
        return "USB 1.x"
    return ""


def _note(speed: int | None) -> str:
    if speed is None:
        return ""
    if speed <= 480:
        return "USB 2.0 high-speed tops out at 480 Mbps and can limit a Gigabit DUT port"
    return ""


def _driver(device: Path) -> str:
    driver = device / "driver"
    try:
        if driver.exists():
            return driver.resolve().name
    except OSError:
        return ""
    return ""


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def _read_int(path: Path) -> int | None:
    text = _read_text(path)
    try:
        return int(text)
    except ValueError:
        return None
