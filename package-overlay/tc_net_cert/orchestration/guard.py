"""Stop a window before GO, and stop it again if the socket or the DUT dies.

The panel must not say a port is transmitting until the kernel counters move.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path

from tc_net_cert.core.executor import Executor
from tc_net_cert.dhcp.acquire import is_link_local

TRAFFIC_FLOOR_MBPS = 1.0
QUIET_SECONDS = 2.0
CARRIER_DOWN_SECONDS = 4.0
_BIND_TEXT = "Cannot assign requested address"
_SOCKET_ERRORS = (
    ("No route to host", "EHOSTUNREACH"),
    ("Connection refused", "ECONNREFUSED"),
    ("Connection reset by peer", "ECONNRESET"),
)


def validate_interface(iface: str, carrier: str, address: str) -> dict[str, str | bool]:
    """Carrier must be up and the address must be a real DHCP lease."""
    name = iface or "la interfaz"
    if str(carrier).strip() != "1":
        return {
            "ok": False,
            "status": "INTERFACE_DOWN",
            "address": "",
            "details": f"{name} no tiene enlace físico",
        }
    ip = (address or "").split("/")[0].strip()
    if not ip or is_link_local(ip):
        shown = ip or "sin dirección"
        return {
            "ok": False,
            "status": "NO_IP",
            "address": "",
            "details": f"{name} no tiene IP del equipo ({shown})",
        }
    return {"ok": True, "status": "READY", "address": ip, "details": ""}


def validateInterface(
    iface: str,
    carrier: str | None = None,
    address: str = "",
    *,
    namespace: str = "",
    executor: Executor | None = None,
) -> dict[str, str | bool]:
    """Read one LAN before GO. A missing carrier or address aborts that window."""
    if carrier is None:
        carrier = _read_text(executor, namespace, iface, "carrier")
    if not address:
        address = _ipv4(executor, namespace, iface)
    return validate_interface(iface, carrier, address)


def classify_iperf_failure(text: str) -> dict[str, str] | None:
    """Bind and socket errors are fatal. iperf3 must not keep the clock running."""
    raw = text or ""
    if _BIND_TEXT in raw or "unable to bind" in raw.lower():
        return {"status": "FAILED", "error": "BIND_ERROR", "details": _BIND_TEXT}
    for phrase, code in _SOCKET_ERRORS:
        if phrase in raw:
            return {"status": "FAILED", "error": code, "details": phrase}
    return None


def traffic_led(tx_mbps: float | None, rx_mbps: float | None, quiet_for: float) -> dict[str, str | float | None]:
    """The LED follows kernel bytes. A live process with no bytes is not transmission."""
    if tx_mbps is None and rx_mbps is None:
        return {"led": "off", "alarm": "", "mbps": None}
    level = max(tx_mbps or 0.0, rx_mbps or 0.0)
    if level > TRAFFIC_FLOOR_MBPS:
        return {"led": "on", "alarm": "", "mbps": level}
    if quiet_for > QUIET_SECONDS:
        return {"led": "off", "alarm": "FAIL_NO_TRAFFIC", "mbps": level}
    return {"led": "off", "alarm": "", "mbps": level}


class UnitWatch:
    """Carrier inside the namespaces, twice a second.

    A flat byte counter must not abort the flow. That check cut a live
    gigabit transfer: the counter read stalled and iperf3 was killed near 40 s.
    """

    def __init__(
        self,
        executor: Executor,
        endpoints: list[tuple[str, str, str]],
        on_down: Callable[[list[str]], None] | None = None,
    ) -> None:
        self.executor = executor
        self.endpoints = endpoints
        self.on_down = on_down
        self.cancel = threading.Event()
        self.reason = ""
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._down_since: float | None = None
        self._announced = False

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="tc-unit-watch", daemon=True)
        self._thread.start()

    def stop(self) -> str:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        return self.reason

    def _loop(self) -> None:
        while not self._stop.wait(0.5):
            carriers = [self._carrier(iface, namespace) for _name, iface, namespace in self.endpoints]
            now = time.monotonic()
            down = [
                name
                for (name, _iface, _namespace), carrier in zip(self.endpoints, carriers, strict=True)
                if carrier == "0"
            ]
            if carriers and any(item == "0" for item in carriers):
                if self._down_since is None:
                    self._down_since = now
                    continue
                if now - self._down_since < CARRIER_DOWN_SECONDS:
                    continue
                if not self._announced and self.on_down is not None:
                    self._announced = True
                    self.on_down(down)
                if all(item == "0" for item in carriers):
                    self.reason = "UNIT_POWER_OFF_OR_REBOOT"
                else:
                    self.reason = "UNIT_POWER_OFF_OR_REBOOT " + ",".join(down)
                self.cancel.set()
                return
            self._down_since = None
            self._announced = False

    def _carrier(self, iface: str, namespace: str) -> str:
        return _read_text(self.executor, namespace, iface, "carrier")


def _read_text(executor: Executor | None, namespace: str, iface: str, leaf: str) -> str:
    if not iface or not _safe_iface(iface):
        return ""
    path = f"/sys/class/net/{iface}/{leaf}"
    if namespace and executor is not None:
        result = executor.run(["cat", path], namespace=namespace, interface=iface, timeout=3)
        return (result.stdout or "").strip()
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _ipv4(executor: Executor | None, namespace: str, iface: str) -> str:
    if not iface or not _safe_iface(iface):
        return ""
    argv = ["ip", "-4", "-br", "addr", "show", "dev", iface]
    if executor is not None:
        result = executor.run(argv, namespace=namespace or None, interface=iface, timeout=3)
        text = result.stdout or ""
    else:
        return ""
    parts = text.split()
    for part in parts:
        if part.count(".") == 3:
            return part.split("/")[0]
    return ""


def _safe_iface(iface: str) -> bool:
    return bool(iface) and len(iface) <= 15 and all(char.isalnum() or char in "._-" for char in iface)
