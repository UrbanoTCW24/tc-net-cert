"""Subprocess execution with timeouts, process-group kill, and audit hooks."""

from __future__ import annotations

import contextvars
import os
import shlex
import signal
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol

from tc_net_cert.core.exceptions import MissingToolError

current_audit: contextvars.ContextVar[object | None] = contextvars.ContextVar(
    "tc_net_cert_audit",
    default=None,
)


@dataclass
class CommandResult:
    argv: list[str]
    exit_code: int
    stdout: str
    stderr: str
    duration_s: float
    timed_out: bool = False
    namespace: str | None = None
    interface: str | None = None
    stdout_path: str = ""
    stderr_path: str = ""
    started_at: str = ""
    command: str = ""

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


class Executor(Protocol):
    def run(
        self,
        argv: list[str],
        *,
        namespace: str | None = None,
        interface: str | None = None,
        timeout: float = 30,
        check: bool = False,
        input_text: str | None = None,
    ) -> CommandResult: ...


def which(tool: str) -> str | None:
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        candidate = os.path.join(directory, tool)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def require_tool(tool: str, hint: str) -> str:
    path = which(tool)
    if path is None:
        raise MissingToolError(tool, hint)
    return path


@dataclass
class SystemExecutor:
    """Run commands on the host or inside a network namespace."""

    audit_stdout: bool = True
    extra_env: dict[str, str] = field(default_factory=dict)

    def run(
        self,
        argv: list[str],
        *,
        namespace: str | None = None,
        interface: str | None = None,
        timeout: float = 30,
        check: bool = False,
        input_text: str | None = None,
    ) -> CommandResult:
        if namespace:
            command = ["ip", "netns", "exec", namespace, *argv]
        else:
            command = list(argv)
        started = datetime.now(timezone.utc)
        start = time.monotonic()
        timed_out = False
        proc = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.PIPE if input_text is not None else None,
            text=True,
            start_new_session=True,
            env={**os.environ, **self.extra_env},
        )
        try:
            stdout, stderr = proc.communicate(input=input_text, timeout=timeout)
            exit_code = proc.returncode if proc.returncode is not None else 1
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_group(proc.pid)
            stdout, stderr = proc.communicate(timeout=5)
            exit_code = proc.returncode if proc.returncode is not None else 124
            stderr = (stderr or "") + f"\nTIMEOUT after {timeout}s\n"
        duration = time.monotonic() - start
        result = CommandResult(
            argv=list(argv),
            exit_code=exit_code,
            stdout=stdout or "",
            stderr=stderr or "",
            duration_s=duration,
            timed_out=timed_out,
            namespace=namespace,
            interface=interface,
            started_at=started.isoformat(),
            command=shlex.join(argv),
        )
        audit = current_audit.get()
        if audit is not None:
            record = getattr(audit, "record_command", None)
            if record is not None:
                record(result)
        if check and not result.ok:
            detail = result.stderr.strip() or result.stdout.strip() or "command failed"
            raise RuntimeError(f"{result.command} -> {result.exit_code}: {detail}")
        return result


def _kill_group(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except PermissionError:
        return
    time.sleep(0.2)
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        return


class ScriptedExecutor:
    """Deterministic executor for tests. Records every call."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict[str, object]]] = []
        self._handlers: list[tuple[object, CommandResult | Exception]] = []
        self.default = CommandResult(
            argv=[],
            exit_code=0,
            stdout="",
            stderr="",
            duration_s=0.01,
            command="",
        )

    def when(self, predicate, result: CommandResult | Exception) -> None:
        self._handlers.append((predicate, result))

    def run(
        self,
        argv: list[str],
        *,
        namespace: str | None = None,
        interface: str | None = None,
        timeout: float = 30,
        check: bool = False,
        input_text: str | None = None,
    ) -> CommandResult:
        meta = {
            "namespace": namespace,
            "interface": interface,
            "timeout": timeout,
            "check": check,
            "input_text": input_text,
        }
        self.calls.append((list(argv), meta))
        chosen: CommandResult | Exception | None = None
        for predicate, result in self._handlers:
            if predicate(argv, meta):
                chosen = result
                break
        if isinstance(chosen, Exception):
            raise chosen
        if chosen is None:
            chosen = CommandResult(
                argv=list(argv),
                exit_code=self.default.exit_code,
                stdout=self.default.stdout,
                stderr=self.default.stderr,
                duration_s=self.default.duration_s,
                namespace=namespace,
                interface=interface,
                command=shlex.join(argv),
                started_at=datetime.now(timezone.utc).isoformat(),
            )
        else:
            chosen = CommandResult(
                argv=list(argv),
                exit_code=chosen.exit_code,
                stdout=chosen.stdout,
                stderr=chosen.stderr,
                duration_s=chosen.duration_s,
                timed_out=chosen.timed_out,
                namespace=namespace,
                interface=interface,
                command=shlex.join(argv),
                started_at=datetime.now(timezone.utc).isoformat(),
            )
        audit = current_audit.get()
        if audit is not None:
            record = getattr(audit, "record_command", None)
            if record is not None:
                record(chosen)
        if check and not chosen.ok:
            raise RuntimeError(chosen.command)
        return chosen
