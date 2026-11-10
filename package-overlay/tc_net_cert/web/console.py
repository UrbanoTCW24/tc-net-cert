"""Launch one local certification at a time and report its progress.

The dashboard binds to loopback. Moving NICs still needs root, so a
non-root panel asks PolicyKit once per run instead of touching the host
network itself.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import threading
from pathlib import Path

from tc_net_cert.dut.huawei import (
    BRAND_ENV,
    HOST_ENV,
    MAC_ENV,
    MODEL_ENV,
    PASSWORD_ENV,
    PORTALS_ENV,
    SERIAL_ENV,
    USER_ENV,
    allowed_host,
)
from tc_net_cert.config.benches import bench_port_list
from tc_net_cert.config.loader import load_config
from tc_net_cert.orchestration.session import BENCH_CREDENTIALS_ENV, BENCH_ID_ENV, BENCH_PORTS_ENV
from tc_net_cert.storage.supabase_runs import SUPABASE_ENV, config_path
from tc_net_cert.wifi.throughput import PSK_24, PSK_50, READING_ENV, SSID_24, SSID_50, reading_path, stored_env
from tc_net_cert.dut.portals import portals_path

STAGES = (
    "all",
    "throughput",
    "dhcp",
    "physical",
    "cross",
    "simultaneous",
    "wifi",
    "bidirectional",
)
DURATIONS = (30, 60, 120, 300, 600, 1800, 3600, 7200)
DURATION_LABELS = {
    30: "30 s",
    60: "1 min",
    120: "2 min",
    300: "5 min",
    600: "10 min",
    1800: "30 min",
    3600: "1 h",
    7200: "2 h",
}
_TIMED = {"throughput", "simultaneous", "bidirectional", "all", "wifi"}
_DEFAULT_PREFIX = Path("/opt/tc-net-cert")
_PROGRAM = Path("/usr/local/bin/tc-net-cert")


def _runtime_python() -> Path:
    prefix = Path(os.environ.get("PREFIX", str(_DEFAULT_PREFIX)))
    candidate = prefix / "venv/bin/python3"
    if candidate.is_file():
        return candidate
    legacy = _DEFAULT_PREFIX / "venv/bin/python3"
    return legacy if legacy.is_file() else Path(sys.executable)


_PYTHON = _runtime_python()
_HELPER = Path("/usr/local/sbin/techcorp-lab-root")
_PKEXEC = Path("/usr/bin/pkexec")
_SUDO = Path("/usr/bin/sudo")
_ASKPASS = Path(__file__).resolve().parent / "askpass.sh"
_ZENITY = Path("/usr/bin/zenity")
_PASSWORDLESS: bool | None = None


class ConsoleBusy(RuntimeError):
    pass


def _clear_bench_readings_after_cert(bench_id: str) -> None:
    from tc_net_cert.storage.bench_identity import clear_all_bench_readings, clear_bench_reading

    bench = (bench_id or "").strip().upper()
    if bench in {"A", "B"}:
        clear_bench_reading(bench)
    else:
        clear_all_bench_readings()


_WIFI_ACTIONS = {"on": "up", "off": "down"}
_IFACE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{0,15}$")


class Console:
    def __init__(self, config_path: Path, log_path: Path) -> None:
        self.config_path = config_path
        self.log_path = log_path
        self._lock = threading.Lock()
        self._proc: subprocess.Popen[str] | None = None
        self._stage = ""
        self._duration: int | None = None
        self._message = ""
        self._stopping = False
        self._bench_id = ""
        self._cleared_readings_after_run = True
        self._log_fp = None

    @property
    def privileged(self) -> bool:
        return os.geteuid() == 0

    @property
    def can_launch(self) -> bool:
        return self.privileged or _PKEXEC.is_file() or (_SUDO.is_file() and _ZENITY.is_file() and _ASKPASS.is_file())

    def _release_log_fp(self) -> None:
        if self._log_fp is not None:
            try:
                self._log_fp.close()
            except OSError:
                pass
            self._log_fp = None

    def status(self) -> dict:
        with self._lock:
            own = self._own_state()
        if own is not None:
            return own
        external = _external_test()
        if external is not None:
            return {
                "running": True,
                "stage": external["stage"],
                "duration": external["duration"],
                "source": "terminal",
                "message": "Hay una prueba en curso desde la terminal. El certificado se actualiza al terminar.",
                "log": "",
                "exit_code": None,
                "can_launch": self.can_launch,
                "privileged": self.privileged,
            }
        with self._lock:
            code = None if self._proc is None else self._proc.poll()
            if self._proc is not None and code is not None:
                self._release_log_fp()
            if (
                self._proc is not None
                and code is not None
                and not self._cleared_readings_after_run
            ):
                _clear_bench_readings_after_cert(self._bench_id)
                self._cleared_readings_after_run = True
            log = _tail(self.log_path)
            message = self._message
            if code not in {None, 0} and not log.strip():
                if self.privileged:
                    message = (
                        "La prueba no arrancó (sin log). Pulsa Recuperar namespaces y vuelve a Correr."
                    )
                else:
                    message = "La prueba no arrancó. Pulsa Correr y escribe la contraseña en la ventana de TECHCORP LAB."
            elif code not in {None, 0}:
                last = log.strip().splitlines()[-1][:240]
                if "FINAL RESULT:" in log:
                    message = "La prueba terminó. " + last
                else:
                    message = "La prueba no arrancó. " + last
            elif code == 0:
                message = "La prueba terminó. El certificado está a la derecha."
            return {
                "running": False,
                "stage": self._stage,
                "duration": self._duration,
                "source": "",
                "message": message,
                "log": log,
                "exit_code": code,
                "can_launch": self.can_launch,
                "privileged": self.privileged,
                "bench_id": self._bench_id,
            }

    def start(
        self,
        stage: str,
        duration: int | None,
        *,
        job: str = "",
        dut_user: str = "",
        dut_password: str = "",
        dut_host: str = "",
        wifi: dict[str, str] | None = None,
        identity: dict[str, str] | None = None,
        bench_id: str = "",
        bench_credentials: dict | None = None,
    ) -> dict:
        if stage not in STAGES:
            raise ValueError("Prueba no reconocida")
        if stage in _TIMED:
            if duration not in DURATIONS:
                raise ValueError("La duración tiene que ser 30, 60, 120, 300 o 600 segundos")
        else:
            duration = None
        if not self.can_launch:
            raise ConsoleBusy("Este panel no puede pedir la contraseña. Falta zenity o sudo.")
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                raise ConsoleBusy("Ya hay una prueba lanzada desde el panel.")
            external = _external_test()
            if external is not None:
                raise ConsoleBusy(
                    f"Ya corre {external['stage']} en la terminal. Espera a que termine."
                )
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            self._release_log_fp()
            log = self.log_path.open("w", encoding="utf-8")
            self._log_fp = log
            env = os.environ.copy()
            env["PYTHONUNBUFFERED"] = "1"
            env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
            if dut_user:
                env[USER_ENV] = dut_user
            if dut_password:
                env[PASSWORD_ENV] = dut_password
            if allowed_host(dut_host):
                env[HOST_ENV] = dut_host
            for key, value in (identity or {}).items():
                if value:
                    env[key] = value
            bench = (bench_id or "").strip().upper()
            self._bench_id = bench
            self._cleared_readings_after_run = False
            if bench:
                env[BENCH_ID_ENV] = bench
                try:
                    config, _loaded = load_config(self.config_path)
                    names = bench_port_list(config, bench)
                    if names:
                        env[BENCH_PORTS_ENV] = ",".join(names)
                except Exception:
                    pass
                from tc_net_cert.storage.bench_identity import bench_wifi_reading_path

                wifi_file = bench_wifi_reading_path(bench)
                if wifi_file.is_file():
                    env[READING_ENV] = str(wifi_file)
                    for key, value in stored_env(wifi_file).items():
                        env.setdefault(key, value)
            env[PORTALS_ENV] = str(portals_path())
            env[SUPABASE_ENV] = str(config_path())
            dual_lab = isinstance(bench_credentials, dict) and bool(bench_credentials)
            if dual_lab:
                env[BENCH_CREDENTIALS_ENV] = json.dumps(bench_credentials, ensure_ascii=False)
            if not dual_lab:
                for key, value in (wifi or {}).items():
                    if value:
                        env[key] = value
                for key, value in stored_env().items():
                    env.setdefault(key, value)
            if not env.get(READING_ENV):
                env[READING_ENV] = str(reading_path())
            env["DISPLAY"] = os.environ.get("DISPLAY", ":0")
            env["XDG_RUNTIME_DIR"] = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
            env.setdefault(
                "DBUS_SESSION_BUS_ADDRESS",
                f"unix:path={env['XDG_RUNTIME_DIR']}/bus",
            )
            if _ASKPASS.is_file():
                env["SUDO_ASKPASS"] = str(_ASKPASS)
            launch_env = _write_launch_env(env) if not self.privileged and _passwordless() else None
            argv = _argv(self.config_path, stage, duration, privileged=self.privileged, env_file=launch_env)
            try:
                self._proc = subprocess.Popen(
                    argv,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    cwd="/",
                    env=env,
                    start_new_session=True,
                    text=True,
                )
            except OSError as exc:
                self._release_log_fp()
                raise ConsoleBusy(f"No se pudo iniciar la prueba: {exc}") from exc
            self._stage = stage
            self._duration = duration
            self._stopping = False
            if self.privileged or _passwordless():
                self._message = "Prueba en curso."
            else:
                self._message = "Escribe la contraseña en la ventana de TECHCORP LAB. Sin eso la prueba no mueve las tarjetas."
        return self.status()

    def stop(self) -> dict:
        """Ask the running certification to stop. SIGINT runs the same cleanup as a finished test."""
        with self._lock:
            proc = self._proc if self._proc is not None and self._proc.poll() is None else None
            pid = proc.pid if proc is not None else None
        if pid is None:
            external = _external_test()
            if external is None:
                raise ConsoleBusy("No hay una prueba en curso.")
            pid = int(external["pid"])
        _signal_tree(pid, privileged=self.privileged)
        with self._lock:
            self._stopping = True
            self._message = "Deteniendo la prueba. Las tarjetas vuelven a la PC al terminar la limpieza."
        return self.status()

    def set_wifi(self, action: str) -> str:
        """Bring the detected wireless NIC up or down. The name never comes from the browser."""
        verb = _WIFI_ACTIONS.get(action)
        if verb is None:
            raise ValueError("Acción no permitida")
        from tc_net_cert.web.live import detect_wifi

        info = detect_wifi()
        iface = str(info.get("iface") or "")
        if not _IFACE_RE.fullmatch(iface):
            raise ValueError("No hay una interfaz Wi-Fi identificada")
        sysfs = Path("/sys/class/net") / iface
        if not (sysfs / "wireless").exists() and not (sysfs / "phy80211").exists():
            raise ValueError("La interfaz no es inalámbrica")
        argv = ["ip", "link", "set", iface, verb]
        if not self.privileged:
            if _passwordless():
                argv = [str(_SUDO), "-n", "--", str(_HELPER), "link", iface, verb]
            elif _SUDO.is_file() and _ZENITY.is_file() and _ASKPASS.is_file():
                argv = [str(_SUDO), "-A", "--", *argv]
            else:
                raise ConsoleBusy("Este panel no puede pedir la contraseña para el Wi-Fi.")
        env = os.environ.copy()
        env["DISPLAY"] = os.environ.get("DISPLAY", ":0")
        env["XDG_RUNTIME_DIR"] = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        if _ASKPASS.is_file():
            env["SUDO_ASKPASS"] = str(_ASKPASS)
        try:
            result = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
                env=env,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ConsoleBusy(f"No se pudo cambiar el Wi-Fi: {exc}") from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "el cambio no se aplicó").strip().splitlines()
            raise ConsoleBusy(detail[-1][:240] if detail else "el cambio no se aplicó")
        return "UP" if verb == "up" else "OFF"

    def _own_state(self) -> dict | None:
        if self._proc is None or self._proc.poll() is not None:
            return None
        log = _tail(self.log_path)
        message = self._message
        if log.strip() and not self._stopping:
            message = "Prueba en curso."
        return {
            "running": True,
            "stage": self._stage,
            "duration": self._duration,
            "source": "panel",
            "message": message,
            "log": log,
            "exit_code": None,
            "can_launch": self.can_launch,
            "privileged": self.privileged,
            "bench_id": self._bench_id,
        }


def _signal_tree(pid: int, *, privileged: bool) -> None:
    targets = [pid, *_descendants(pid)]
    denied: list[int] = []
    for item in targets:
        try:
            os.kill(item, signal.SIGINT)
        except ProcessLookupError:
            continue
        except PermissionError:
            denied.append(item)
    if not denied:
        return
    if privileged or not (_passwordless() or (_SUDO.is_file() and _ZENITY.is_file() and _ASKPASS.is_file())):
        raise ConsoleBusy("La prueba corre como root y este panel no puede detenerla.")
    env = os.environ.copy()
    env["DISPLAY"] = os.environ.get("DISPLAY", ":0")
    env["XDG_RUNTIME_DIR"] = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    env["SUDO_ASKPASS"] = str(_ASKPASS)
    if _passwordless():
        stop_argv = [str(_SUDO), "-n", "--", str(_HELPER), "signal", "INT", *[str(item) for item in denied]]
    else:
        stop_argv = [str(_SUDO), "-A", "--", "kill", "-INT", *[str(item) for item in denied]]
    result = subprocess.run(
        stop_argv,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env=env,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "no se pudo detener").strip().splitlines()
        raise ConsoleBusy(detail[-1][:240] if detail else "no se pudo detener")


def _descendants(pid: int) -> list[int]:
    children: dict[int, list[int]] = {}
    proc = Path("/proc")
    if not proc.is_dir():
        return []
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            text = (entry / "status").read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            if line.startswith("PPid:"):
                parent = int(line.split()[1])
                children.setdefault(parent, []).append(int(entry.name))
                break
    found: list[int] = []
    pending = list(children.get(pid, []))
    while pending:
        item = pending.pop()
        found.append(item)
        pending.extend(children.get(item, []))
    return found


def _kept_env() -> str:
    return ",".join(
        [
            "PYTHONPATH",
            USER_ENV,
            PASSWORD_ENV,
            HOST_ENV,
            SERIAL_ENV,
            MAC_ENV,
            BRAND_ENV,
            MODEL_ENV,
            PORTALS_ENV,
            SUPABASE_ENV,
            SSID_24,
            PSK_24,
            SSID_50,
            PSK_50,
            READING_ENV,
            BENCH_ID_ENV,
            BENCH_PORTS_ENV,
            BENCH_CREDENTIALS_ENV,
        ]
    )


def _passwordless() -> bool:
    """True when this PC already authorized the lab helper. No password dialog."""
    global _PASSWORDLESS
    if _PASSWORDLESS is not None:
        return _PASSWORDLESS
    if not (_SUDO.is_file() and _HELPER.is_file()):
        _PASSWORDLESS = False
        return False
    try:
        result = subprocess.run(
            [str(_SUDO), "-n", "-l", str(_HELPER)],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        _PASSWORDLESS = False
        return False
    _PASSWORDLESS = result.returncode == 0
    return _PASSWORDLESS


def _write_launch_env(env: dict[str, str]) -> Path:
    """Hand the portal data to the root helper without asking sudo to keep those variables."""
    folder = Path.home() / ".local" / "state" / "tc-net-cert"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "launch.env.json"
    payload = {key: env[key] for key in _kept_env().split(",") if key != "PYTHONPATH" and env.get(key)}
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o600)
    return path


def _argv(
    config_path: Path,
    stage: str,
    duration: int | None,
    *,
    privileged: bool,
    env_file: Path | None = None,
) -> list[str]:
    if _PYTHON.is_file() and _PROGRAM.is_file():
        command = [str(_PYTHON), str(_PROGRAM)]
    else:
        command = ["tc-net-cert"]
    command.extend(["--config", str(config_path), "test", stage])
    if duration is not None:
        command.extend(["--duration", str(duration)])
    try:
        from tc_net_cert.config.loader import load_config

        cfg, _loaded = load_config(config_path)
        if len(cfg.ports) < cfg.dut_profile().ports:
            command.append("--allow-partial-ports")
    except OSError:
        pass
    if privileged:
        return command
    if _passwordless():
        helper = [str(_SUDO), "-n", "--", str(_HELPER), "test", "--config", str(config_path), "--stage", stage]
        if duration is not None:
            helper.extend(["--duration", str(duration)])
        try:
            from tc_net_cert.config.loader import load_config

            cfg, _loaded = load_config(config_path)
            if len(cfg.ports) < cfg.dut_profile().ports:
                helper.append("--allow-partial-ports")
        except OSError:
            pass
        if env_file is not None:
            helper.extend(["--env-file", str(env_file)])
        return helper
    if _SUDO.is_file() and _ZENITY.is_file() and _ASKPASS.is_file():
        return [str(_SUDO), "-A", f"--preserve-env={_kept_env()}", "--", *command]
    return [str(_PKEXEC), *command]


def _external_test() -> dict | None:
    proc = Path("/proc")
    if not proc.is_dir():
        return None
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        args = [part for part in raw.decode("utf-8", "replace").split("\0") if part]
        if "test" not in args:
            continue
        if not any(part.endswith("tc-net-cert") for part in args):
            continue
        stage_at = args.index("test") + 1
        stage = args[stage_at] if stage_at < len(args) else ""
        if stage not in STAGES:
            continue
        duration = None
        if "--duration" in args:
            at = args.index("--duration") + 1
            if at < len(args) and args[at].isdigit():
                duration = int(args[at])
        return {"stage": stage, "duration": duration, "pid": int(entry.name)}
    return None


def _tail(path: Path, limit: int = 120) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(lines[-limit:])
