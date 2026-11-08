"""Start and stop one iperf3 server per namespace. Operators do not open terminals."""

from __future__ import annotations

import os
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from tc_net_cert.core.exceptions import MissingToolError, ToolError
from tc_net_cert.core.executor import which
from tc_net_cert.core.models import Endpoint
from tc_net_cert.iperf.parser import IperfMeasurement, parse_iperf_json
from tc_net_cert.orchestration.guard import classify_iperf_failure


@dataclass
class RunningServer:
    endpoint_name: str
    namespace: str
    process: subprocess.Popen[str]


@dataclass
class IperfPool:
    servers: list[RunningServer] = field(default_factory=list)

    def start(self, endpoint: Endpoint) -> None:
        binary = which("iperf3")
        if binary is None:
            raise MissingToolError("iperf3", "Install it with: sudo apt-get install -y iperf3")
        # A long-lived server accepts clients one after another. Do not pass -1.
        command = [
            "ip",
            "netns",
            "exec",
            endpoint.namespace,
            binary,
            "-s",
            "-B",
            endpoint.address,
            "-p",
            str(endpoint.iperf_port),
        ]
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            stdout, stderr = process.communicate(timeout=0.4)
            raise ToolError(
                f"iperf3 server on {endpoint.logical_name} exited early: {(stderr or stdout).strip()}"
            )
        except subprocess.TimeoutExpired:
            self.servers.append(
                RunningServer(
                    endpoint_name=endpoint.logical_name,
                    namespace=endpoint.namespace,
                    process=process,
                )
            )

    def stop(self) -> None:
        for server in self.servers:
            _terminate(server.process)
        self.servers.clear()


def client_argv(
    namespace: str,
    source_ip: str,
    destination_ip: str,
    port: int,
    duration: int,
    streams: int,
    binary: str = "iperf3",
    interface: str = "",
) -> list[str]:
    """iperf3 client bound to one namespace address. The browser never supplies this argv."""
    if not namespace or not source_ip or not destination_ip:
        raise ToolError("iperf3 requires a namespace and a source and destination address")
    command = [
        "ip",
        "netns",
        "exec",
        namespace,
        binary,
        "-c",
        destination_ip,
        "-B",
        source_ip,
        "-p",
        str(port),
        "-P",
        str(streams),
        "-t",
        str(duration),
        "-J",
        "--forceflush",
        "--connect-timeout",
        "5000",
    ]
    if interface:
        command.extend(["--bind-dev", interface])
    return command


def _temp_file(suffix: str) -> str:
    descriptor, name = tempfile.mkstemp(prefix="tc-iperf-", suffix=suffix)
    os.close(descriptor)
    return name


def capture_command(
    command: list[str],
    timeout: float,
    cancel: threading.Event | None = None,
) -> tuple[str, str, int]:
    """Write iperf JSON to a file. A bind error or a cancel stops the client at once."""
    output = Path(_temp_file(".json"))
    error = Path(_temp_file(".err"))
    out_handle = output.open("w", encoding="utf-8")
    err_handle = error.open("w", encoding="utf-8")
    try:
        process = subprocess.Popen(
            command,
            stdout=out_handle,
            stderr=err_handle,
            text=True,
            start_new_session=True,
        )
    finally:
        out_handle.close()
        err_handle.close()
    code = 0
    stopped = ""
    deadline = time.monotonic() + timeout
    while True:
        finished = process.poll()
        stderr_now = error.read_text(encoding="utf-8", errors="replace")
        failure = classify_iperf_failure(stderr_now)
        if failure is not None:
            stopped = f"{failure['error']}: {failure['details']}"
            _signal(process)
            code = process.wait(timeout=5)
            if code is None:
                process.kill()
                code = process.wait(timeout=5) or 1
            break
        if cancel is not None and cancel.is_set():
            stopped = "CANCELLED"
            _signal(process)
            code = process.wait(timeout=5)
            if code is None:
                process.kill()
                code = process.wait(timeout=5) or 1
            break
        if finished is not None:
            code = finished
            break
        if time.monotonic() >= deadline:
            _signal(process)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            code = 124
            stopped = "TIMEOUT"
            break
        time.sleep(0.2)
    stdout = output.read_text(encoding="utf-8", errors="replace")
    stderr = error.read_text(encoding="utf-8", errors="replace")
    output.unlink(missing_ok=True)
    error.unlink(missing_ok=True)
    if stopped and stopped not in stderr:
        stderr = f"{stderr}\n{stopped}\n"
    return stdout, stderr, code


def run_client(
    source: Endpoint,
    destination: Endpoint,
    *,
    duration: int,
    streams: int,
    timeout: float,
    cancel: threading.Event | None = None,
) -> tuple[IperfMeasurement, str, str, int]:
    binary = which("iperf3")
    if binary is None:
        raise MissingToolError("iperf3", "Install it with: sudo apt-get install -y iperf3")
    command = client_argv(
        source.namespace,
        source.address,
        destination.address,
        destination.iperf_port,
        duration,
        streams,
        binary,
        interface=source.interface,
    )
    stdout, stderr, code = capture_command(command, timeout, cancel)
    measurement = parse_iperf_json(stdout or "")
    if code not in {0, 124} and measurement.ok:
        measurement.ok = False
        measurement.error = measurement.error or (stderr.strip() or f"iperf3 exit {code}")
    if code != 0 and not measurement.ok and not measurement.error:
        measurement.error = stderr.strip() or f"iperf3 exit {code}"
    return measurement, stdout or "", stderr or "", code


def read_process(process: subprocess.Popen[str], timeout: float) -> tuple[str, str, int]:
    """Keep the JSON already written if the client is stopped by the timeout."""
    try:
        stdout, stderr = process.communicate(timeout=timeout)
        code = process.returncode if process.returncode is not None else 1
        return _text(stdout), _text(stderr), code
    except subprocess.TimeoutExpired as exc:
        stdout = _text(exc.stdout)
        stderr = _text(exc.stderr)
        _signal(process)
        try:
            extra_out, extra_err = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            extra_out, extra_err = process.communicate(timeout=5)
        return stdout + _text(extra_out), stderr + _text(extra_err) + "\nTIMEOUT\n", 124


def _text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value


def _terminate(process: subprocess.Popen[str]) -> None:
    _signal(process)
    try:
        process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()


def _signal(process: subprocess.Popen[str]) -> None:
    import os
    import signal
    import time

    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    time.sleep(0.3)
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
