"""Append-only command log and raw evidence files for one run."""

from __future__ import annotations

import json
from pathlib import Path

from tc_net_cert.core.executor import CommandResult
from tc_net_cert.core.models import CommandLogEntry


class AuditTrail:
    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir
        self.raw = run_dir / "raw"
        self.command_log_path = self.raw / "system" / "commands.jsonl"
        self.entries: list[CommandLogEntry] = []
        self._closed = False
        for folder in ("ethtool", "ip", "iperf3", "dhcp", "ping", "system"):
            (self.raw / folder).mkdir(parents=True, exist_ok=True)

    def record_command(self, result: CommandResult) -> None:
        if self._closed:
            return
        stdout_file = ""
        stderr_file = ""
        if result.stdout:
            stdout_file = self._spill("system/stdout", result.stdout)
        if result.stderr:
            stderr_file = self._spill("system/stderr", result.stderr)
        entry = CommandLogEntry(
            timestamp=result.started_at,
            namespace=result.namespace or "",
            interface=result.interface or "",
            command=result.command or " ".join(result.argv),
            exit_code=result.exit_code,
            timed_out=result.timed_out,
            duration_s=round(result.duration_s, 3),
            stdout_file=stdout_file,
            stderr_file=stderr_file,
        )
        self.entries.append(entry)
        with self.command_log_path.open("a", encoding="utf-8") as handle:
            handle.write(entry.model_dump_json() + "\n")

    def write_raw(self, relative: str, content: str) -> str:
        path = self.raw / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return str(Path("raw") / relative)

    def close(self) -> None:
        self._closed = True

    def _spill(self, folder: str, content: str) -> str:
        index = len(self.entries) + 1
        relative = Path(folder) / f"{index:04d}.log"
        path = self.raw / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return str(Path("raw") / relative)


def read_command_log(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows
