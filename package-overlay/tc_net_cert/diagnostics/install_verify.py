"""Post-install verification report (E2). Read-only; no secrets in output."""

from __future__ import annotations

import importlib.util
import os
import shutil
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from tc_net_cert.config.loader import default_config_path, load_config, runs_directory
from tc_net_cert.config.station_ports import _configured_port_issues, _certification_candidates
from tc_net_cert.core.executor import SystemExecutor
from tc_net_cert.network.discovery import discover_interfaces, index_by_mac
from tc_net_cert.network.management import detect_management, mark_protected


@dataclass(frozen=True)
class CheckLine:
    status: str  # PASS, WARN, FAIL
    label: str
    detail: str = ""


def _prefix() -> Path:
    return Path(os.environ.get("PREFIX", "/opt/tc-net-cert"))


def _venv_python() -> Path:
    return _prefix() / "venv/bin/python3"


def _run(cmd: list[str], *, timeout: int = 8) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _systemd_unavailable(text: str) -> bool:
    lowered = text.lower()
    return "systemd as init" in lowered or "failed to connect to system scope bus" in lowered


def _resolve_config(explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit
    system = Path("/etc/tc-net-cert/config.yaml")
    if system.is_file():
        return system
    return default_config_path()


def _importable(module: str) -> bool:
    return importlib.util.find_spec(module) is not None


def _check_python() -> CheckLine:
    if sys.version_info >= (3, 12):
        return CheckLine("PASS", "Python compatible", sys.version.split()[0])
    return CheckLine("FAIL", "Python compatible", f"requiere 3.12+, hay {sys.version.split()[0]}")


def _check_venv() -> CheckLine:
    python = _venv_python()
    if python.is_file() and os.access(python, os.X_OK):
        return CheckLine("PASS", "Entorno virtual", str(python))
    return CheckLine("FAIL", "Entorno virtual", f"no encontrado: {python}")


def _check_python_deps() -> CheckLine:
    missing = [name for name in ("typer", "pydantic", "yaml", "jinja2") if not _importable(name)]
    if _importable("psycopg"):
        psycopg = "psycopg OK"
    else:
        missing.append("psycopg")
        psycopg = "psycopg falta"
    if missing:
        return CheckLine("FAIL", "Dependencias Python", ", ".join(missing))
    return CheckLine("PASS", "Dependencias Python", psycopg)


def _check_network_tools() -> CheckLine:
    tools = ("iperf3", "ethtool", "ip", "ping")
    missing = [name for name in tools if shutil.which(name) is None]
    if missing:
        return CheckLine("FAIL", "Herramientas de red", "faltan: " + ", ".join(missing))
    return CheckLine("PASS", "Herramientas de red", "iperf3, ethtool, ip, ping")


def _check_network_manager() -> CheckLine:
    if shutil.which("nmcli") is None:
        return CheckLine("FAIL", "NetworkManager", "nmcli no instalado")
    active = _run(["systemctl", "is-active", "NetworkManager"])
    if active.stdout.strip() == "active":
        return CheckLine("PASS", "NetworkManager", "activo")
    detail = (active.stderr or active.stdout or "inactivo").strip()[:120]
    if _systemd_unavailable(detail):
        return CheckLine("WARN", "NetworkManager", "systemd no disponible en este entorno")
    return CheckLine("FAIL", "NetworkManager", detail)


def _check_port_config(config_path: Path) -> CheckLine:
    try:
        config, _ = load_config(config_path)
    except Exception as exc:
        return CheckLine("FAIL", "Configuración de interfaces", str(exc)[:160])
    if not config.ports:
        return CheckLine("WARN", "Configuración de interfaces", "sin LAN asignadas; ejecuta instalar.sh")
    executor = SystemExecutor()
    found = discover_interfaces(executor)
    marked = mark_protected(found, detect_management(executor, found))
    try:
        by_mac = index_by_mac(marked)
    except RuntimeError as exc:
        return CheckLine("FAIL", "Configuración de interfaces", f"MAC duplicada: {exc}")
    issues = _configured_port_issues(config, by_mac)
    if issues:
        return CheckLine("FAIL", "Configuración de interfaces", "; ".join(issues)[:200])
    count = len(config.ports)
    usable = len(_certification_candidates(executor))
    detail = f"{count} LAN configuradas"
    if count < 4:
        return CheckLine(
            "WARN",
            "Configuración de interfaces",
            f"{detail}; certificación 4-LAN no completa (detectadas {usable} aptas)",
        )
    return CheckLine("PASS", "Configuración de interfaces", detail)


def _check_operator_sudo(operator: str) -> CheckLine:
    if not operator or operator == "root":
        return CheckLine("WARN", "Permisos del operador", "instala con sudo desde tu usuario para NOPASSWD")
    helper = Path("/usr/local/sbin/techcorp-lab-root")
    if not helper.is_file():
        return CheckLine("FAIL", "Permisos del operador", "falta techcorp-lab-root")
    sudoers = Path("/etc/sudoers.d/techcorp-lab")
    if sudoers.is_file():
        text = sudoers.read_text(encoding="utf-8", errors="replace")
        if operator in text and "NOPASSWD" in text and "techcorp-lab-root" in text:
            return CheckLine("PASS", "Permisos del operador", operator)
    return CheckLine(
        "WARN",
        "Permisos del operador",
        f"{operator} tendrá que escribir la contraseña al certificar",
    )


def _check_web_service() -> CheckLine:
    active = _run(["systemctl", "is-active", "tc-net-cert-web.service"])
    if active.stdout.strip() == "active":
        return CheckLine("PASS", "Servicio web", "tc-net-cert-web activo")
    detail = (active.stderr or active.stdout or "inactivo").strip()[:120]
    if _systemd_unavailable(detail):
        return CheckLine("WARN", "Servicio web", "systemd no disponible en este entorno")
    return CheckLine("FAIL", "Servicio web", detail)


def _check_panel_http(host: str = "127.0.0.1", port: int = 8080) -> CheckLine:
    import time

    url = f"http://{host}:{port}/"
    last_error = ""
    code = 0
    for attempt in range(5):
        if attempt:
            time.sleep(1.5)
        try:
            with urllib.request.urlopen(url, timeout=6) as response:
                code = response.status
            last_error = ""
            break
        except urllib.error.HTTPError as exc:
            code = exc.code
            last_error = ""
            break
        except (OSError, urllib.error.URLError, socket.timeout) as exc:
            last_error = str(exc)
            code = 0
    if last_error:
        service = _run(["systemctl", "is-active", "tc-net-cert-web.service"])
        if service.stdout.strip() == "active":
            return CheckLine(
                "WARN",
                "Panel HTTP",
                "servicio activo pero sin respuesta; reintenta: sudo systemctl restart tc-net-cert-web",
            )
        return CheckLine("FAIL", "Panel HTTP", last_error[:160])
    if code == 200:
        return CheckLine("PASS", "Panel HTTP", url)
    return CheckLine("FAIL", "Panel HTTP", f"HTTP {code}")


def _check_local_storage(config_path: Path) -> CheckLine:
    try:
        config, _ = load_config(config_path)
        runs_root = runs_directory(config)
    except Exception as exc:
        return CheckLine("FAIL", "Base de datos local", str(exc)[:160])
    try:
        runs_root.mkdir(parents=True, exist_ok=True)
        test = runs_root / ".write-test"
        test.write_text("ok", encoding="utf-8")
        test.unlink(missing_ok=True)
    except OSError as exc:
        return CheckLine("FAIL", "Base de datos local", f"runs no escribible: {exc}")
    index = runs_root / "index.sqlite"
    if index.is_file():
        return CheckLine("PASS", "Base de datos local", f"{runs_root} (index.sqlite presente)")
    return CheckLine("PASS", "Base de datos local", str(runs_root))


def _check_supabase() -> CheckLine:
    from tc_net_cert.storage.supabase_runs import _settings_error, config_path, listed_run_ids

    path = config_path()
    if not path.is_file():
        return CheckLine("WARN", "Supabase", "no configurado (corridas solo locales)")
    err = _settings_error(path)
    if err:
        return CheckLine("WARN", "Supabase", err[:160])
    listed = listed_run_ids(path)
    if listed is None:
        return CheckLine("WARN", "Supabase", "credenciales presentes pero no hay conexión ahora")
    return CheckLine("PASS", "Supabase", "conexión OK")


def _check_certification_pending() -> CheckLine:
    return CheckLine("WARN", "Prueba de certificación real", "pendiente (requiere DUT y cables)")


def run_install_verify(
    *,
    config_path: Path | None = None,
    operator: str = "",
    panel_host: str = "127.0.0.1",
    panel_port: int = 8080,
) -> tuple[list[CheckLine], str, int]:
    path = _resolve_config(config_path)
    lines = [
        _check_python(),
        _check_venv(),
        _check_python_deps(),
        _check_network_tools(),
        _check_network_manager(),
        _check_port_config(path),
        _check_operator_sudo(operator or os.environ.get("SUDO_USER", "")),
        _check_web_service(),
        _check_panel_http(panel_host, panel_port),
        _check_local_storage(path),
        _check_supabase(),
        _check_certification_pending(),
    ]
    if any(item.status == "FAIL" for item in lines):
        state = "INSTALACIÓN INCOMPLETA O CON ERRORES"
        code = 1
    elif any(item.status == "WARN" for item in lines):
        state = "INSTALACIÓN COMPLETADA CON ADVERTENCIAS"
        code = 0
    else:
        state = "INSTALACIÓN COMPLETADA"
        code = 0
    return lines, state, code


def format_report(lines: list[CheckLine], state: str) -> str:
    out = ["TECHCORP LAB — VERIFICACIÓN DE INSTALACIÓN", ""]
    for item in lines:
        suffix = f" ({item.detail})" if item.detail else ""
        out.append(f"[{item.status}] {item.label}{suffix}")
    out.extend(["", f"Estado: {state}", ""])
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Verify TECHCORP LAB installation.")
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--operator", default=os.environ.get("SUDO_USER", ""))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args(argv)
    lines, state, code = run_install_verify(
        config_path=args.config,
        operator=args.operator,
        panel_host=args.host,
        panel_port=args.port,
    )
    print(format_report(lines, state))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
