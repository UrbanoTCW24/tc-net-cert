"""Separate a slow measurement from an automatic DUT failure.

A low iPerf result is not, by itself, an ONT failure. USB 2.0, a saturated
CPU, or a link that never reached the profile speed are host-side or link-side
limits and stay labeled as such.
"""

from __future__ import annotations

from tc_net_cert.core.models import Bottleneck, UsbInfo


def assess_bottleneck(
    *,
    throughput_mbps: float | None,
    minimum_mbps: float,
    link_mbps: int | None,
    expected_link_mbps: int,
    cpu_percent: float | None,
    cpu_busy_percent: float,
    usb: UsbInfo | None,
    nic_max_mbps: int | None,
) -> tuple[Bottleneck, str]:
    if throughput_mbps is None:
        return Bottleneck.UNKNOWN, "No throughput measurement"
    if throughput_mbps + 1e-6 >= minimum_mbps:
        return Bottleneck.NONE, "Throughput meets the configured minimum"
    if link_mbps is not None and link_mbps < expected_link_mbps:
        return (
            Bottleneck.LINK_LIMIT,
            f"DUT link negotiated {link_mbps} Mbps, profile expects {expected_link_mbps} Mbps",
        )
    if nic_max_mbps is not None and nic_max_mbps < expected_link_mbps:
        return (
            Bottleneck.NIC_LIMIT,
            f"NIC advertises {nic_max_mbps} Mbps, below the profile speed {expected_link_mbps} Mbps",
        )
    usb_cap = _usb_practical_mbps(usb)
    if usb_cap is not None and usb_cap < expected_link_mbps and throughput_mbps <= usb_cap * 1.05:
        generation = usb.usb_generation if usb else "USB"
        return (
            Bottleneck.USB_LIMIT,
            f"{generation} practical ceiling is about {usb_cap:.0f} Mbps",
        )
    if cpu_percent is not None and cpu_percent >= cpu_busy_percent:
        return Bottleneck.HOST_LIMIT, f"Host CPU reached {cpu_percent:.0f}%"
    if (cpu_percent is None or cpu_percent < 70) and (usb_cap is None or usb_cap >= expected_link_mbps):
        return (
            Bottleneck.DUT_LIMIT,
            f"Throughput {throughput_mbps:.0f} Mbps is below {minimum_mbps:.0f} Mbps "
            "with link, NIC, USB, and CPU not indicating a host limit",
        )
    return Bottleneck.UNKNOWN, "Throughput is below the minimum and the limiting side is not identified"


def _usb_practical_mbps(usb: UsbInfo | None) -> float | None:
    if usb is None or not usb.is_usb or usb.usb_speed_mbps is None:
        return None
    if usb.usb_speed_mbps <= 480:
        return 300.0
    return float(usb.usb_speed_mbps)
