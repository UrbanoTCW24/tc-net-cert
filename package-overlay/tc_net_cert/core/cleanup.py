"""Idempotent cleanup that runs on success, failure, SIGINT, and SIGTERM."""

from __future__ import annotations

import signal
import threading
from collections.abc import Callable
from types import FrameType


class CleanupController:
    def __init__(self) -> None:
        self._steps: list[tuple[str, Callable[[], None]]] = []
        self._lock = threading.Lock()
        self._ran = False
        self.errors: list[str] = []
        self._previous: dict[int, signal.Handlers] = {}

    def add(self, name: str, step: Callable[[], None]) -> None:
        self._steps.append((name, step))

    def run(self) -> list[str]:
        with self._lock:
            if self._ran:
                return list(self.errors)
            self._ran = True
            steps = list(self._steps)
        for name, step in reversed(steps):
            try:
                step()
            except Exception as exc:  # noqa: BLE001 - cleanup must continue
                self.errors.append(f"{name}: {exc}")
        return list(self.errors)

    def install_signals(self) -> None:
        def _handler(signum: int, _frame: FrameType | None) -> None:
            self.run()
            raise SystemExit(128 + signum)

        for sig in (signal.SIGINT, signal.SIGTERM):
            self._previous[sig] = signal.signal(sig, _handler)

    def restore_signals(self) -> None:
        for sig, previous in self._previous.items():
            signal.signal(sig, previous)
        self._previous.clear()
