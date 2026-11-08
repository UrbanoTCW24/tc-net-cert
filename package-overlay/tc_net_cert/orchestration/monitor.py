"""Sample host load and watch carrier state while iPerf is running."""

from __future__ import annotations

import threading
import time
from pathlib import Path

from tc_net_cert.core.executor import Executor
from tc_net_cert.core.hostinfo import load_average, memory_percent, read_proc_stat, temperature_c
from tc_net_cert.core.models import HostSample


class HostSampler:
    def __init__(self, interval: float = 2.0) -> None:
        self.interval = interval
        self.samples: list[float] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="tc-net-cert-cpu", daemon=True)
        self._thread.start()

    def stop(self) -> HostSample:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.interval + 2)
        if not self.samples:
            self.samples.append(_cpu_percent(0.2))
        return HostSample(
            cpu_percent_max=round(max(self.samples), 1),
            cpu_percent_avg=round(sum(self.samples) / len(self.samples), 1),
            memory_percent=memory_percent(),
            load_average=load_average(),
            temperature_c=temperature_c(),
        )

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.samples.append(_cpu_percent(min(0.5, self.interval)))
            self._stop.wait(self.interval)


class LinkWatch:
    """Record UP → DOWN → UP transitions inside one namespace."""

    def __init__(self, executor: Executor, interface: str, namespace: str) -> None:
        self.executor = executor
        self.interface = interface
        self.namespace = namespace
        self.events: list[str] = []
        self._last: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._last = self._carrier()
        self._thread = threading.Thread(target=self._loop, name=f"link-{self.interface}", daemon=True)
        self._thread.start()

    def stop(self) -> int:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)
        downs = self.events.count("down")
        ups_after = 0
        seen_down = False
        for event in self.events:
            if event == "down":
                seen_down = True
            elif event == "up" and seen_down:
                ups_after += 1
                seen_down = False
        return max(downs, ups_after)

    def _loop(self) -> None:
        while not self._stop.wait(1.0):
            current = self._carrier()
            if self._last == "1" and current == "0":
                self.events.append("down")
            elif self._last == "0" and current == "1":
                self.events.append("up")
            if current in {"0", "1"}:
                self._last = current

    def _carrier(self) -> str:
        sysfs = Path(f"/sys/class/net/{self.interface}/carrier")
        if self.namespace:
            result = self.executor.run(
                ["cat", f"/sys/class/net/{self.interface}/carrier"],
                namespace=self.namespace,
                interface=self.interface,
                timeout=5,
            )
            return result.stdout.strip()
        try:
            return sysfs.read_text(encoding="utf-8").strip()
        except OSError:
            return ""


def _cpu_percent(interval: float) -> float:
    try:
        total_1, idle_1 = read_proc_stat()
        time.sleep(interval)
        total_2, idle_2 = read_proc_stat()
    except OSError:
        return 0.0
    delta = total_2 - total_1
    if delta <= 0:
        return 0.0
    return max(0.0, min(100.0, (1 - (idle_2 - idle_1) / delta) * 100))
