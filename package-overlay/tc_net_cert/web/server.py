"""Local operator console on 127.0.0.1.

The page starts a certification and shows the DUT certificate. It does not
bind on any other address.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from tc_net_cert.config.benches import live_ports_mac_map, resolve_benches
from tc_net_cert.config.loader import load_config, runs_directory
from tc_net_cert.storage.bench_identity import load_all as load_bench_identities
from tc_net_cert.storage.bench_identity import load_all_inventories
from tc_net_cert.core.executor import SystemExecutor
from tc_net_cert.network.management import refresh_portal_links
from tc_net_cert.dut.huawei import (
    BRAND_ENV,
    MAC_ENV,
    MODEL_ENV,
    SERIAL_ENV,
    allowed_host,
    discover_portal,
    dut_paths,
    portal_retryable,
    read_portal,
    valid_password,
    valid_user,
)
from tc_net_cert.dut.portals import (
    delete_portal,
    ensure_portals,
    load_portals,
    portals_path,
    profile_for,
    save_portal,
    valid_model,
)
from tc_net_cert.dut.portals_spreadsheet import export_portals_csv, import_portals_csv, list_portal_rows
from tc_net_cert.core.models import AppConfig, CertificationReport
from tc_net_cert.storage.runs import RUN_ID_RE, find_run, list_runs
from tc_net_cert.web.console import DURATION_LABELS, DURATIONS, STAGES, Console, ConsoleBusy
from tc_net_cert.wifi.throughput import PSK_24, PSK_50, SSID_24, SSID_50, store_reading
from tc_net_cert.ethtool.link import explain_link_down
from tc_net_cert.reporting.clock import human_clock, readable_clocks
from tc_net_cert.reporting.pdfdoc import certificate_pdf
from tc_net_cert.reporting.reference import install_builtin_profiles, load_reference, profile_catalog, save_reference
from tc_net_cert.version import __version__
from tc_net_cert.reporting.render import printable_html
from tc_net_cert.web.live import TrafficMonitor, probe_port, wifi_networks

_TEMPLATE = Path(__file__).resolve().parent / "templates"
_LOGO_PATH = Path(__file__).resolve().parents[1] / "reporting" / "templates" / "logo.svg"
_RUN_ID = RUN_ID_RE
_SYSTEM_RUNS = Path("/var/lib/tc-net-cert/runs")


def _certificate_view(html: str, run_id: str) -> str:
    """Show the certificate on a white page. The print dialog is the PDF save."""
    bar = (
        "<style>@media print { .tc-bar { display: none !important; } }"
        ".tc-bar { position: sticky; top: 0; z-index: 20; display: flex; justify-content: space-between;"
        " align-items: center; gap: 12px; margin: 0 0 16px; padding: 10px 16px; background: #0f172a; color: #fff;"
        " font-family: Segoe UI, sans-serif; }"
        ".tc-bar button { margin: 0; border: 0; border-radius: 8px; padding: 8px 14px; background: #059669;"
        " color: #fff; font-weight: 700; cursor: pointer; }</style>"
        f'<div class="tc-bar"><strong>{run_id}</strong>'
        '<button type="button" onclick="window.print()">Guardar PDF</button></div>'
    )
    if "<body>" in html:
        return html.replace("<body>", "<body>" + bar, 1)
    return bar + html


_PANEL_BOOT = ""


def _load_panel_token(state: Path, port: int) -> str:
    """Stable token across panel restarts so open tabs keep working after deploy."""
    state.mkdir(parents=True, exist_ok=True)
    path = state / f"panel-token-{port}.txt"
    try:
        stored = path.read_text(encoding="utf-8").strip()
        if len(stored) >= 16:
            return stored
    except OSError:
        pass
    token = secrets.token_urlsafe(24)
    try:
        path.write_text(token, encoding="utf-8")
        path.chmod(0o600)
    except OSError:
        pass
    return token


def serve(
    config: AppConfig,
    host: str = "127.0.0.1",
    port: int = 8080,
    config_path: Path | None = None,
) -> None:
    global _PANEL_BOOT
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError("Refusing to bind a non-loopback address. The dashboard is local only.")
    from tc_net_cert.storage.bench_identity import clear_all_bench_readings

    clear_all_bench_readings()
    _PANEL_BOOT = secrets.token_urlsafe(12)
    root = _runs_root(config)
    state = Path.home() / ".local" / "state" / "tc-net-cert"
    if os.geteuid() == 0:
        state = Path("/run/tc-net-cert")
    token = _load_panel_token(state, port)
    console = Console(config_path or Path("/etc/tc-net-cert/config.yaml"), state / "panel-job.log")
    monitor = TrafficMonitor()
    try:
        boot_config, _ = load_config(config_path or Path("/etc/tc-net-cert/config.yaml"))
        from tc_net_cert.config.remote_sync import sync_profiles_background
        from tc_net_cert.storage.supabase_runs import config_path as supabase_env_path

        from tc_net_cert.config.release_sync import check_release_background

        sync_profiles_background(boot_config.station, env_path=supabase_env_path())
        check_release_background(supabase_env_path())
    except Exception:
        pass
    handler = _handler(root, console, token, monitor)
    server = ThreadingHTTPServer((host, port), handler)
    print(f"TECHCORP LAB console at http://{host}:{port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


def _portal_targets(ports: dict[str, str], portal_host: str = "") -> list[dict[str, str]]:
    """Test NIC routes to the modem. Optional portal_host is tried first on the same NIC."""
    cables = [item for item in dut_paths(set(ports.values())) if allowed_host(item.get("gateway", ""))][:2]
    if not cables:
        return []
    host = portal_host.strip()
    if not allowed_host(host):
        return cables
    targets: list[dict[str, str]] = []
    seen: set[str] = set()
    for cable in cables:
        lead = dict(cable)
        lead["gateway"] = host
        key = f"{lead['iface']}:{host}"
        if key not in seen:
            targets.append(lead)
            seen.add(key)
    for cable in cables:
        key = f"{cable['iface']}:{cable['gateway']}"
        if key not in seen:
            targets.append(dict(cable))
            seen.add(key)
    return targets


def _dut_kind_from_reading(found: dict) -> str:
    kind = str(found.get("family") or "")
    if kind in {"ont", "emta"}:
        return kind
    model = str(found.get("model") or "").upper()
    brand = str(found.get("brand") or "").upper()
    if model in {"CGNV5", "CGNV5CLR"} or brand in {"HISTRON", "HITRON", "KAON"}:
        return "emta"
    if model.startswith("HG") or brand == "HUAWEI":
        return "ont"
    return ""


def _dut_kind_from_model(model: str) -> str:
    token = str(model or "").strip().upper().replace(" ", "")
    if not token:
        return ""
    if token in {"CGNV5", "CGNV5CLR", "CG-2200", "CG2200", "CG-3000", "CG3000", "KAON"}:
        return "emta"
    if token.startswith("CG") or token.startswith("CGNV"):
        return "emta"
    if token.startswith("HG"):
        return "ont"
    if token.startswith("HS") or "8145" in token or "8245" in token:
        return "ont"
    return ""


def _remember_dut(path: Path, config: AppConfig, kind: str, model: str) -> None:
    """The next run and its certificate follow the device just discovered."""
    if kind not in {"ont", "emta"} or not valid_model(model):
        return
    config.dut.type = kind
    config.dut.model = model
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    updated, count = re.subn(
        r"(dut:\n  type: )\w+(\n  model: ).*",
        rf'\1{kind}\2"{model}"',
        text,
        count=1,
    )
    if count == 1 and updated != text:
        path.write_text(updated, encoding="utf-8")


def _runs_root(config: AppConfig) -> Path:
    if config.runs_dir:
        return Path(config.runs_dir).expanduser()
    if _SYSTEM_RUNS.is_dir() and os.access(_SYSTEM_RUNS, os.R_OK | os.X_OK):
        return _SYSTEM_RUNS
    return runs_directory(config)


def _handler(
    root: Path,
    console: Console,
    token: str,
    monitor: TrafficMonitor,
):
    environment = Environment(
        loader=FileSystemLoader(_TEMPLATE),
        autoescape=True,
        undefined=StrictUndefined,
    )

    class Handler(BaseHTTPRequestHandler):
        def _station_view(self) -> tuple[AppConfig, dict[str, str]]:
            config, _path = load_config(console.config_path)
            ports = {name: port.mac.lower() for name, port in config.ports.items()}
            return config, ports

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path = unquote(parsed.path)
            if path in {"/", "/index.html"}:
                self._dashboard(None)
                return
            if path == "/api/status":
                _config, ports = self._station_view()
                self._json(200, _status_payload(root, console, monitor, ports))
                return
            if path == "/api/system/status":
                if not _local_request(self):
                    self._json(403, {"ok": False, "error": "Solo se aceptan peticiones locales."})
                    return
                self._json(200, _system_status_payload(console))
                return
            if path == "/api/profiles/status":
                if not _local_request(self):
                    self._json(403, {"ok": False, "error": "Solo se aceptan peticiones locales."})
                    return
                config, _ports = self._station_view()
                from tc_net_cert.config.remote_sync import profile_sync_status

                self._json(200, {"ok": True, **profile_sync_status(config.station), "app_version": __version__})
                return
            if path == "/api/log":
                if not _local_request(self):
                    self._json(403, {"ok": False, "error": "Solo se aceptan peticiones locales."})
                    return
                self._job_log()
                return
            if path == "/api/wifi/networks":
                if not _local_request(self):
                    self._json(403, {"ok": False, "error": "Solo se aceptan peticiones locales."})
                    return
                query = parsed.query
                rescan = "rescan=1" in query
                try:
                    networks = wifi_networks(rescan=rescan)
                except (OSError, subprocess.TimeoutExpired):
                    self._json(200, {"ok": False, "networks": [], "error": "No se pudo leer el radio Wi-Fi."})
                    return
                self._json(200, {"ok": True, "networks": networks})
                return
            if path == "/api/runs":
                if not _local_request(self):
                    self._json(403, {"ok": False, "error": "Solo se aceptan peticiones locales."})
                    return
                from tc_net_cert.storage.supabase_runs import config_path, listed_run_ids

                self._json(200, {"ok": True, "runs": _catalog(root, listed_run_ids(config_path()))})
                return
            if path.startswith("/cert/"):
                run_id = path.removeprefix("/cert/").strip("/")
                measurement = _measurement_query(parsed.query)
                self._view(run_id, measurement=measurement)
                return
            if path.startswith("/pdf/"):
                run_id = path.removeprefix("/pdf/").strip("/")
                measurement = _measurement_query(parsed.query)
                self._pdf(run_id, measurement=measurement)
                return
            if path.startswith("/print/"):
                run_id = path.removeprefix("/print/").strip("/")
                measurement = _measurement_query(parsed.query)
                self._print(run_id, measurement=measurement)
                return
            if path.startswith("/run/"):
                self._dashboard(path.removeprefix("/run/").strip("/"))
                return
            if path.startswith("/runs/"):
                self._file(path.removeprefix("/runs/"))
                return
            if path == "/api/reference/export":
                if not _local_request(self):
                    self._json(403, {"ok": False, "error": "Solo se aceptan peticiones locales."})
                    return
                query = parse_qs(parsed.query)
                name = str((query.get("name") or query.get("profile") or [""])[0]).strip()
                if not name:
                    self._json(400, {"ok": False, "error": "Indica el nombre del perfil."})
                    return
                from tc_net_cert.reporting.reference_spreadsheet import export_reference_csv

                try:
                    payload, filename = export_reference_csv(name)
                except ValueError as exc:
                    self._json(404, {"ok": False, "error": str(exc)})
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/csv; charset=utf-8")
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            if path == "/api/portals/export":
                if not _local_request(self):
                    self._json(403, {"ok": False, "error": "Solo se aceptan peticiones locales."})
                    return
                payload, filename = export_portals_csv(portals_path())
                self.send_response(200)
                self.send_header("Content-Type", "text/csv; charset=utf-8")
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            if path == "/api/portals":
                if not _local_request(self):
                    self._json(403, {"ok": False, "error": "Solo se aceptan peticiones locales."})
                    return
                self._json(200, {"ok": True, "rows": list_portal_rows(portals_path())})
                return
            self.send_error(404)

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path = unquote(parsed.path)
            if path not in {
                "/api/run",
                "/api/stop",
                "/api/scrape",
                "/api/discover",
                "/api/benches/discover",
                "/api/bench/reset",
                "/api/recover",
                "/api/ports/remap",
                "/api/wifi",
                "/api/link",
                "/api/portal",
                "/api/portals/import",
                "/api/reference",
                "/api/reference/import",
                "/api/sync",
                "/api/profiles/sync",
                "/api/system/update",
                "/api/system/update/check",
            }:
                self.send_error(404)
                return
            if not _local_request(self):
                self._json(403, {"ok": False, "error": "Solo se aceptan peticiones locales."})
                return
            length = int(self.headers.get("Content-Length") or "0")
            max_body = 524288 if path in {"/api/reference/import", "/api/portals/import"} else 16384
            if length < 0 or length > max_body:
                self.send_error(413)
                return
            try:
                body = json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError):
                self._json(400, {"ok": False, "error": "Petición inválida."})
                return
            if not isinstance(body, dict) or body.get("token") != token:
                self._json(403, {"ok": False, "error": "La página expiró. Recarga el panel."})
                return
            config, ports = self._station_view()
            if path == "/api/link":
                logical = str(body.get("port") or "")
                try:
                    reading = probe_port(ports, logical)
                except ValueError as exc:
                    self._json(400, {"ok": False, "error": str(exc)})
                    return
                self._json(200, {"ok": True, "link": reading, **_status_payload(root, console, monitor, ports)})
                return
            if path == "/api/wifi":
                action = str(body.get("action") or "")
                if action not in {"on", "off"}:
                    self._json(400, {"ok": False, "error": "Acción no permitida."})
                    return
                try:
                    state = console.set_wifi(action)
                except (ConsoleBusy, ValueError) as exc:
                    self._json(409, {"ok": False, "error": str(exc), **_status_payload(root, console, monitor, ports)})
                    return
                self._json(200, {"ok": True, "wifi_state": state, **_status_payload(root, console, monitor, ports)})
                return
            if path == "/api/stop":
                try:
                    console.stop()
                except ConsoleBusy as exc:
                    self._json(409, {"ok": False, "error": str(exc), **_status_payload(root, console, monitor, ports)})
                    return
                self._json(200, {"ok": True, **_status_payload(root, console, monitor, ports)})
                return
            if path == "/api/benches/discover":
                if console.status().get("running"):
                    self._json(409, {"ok": False, "error": "Hay una prueba en curso."})
                    return
                import time

                from tc_net_cert.web.discover_benches import discover_benches

                scope = str(body.get("scope") or "all")
                dut_user = str(body.get("dut_user") or "").strip()
                dut_password = body.get("dut_password") if isinstance(body.get("dut_password"), str) else ""
                portal_host = str(body.get("portal_host") or "").strip()
                form_model = str(body.get("portal_model") or config.dut.model or "").strip()
                bench_credentials = body.get("bench_credentials")
                if not isinstance(bench_credentials, dict):
                    bench_credentials = None
                payload = discover_benches(
                    console.config_path,
                    scope=scope,
                    dut_user=dut_user,
                    dut_password=dut_password,
                    portal_host=portal_host,
                    form_model=form_model,
                    bench_credentials=bench_credentials,
                )
                if not payload.get("ok"):
                    time.sleep(0.5)
                self._json(200 if payload.get("ok") else 409, {**payload, **_status_payload(root, console, monitor, ports)})
                return
            if path == "/api/benches/clear-reading":
                from tc_net_cert.storage.bench_identity import clear_all_bench_readings, clear_bench_reading

                bench = str(body.get("bench_id") or body.get("bench") or "").strip().upper()
                if bench in {"A", "B"}:
                    clear_bench_reading(bench)
                else:
                    clear_all_bench_readings()
                self._json(200, {"ok": True, "bench_id": bench, **_status_payload(root, console, monitor, ports)})
                return
            if path == "/api/recover":
                if console.status().get("running"):
                    self._json(409, {"ok": False, "error": "Hay una prueba en curso."})
                    return
                from tc_net_cert.core.executor import SystemExecutor
                from tc_net_cert.orchestration.recover import recover

                lines = recover(SystemExecutor(), force_prefixes=True)
                self._json(
                    200,
                    {
                        "ok": True,
                        "messages": lines,
                        **_status_payload(root, console, monitor, ports),
                    },
                )
                return
            if path == "/api/bench/reset":
                if console.status().get("running"):
                    self._json(409, {"ok": False, "error": "Hay una prueba en curso."})
                    return
                bench = str(body.get("bench_id") or body.get("bench") or "").strip().upper()
                if bench not in {"A", "B"}:
                    self._json(400, {"ok": False, "error": "Indica banco A o B."})
                    return
                from tc_net_cert.storage.bench_identity import clear_bench_reading
                from tc_net_cert.web.privilege import run_helper

                try:
                    result = run_helper("bench-reset", bench, str(console.config_path))
                except ConsoleBusy as exc:
                    self._json(409, {"ok": False, "error": str(exc)})
                    return
                lines = (result.stdout or result.stderr or "").strip().splitlines()
                ok = result.returncode == 0
                if ok:
                    clear_bench_reading(bench)
                self._json(
                    200 if ok else 409,
                    {
                        "ok": ok,
                        "bench_id": bench,
                        "messages": lines,
                        "error": "" if ok else (lines[-1] if lines else "No se pudo reiniciar el banco."),
                        **_status_payload(root, console, monitor, ports),
                    },
                )
                return
            if path == "/api/ports/remap":
                if console.status().get("running"):
                    self._json(409, {"ok": False, "error": "Hay una prueba en curso."})
                    return
                from tc_net_cert.web.privilege import run_helper

                bench = str(body.get("bench_id") or body.get("bench") or "").strip().upper()
                try:
                    if bench in {"A", "B"}:
                        result = run_helper("ports-remap-bench", bench, str(console.config_path))
                    else:
                        result = run_helper("ports-remap", str(console.config_path))
                except ConsoleBusy as exc:
                    self._json(409, {"ok": False, "error": str(exc)})
                    return
                lines = (result.stdout or result.stderr or "").strip().splitlines()
                ok = result.returncode == 0
                self._json(
                    200 if ok else 409,
                    {
                        "ok": ok,
                        "messages": lines,
                        "error": "" if ok else (lines[-1] if lines else "No se pudo remapear LAN."),
                        **_status_payload(root, console, monitor, ports),
                    },
                )
                return
            if path == "/api/discover":
                if console.status().get("running"):
                    self._json(409, {"ok": False, "error": "Hay una prueba en curso. Espera a que termine para descubrir el equipo."})
                    return
                import time

                refresh_portal_links(SystemExecutor(), dict(ports))
                time.sleep(1.5)
                portal_host = str(body.get("portal_host") or "").strip()
                cables = _portal_targets(ports, portal_host)
                if not cables:
                    self._json(409, {"ok": False, "error": "Ningún puerto de prueba tiene puerta de enlace DHCP. Conecta el módem por Ethernet (LAN con link). El Wi-Fi no se usa."})
                    return
                form_model = str(body.get("portal_model") or config.dut.model or "").strip()
                dut_user = str(body.get("dut_user") or "").strip()
                dut_password = body.get("dut_password") if isinstance(body.get("dut_password"), str) else ""
                portals = load_portals(portals_path())
                guess_model = form_model or config.dut.model or ""
                found: dict = {"ok": False, "error": "El portal no respondió por el puerto de prueba. No se intentó el ingreso."}
                try:
                    if valid_user(dut_user) and valid_password(dut_password):
                        for cable in cables:
                            found = read_portal(
                                cable["gateway"],
                                dut_user,
                                dut_password,
                                portals,
                                source=cable["source"],
                                device=cable["iface"],
                                form_model=guess_model,
                                inventory=True,
                            )
                            if found.get("ok") or not portal_retryable(str(found.get("error") or "")):
                                break
                    else:
                        for cable in cables:
                            found = discover_portal(
                                cable["gateway"],
                                portals,
                                source=cable["source"],
                                device=cable["iface"],
                            )
                            if not portal_retryable(str(found.get("error") or "")):
                                break
                        if not found.get("ok") and "No hay un perfil guardado" in str(found.get("error") or ""):
                            found["error"] = (
                                str(found.get("error") or "")
                                + " Escribe usuario y contraseña del equipo y vuelve a pulsar Buscar."
                            )
                except Exception:
                    self._json(409, {"ok": False, "error": "No se pudo consultar el equipo."})
                    return
                if dut_password and dut_password in str(found.get("error") or ""):
                    found["error"] = "No se pudo leer el portal. Revisa usuario y contraseña."
                kind = _dut_kind_from_reading(found)
                if found.get("ok") and kind in {"ont", "emta"} and valid_model(str(found.get("model") or "")):
                    _remember_dut(console.config_path, config, kind, str(found["model"]))
                    found["family"] = kind
                if found.get("ok"):
                    from tc_net_cert.storage.dut_identity import store_identity

                    store_reading(found)
                    store_identity(
                        serial=str(found.get("serial") or ""),
                        mac=str(found.get("mac") or ""),
                        brand=str(found.get("brand") or ""),
                        model=str(found.get("model") or ""),
                    )
                self._json(200 if found.get("ok") else 409, found)
                return
            if path == "/api/scrape":
                if console.status().get("running"):
                    self._json(409, {"ok": False, "error": "Hay una prueba en curso. Espera a que termine para leer el portal."})
                    return
                import time

                refresh_portal_links(SystemExecutor(), dict(ports))
                time.sleep(1.5)
                model = str(body.get("portal_model") or config.dut.model or "").strip()
                lookup = "Kaon" if model in {"CG-2200", "CG-3000", "CG3000"} else model
                saved = profile_for(lookup) if valid_model(lookup) else None
                dut_user = str(body.get("dut_user") or (saved or {}).get("username") or "").strip()
                dut_password = body.get("dut_password") if isinstance(body.get("dut_password"), str) else ""
                if not dut_password and saved:
                    dut_password = saved["password"]
                portal_host = str(body.get("portal_host") or "").strip()
                cables = _portal_targets(ports, portal_host or host)
                if not cables:
                    self._json(409, {"ok": False, "error": "Ningún puerto de prueba tiene puerta de enlace DHCP. El Wi-Fi no se usa."})
                    return
                if not valid_user(dut_user) or not valid_password(dut_password):
                    self._json(400, {"ok": False, "error": "Revisa el usuario y la contraseña del portal."})
                    return
                found = {"ok": False, "error": "El portal no respondió por el puerto de prueba. No se intentó el ingreso."}
                try:
                    for cable in cables:
                        found = read_portal(
                            cable["gateway"],
                            dut_user,
                            dut_password,
                            load_portals(portals_path()),
                            source=cable["source"],
                            device=cable["iface"],
                            form_model=model,
                            inventory=True,
                        )
                        if not portal_retryable(str(found.get("error") or "")):
                            break
                except Exception:
                    self._json(409, {"ok": False, "error": "No se pudo consultar el ONT."})
                    return
                if dut_password and dut_password in str(found.get("error") or ""):
                    found["error"] = "No se pudo leer el portal."
                kind = _dut_kind_from_reading(found)
                if found.get("ok") and kind in {"ont", "emta"} and valid_model(str(found.get("model") or "")):
                    _remember_dut(console.config_path, config, kind, str(found["model"]))
                    found["family"] = kind
                if found.get("ok"):
                    from tc_net_cert.storage.dut_identity import store_identity
                    from tc_net_cert.storage.supabase_runs import config_path, save_run

                    store_reading(found)
                    store_identity(
                        serial=str(found.get("serial") or ""),
                        mac=str(found.get("mac") or ""),
                        brand=str(found.get("brand") or ""),
                        model=str(found.get("model") or ""),
                    )
                    found["store_error"] = save_run(config_path(), _reading_record(found, config))
                self._json(200 if found.get("ok") else 409, found)
                return
            if path == "/api/portal":
                action = str(body.get("action") or "save").strip().lower()
                store = portals_path()
                if action == "delete":
                    model = str(body.get("model") or "").strip()
                    if not delete_portal(store, model):
                        self._json(404, {"ok": False, "error": "No existe ese perfil."})
                        return
                    self._json(
                        200,
                        {
                            "ok": True,
                            "model": model,
                            "rows": list_portal_rows(store),
                            "portals": [
                                {
                                    "model": row["model"],
                                    "host": row["host"],
                                    "username": row["username"],
                                    "password": row["password"],
                                    "brand": row["brand"],
                                }
                                for row in list_portal_rows(store)
                            ],
                        },
                    )
                    return
                model = str(body.get("model") or body.get("portal_model") or "").strip()
                host = str(body.get("host") or body.get("portal_host") or "").strip()
                username = str(body.get("username") or body.get("dut_user") or "").strip()
                password = body.get("password") if isinstance(body.get("password"), str) else ""
                if not password and isinstance(body.get("dut_password"), str):
                    password = body.get("dut_password")
                kind = str(body.get("kind") or "").strip()
                try:
                    save_portal(store, model, host, username, password, kind=kind)
                except ValueError as exc:
                    self._json(400, {"ok": False, "error": str(exc)})
                    return
                rows = list_portal_rows(store)
                listed = [
                    {
                        "model": row["model"],
                        "host": row["host"],
                        "username": row["username"],
                        "password": row["password"],
                        "brand": row["brand"],
                    }
                    for row in rows
                ]
                self._json(200, {"ok": True, "model": model, "path": str(store), "portals": listed, "rows": rows})
                return
            if path == "/api/portals/import":
                raw = body.get("csv")
                if not isinstance(raw, str) or not raw.strip():
                    self._json(400, {"ok": False, "error": "Falta el contenido del archivo (campo csv)."})
                    return
                store = portals_path()
                try:
                    count, notes = import_portals_csv(raw, store)
                except ValueError as exc:
                    self._json(400, {"ok": False, "error": str(exc)})
                    return
                self._json(
                    200,
                    {
                        "ok": True,
                        "imported": count,
                        "notes": notes,
                        "rows": list_portal_rows(store),
                        "portals": [
                            {
                                "model": row["model"],
                                "host": row["host"],
                                "username": row["username"],
                                "password": row["password"],
                                "brand": row["brand"],
                            }
                            for row in list_portal_rows(store)
                        ],
                    },
                )
                return
            if path == "/api/reference":
                name = str(body.get("name") or "").strip()
                try:
                    saved = save_reference(name, body.get("values") if isinstance(body.get("values"), dict) else {})
                except (TypeError, ValueError) as exc:
                    self._json(400, {"ok": False, "error": str(exc)})
                    return
                self._json(200, {"ok": True, "name": saved["name"]})
                return
            if path == "/api/reference/import":
                name = str(body.get("name") or "").strip()
                raw = body.get("csv")
                if not isinstance(raw, str) or not raw.strip():
                    self._json(400, {"ok": False, "error": "Falta el contenido del archivo (campo csv)."})
                    return
                from tc_net_cert.reporting.reference_spreadsheet import import_reference_csv

                try:
                    saved = import_reference_csv(raw, expected_name=name)
                except (TypeError, ValueError) as exc:
                    self._json(400, {"ok": False, "error": str(exc)})
                    return
                self._json(200, {"ok": True, "name": saved["name"]})
                return
            if path == "/api/sync":
                self._json(200, _sync_unsent(root, config))
                return
            if path == "/api/profiles/sync":
                from tc_net_cert.config.remote_sync import sync_profiles
                from tc_net_cert.storage.supabase_runs import config_path as supabase_env_path

                country = str(body.get("country_code") or "").strip().upper()
                result = sync_profiles(country_code=country, station=config.station, env_path=supabase_env_path())
                self._json(200 if result.get("ok") else 409, {"ok": bool(result.get("ok")), **result})
                return
            if path == "/api/system/update/check":
                from tc_net_cert.config.release_sync import sync_release_offer
                from tc_net_cert.storage.supabase_runs import config_path as supabase_env_path

                channel = str(body.get("channel") or "lab").strip() or "lab"
                result = sync_release_offer(env_path=supabase_env_path(), channel=channel)
                self._json(200 if result.get("ok") else 409, result)
                return
            if path == "/api/system/update":
                if console.status().get("running"):
                    self._json(409, {"ok": False, "error": "Hay una certificación en curso. Aplique la actualización cuando el panel esté libre."})
                    return
                url = str(body.get("url") or "").strip()
                if not url:
                    from tc_net_cert.config.release_sync import resolve_update_url

                    url = resolve_update_url() or str(_update_offer().get("url") or "").strip()
                if not url:
                    self._json(400, {"ok": False, "error": "No hay paquete de actualización configurado."})
                    return
                script = Path("/opt/tc-net-cert/scripts/apply-update.sh")
                if not script.is_file():
                    self._json(500, {"ok": False, "error": "Falta /opt/tc-net-cert/scripts/apply-update.sh en esta estación."})
                    return
                try:
                    proc = subprocess.Popen(
                        [str(script), url],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                    )
                except OSError as exc:
                    self._json(500, {"ok": False, "error": f"No se pudo lanzar la actualización: {exc}"})
                    return
                self._json(
                    202,
                    {
                        "ok": True,
                        "message": "Actualización en curso. El panel se reiniciará al terminar.",
                        "pid": proc.pid,
                        **_status_payload(root, console, monitor, ports),
                    },
                )
                return
            stage = str(body.get("stage") or "")
            raw_duration = body.get("duration")
            duration = None
            if isinstance(raw_duration, bool):
                duration = None
            elif isinstance(raw_duration, (int, float)):
                duration = int(raw_duration)
            elif isinstance(raw_duration, str) and raw_duration.strip().isdigit():
                duration = int(raw_duration.strip())
            model = str(body.get("portal_model") or config.dut.model or "").strip()
            saved = profile_for(model) if valid_model(model) else None
            host = str(body.get("portal_host") or (saved or {}).get("host") or "").strip()
            dut_user = str(body.get("dut_user") or (saved or {}).get("username") or "").strip()
            dut_password = body.get("dut_password") if isinstance(body.get("dut_password"), str) else ""
            if not dut_password and saved:
                dut_password = saved["password"]
            portal_module = str(body.get("portal_module") or "").strip()
            model_kind = _dut_kind_from_model(model)
            if portal_module in {"ont", "emta"} and model_kind and portal_module != model_kind:
                expect = "EMTA DOCSIS" if model_kind == "emta" else "ONT GPON"
                self._json(
                    409,
                    {
                        "ok": False,
                        "error": f"El modelo {model} es {expect}. Abre el módulo "
                        f"{'EMTA' if model_kind == 'emta' else 'ONT'} en el menú o corrige el modelo en la tarjeta.",
                    },
                )
                return
            if not allowed_host(host) or not valid_user(dut_user) or not valid_password(dut_password):
                self._json(400, {"ok": False, "error": "Revisa el portal del módem: dirección, usuario y contraseña."})
                return
            bench_id = str(body.get("bench_id") or body.get("bench") or "").strip().upper()
            bench_credentials = body.get("bench_credentials")
            if not isinstance(bench_credentials, dict):
                bench_credentials = None
            try:
                console.start(
                    stage,
                    duration,
                    dut_user=dut_user,
                    dut_password=dut_password,
                    dut_host=host,
                    wifi=_wifi_env(body),
                    identity=_identity_env(body),
                    bench_id=bench_id,
                    bench_credentials=bench_credentials,
                )
            except (ConsoleBusy, ValueError) as exc:
                self._json(409, {"ok": False, "error": str(exc), **_status_payload(root, console, monitor, ports)})
                return
            self._json(202, {"ok": True, **_status_payload(root, console, monitor, ports)})

        def log_message(self, fmt: str, *args) -> None:
            return

        def _dashboard(self, run_id: str | None) -> None:
            from tc_net_cert.storage.supabase_runs import config_path, listed_run_ids

            config, ports = self._station_view()
            runs = _catalog(root, listed_run_ids(config_path()))
            selected_id = run_id or _latest_finished(runs)
            report = _read_report(root, selected_id) if selected_id else None
            portals = ensure_portals()
            from tc_net_cert.config.remote_sync import merge_portals_with_remote, profile_sync_status, resolve_country_code

            country = resolve_country_code(config.station)
            portals = merge_portals_with_remote(portals, country_code=country, station=config.station)
            profiles_meta = profile_sync_status(config.station)
            measured = str(((report or {}).get("dut") or {}).get("model") or "")
            hitron_model = config.dut.model in {"CGNV5", "CGNV5CLR"}
            kaon_model = config.dut.model in {"", "Kaon", "KAON", "CG-2200", "CG-3000", "CG3000"}
            if config.dut.model in portals:
                active_name = config.dut.model
            elif config.dut.type == "emta" and hitron_model and "CGNV5" in portals:
                active_name = "CGNV5"
            elif config.dut.type == "emta" and kaon_model and "CG-2200" in portals:
                active_name = "CG-2200"
            elif config.dut.type == "emta" and kaon_model and "Kaon" in portals:
                active_name = "Kaon"
            elif measured in portals:
                active_name = measured
            else:
                active_name = next(iter(portals), "")
            shown_model = config.dut.model if config.dut.model not in {"", "Kaon", "KAON"} else active_name
            stale_hitron = (
                config.dut.type == "emta"
                and config.dut.model in {"CGNV5", "CGNV5CLR"}
                and not str(config.dut.serial or "").strip()
                and "CGNV5" not in portals
            )
            if stale_hitron:
                shown_model = "CG-2200"
            elif config.dut.type == "emta" and shown_model in {"", "Kaon", "KAON", "CGNV5"} and not hitron_model:
                shown_model = "CG-2200"
            display_dut = config.dut.model or "—"
            if stale_hitron:
                display_dut = "CG-2200"
            active = portals.get(active_name) or {"host": "192.168.1.1", "username": "", "password": ""}
            body = environment.get_template("dashboard.html").render(
                token=token,
                logo=_LOGO_PATH.read_text(encoding="utf-8"),
                station=config.station or "—",
                operator=config.operator or "—",
                network_mode=config.network_mode.upper(),
                profile=config.active_profile,
                dut_type=config.dut.type,
                dut_model=display_dut,
                portal_model=shown_model,
                portal_host=active["host"],
                portal_user=active["username"],
                portal_password=active["password"],
                portals=[{"model": name, **item} for name, item in portals.items()],
                portal_rows=list_portal_rows(),
                port_count=len(config.ports),
                stages=[{"id": stage, "label": _STAGE_LABELS[stage]} for stage in _STAGE_ORDER],
                durations=[{"seconds": seconds, "label": DURATION_LABELS[seconds]} for seconds in DURATIONS],
                runs=runs,
                cert=_certificate(report) if report else None,
                job=_status_payload(root, console, monitor, ports),
                reference=_active_reference(config),
                app_version=__version__,
                profiles_meta=profiles_meta,
                reference_catalog=profile_catalog(),
            )
            data = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _print(self, run_id: str, *, measurement: str = "") -> None:
            folder = find_run(root, run_id) if _RUN_ID.fullmatch(run_id) else None
            if folder is None:
                self.send_error(404)
                return
            html = _printable_for_folder(folder, measurement, order_id=run_id)
            if html is None:
                self.send_error(404)
                return
            script = "<script>window.addEventListener('load',function(){window.print();});</script>"
            if "</body>" in html:
                html = html.replace("</body>", script + "</body>", 1)
            else:
                html += script
            data = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _view(self, run_id: str, *, measurement: str = "") -> None:
            folder = find_run(root, run_id) if _RUN_ID.fullmatch(run_id) else None
            if folder is None:
                self.send_error(404)
                return
            html = _printable_for_folder(folder, measurement, order_id=run_id)
            if html is None:
                self.send_error(404)
                return
            html = _certificate_view(html, run_id)
            data = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _pdf(self, run_id: str, *, measurement: str = "") -> None:
            folder = find_run(root, run_id) if _RUN_ID.fullmatch(run_id) else None
            payload = _report_json_path(folder, measurement) if folder is not None else None
            if payload is None or not payload.is_file():
                self.send_error(404)
                return
            try:
                from tc_net_cert.reporting.bench_slice import report_for_order_view

                report = CertificationReport.model_validate_json(payload.read_text(encoding="utf-8"))
                report = report_for_order_view(report, run_id)
                data = certificate_pdf(report)
            except Exception:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Disposition", f'inline; filename="{run_id}.pdf"')
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _file(self, relative: str) -> None:
            folder_name, _, rest = relative.partition("/")
            folder = find_run(root, folder_name)
            if folder is None or not rest:
                self.send_error(404)
                return
            target = (folder / rest).resolve()
            if folder.resolve() not in target.parents and target != folder.resolve():
                self.send_error(403)
                return
            if not target.is_file():
                self.send_error(404)
                return
            data = target.read_bytes()
            content_type = "application/octet-stream"
            if target.suffix == ".html":
                content_type = "text/html; charset=utf-8"
            elif target.suffix == ".json":
                content_type = "application/json"
            elif target.suffix == ".csv":
                content_type = "text/csv; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _job_log(self) -> None:
            try:
                text = console.log_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                text = ""
            if len(text) > 1_000_000:
                text = text[-1_000_000:]
            data = text.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Disposition", 'attachment; filename="techcorp-lab-log.txt"')
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _json(self, status: int, payload: dict) -> None:
            data = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    return Handler


_STAGE_LABELS = {
    "simultaneous": "Validar LAN1–LAN4: 2 transmiten y 2 clientes, luego cruzado",
    "wifi": "Wi-Fi → LAN2 o LAN3: throughput 5 GHz y después 2.4 GHz",
    "all": "Certificación completa",
    "throughput": "Matriz secuencial, un flujo",
    "dhcp": "DHCP",
    "physical": "Enlace físico",
    "cross": "Cruce de ping",
    "bidirectional": "Bidireccional en la misma ventana",
}
_STAGE_ORDER = (
    "simultaneous",
    "wifi",
    "all",
    "throughput",
    "bidirectional",
    "cross",
    "physical",
    "dhcp",
)


def _identity_env(body: dict) -> dict[str, str]:
    pairs = (
        ("dut_serial", SERIAL_ENV),
        ("dut_mac", MAC_ENV),
        ("dut_brand", BRAND_ENV),
        ("dut_model", MODEL_ENV),
    )
    found = {}
    for incoming, outgoing in pairs:
        value = body.get(incoming)
        if not isinstance(value, str):
            continue
        text = value.strip()
        if not text or len(text) > 40 or any(char in text for char in "\n\x00"):
            continue
        found[outgoing] = text
    return found


def _wifi_env(body: dict) -> dict[str, str]:
    pairs = (
        ("wifi_ssid_24", SSID_24),
        ("wifi_password_24", PSK_24),
        ("wifi_ssid_50", SSID_50),
        ("wifi_password_50", PSK_50),
    )
    found = {}
    for incoming, outgoing in pairs:
        value = body.get(incoming)
        if not isinstance(value, str):
            continue
        text = value.strip()
        if not text or "\n" in text or "\x00" in text or len(text) > 63:
            continue
        found[outgoing] = text
    return found


def _local_request(handler: BaseHTTPRequestHandler) -> bool:
    host = (handler.headers.get("Host") or "").split(":")[0]
    if host not in {"127.0.0.1", "localhost", "::1"}:
        return False
    origin = handler.headers.get("Origin")
    if not origin:
        return True
    return origin.startswith(("http://127.0.0.1:", "http://localhost:", "http://[::1]:"))


def _reading_record(found: dict, config: AppConfig) -> dict:
    keys = (
        "model", "device_type", "serial", "mac", "software", "hardware", "tx_power", "rx_power",
        "voltage", "bias", "temperature", "wlan_status", "wlan_channel", "ssid_24", "ssid_50",
        "wifi_password_24", "wifi_password_50",
    )
    record = {key: str(found.get(key) or "") for key in keys}
    record.update(
        {
            "run_id": "",
            "station": config.station,
            "operator": config.operator,
            "verdict": "LECTURA",
            "software_version": record.pop("software"),
            "hardware_version": record.pop("hardware"),
            "tx_optical_power": record.pop("tx_power"),
            "rx_optical_power": record.pop("rx_power"),
            "working_voltage": record.pop("voltage"),
            "bias_current": record.pop("bias"),
            "working_temperature": record.pop("temperature"),
            "throughput": [],
        }
    )
    return record


def _sync_unsent(root: Path, config: AppConfig) -> dict:
    from tc_net_cert.storage.supabase_runs import config_path, sync_unsent_runs

    return sync_unsent_runs(
        root,
        env_file=config_path(),
        station=config.station,
        operator=config.operator,
    )


def _active_reference(config: AppConfig) -> dict:
    install_builtin_profiles()
    model = config.dut.model or ""
    if model in {"Kaon", "KAON"}:
        model = "CG-2200"
    return load_reference(model=model, kind=config.dut.type, station=config.station)


def _update_offer() -> dict:
    path = Path("/var/lib/tc-net-cert/update_offer.json")
    if not path.is_file():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _system_status_payload(console: Console) -> dict:
    from tc_net_cert.config.remote_sync import profile_sync_status
    from tc_net_cert.config.release_sync import effective_update_status

    config, _ = load_config(console.config_path)
    profiles = profile_sync_status(config.station)
    updates = effective_update_status()
    return {
        "ok": True,
        "app_version": __version__,
        "profiles": profiles,
        **updates,
    }


def _bench_scope_from_report(report: dict) -> str:
    raw = str(report.get("bench_scope") or "").strip().upper()
    if raw in {"A", "B", "A+B"}:
        return raw
    bench_id = str(report.get("bench_id") or "").strip().upper()
    if bench_id in {"A", "B"}:
        return bench_id
    ports = report.get("ports")
    if not isinstance(ports, list):
        return ""
    names = {
        str(item.get("logical_name") or "").upper()
        for item in ports
        if isinstance(item, dict)
    }
    bank_a = names & {"LAN1", "LAN2", "LAN3", "LAN4"}
    bank_b = names & {"LAN5", "LAN6", "LAN7", "LAN8"}
    if bank_a and bank_b:
        return "A+B"
    if bank_b and not bank_a:
        return "B"
    if bank_a and not bank_b:
        return "A"
    return ""


def _bench_label(scope: str) -> str:
    return {
        "A": "Banco A",
        "B": "Banco B",
    }.get(scope, "")


def _catalog_verdict_for_bench(report: dict, bench_id: str) -> str:
    explicit = str(report.get("bench_scope") or "").strip().upper()
    inferred = _bench_scope_from_report(report)
    if explicit == bench_id and inferred != "A+B":
        return str(report.get("global_verdict") or "EN CURSO")
    if inferred == "A+B" or (
        isinstance(report.get("bench_orders"), dict) and report["bench_orders"].get("A") and report["bench_orders"].get("B")
    ):
        try:
            from tc_net_cert.core.models import CertificationReport
            from tc_net_cert.reporting.bench_slice import narrow_report_to_bench

            scoped = narrow_report_to_bench(CertificationReport.model_validate(report), bench_id)
            return scoped.global_verdict.value
        except Exception:
            pass
    return str(report.get("global_verdict") or "EN CURSO")


def _catalog(root: Path, stored: set[str] | None = None) -> list[dict]:
    from tc_net_cert.storage.runs import find_run, list_archived_reports

    rows: list[dict] = []
    for path in reversed(list_runs(root)):
        folder = find_run(root, path.name) or path
        batch: list[dict] = []
        for archived in reversed(list_archived_reports(folder)):
            try:
                report = json.loads(archived.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(report, dict):
                batch.extend(
                    _catalog_entries(
                        report,
                        path.name,
                        stored,
                        root,
                        measurement=archived.name,
                        archived=True,
                    )
                )
        report = _read_report(root, path.name)
        if report is not None:
            batch.extend(_catalog_entries(report, path.name, stored, root, measurement="", archived=False))
        elif not batch:
            batch.append(
                {
                    "run_id": path.name,
                    "verdict": "EN CURSO",
                    "brand": "",
                    "model": "",
                    "technology": "",
                    "serial": "",
                    "mac": "",
                    "search": "",
                    "job": "",
                    "started_at": "",
                    "has_report": False,
                    "stored": None if stored is None else path.name in stored,
                    "archived": False,
                    "measurement": "",
                    "sort_key": "",
                    "bench_scope": "",
                    "bench_label": "—",
                }
            )
        batch.sort(key=lambda row: str(row.get("sort_key") or ""), reverse=True)
        rows.extend(batch)
    return _catalog_dedupe(rows)


def _catalog_dedupe(rows: list[dict]) -> list[dict]:
    seen: set[tuple[str, str, str, str]] = set()
    kept: list[dict] = []
    for row in rows:
        key = (
            str(row.get("sort_key") or ""),
            str(row.get("run_id") or ""),
            str(row.get("bench_scope") or ""),
            str(row.get("measurement") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        kept.append(row)
    return kept


def _report_snapshot(root: Path, run_id: str, sort_key: str) -> dict | None:
    from tc_net_cert.storage.runs import find_run, list_archived_reports

    folder = find_run(root, run_id)
    if folder is None:
        return None
    for path in reversed(list_archived_reports(folder) + [folder / "report.json"]):
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if sort_key and str(payload.get("finished_at_utc") or "") != sort_key:
            continue
        if isinstance(payload, dict):
            return payload
    return None


def _dut_from_order_folder(
    root: Path,
    run_id: str,
    bench_id: str,
    sort_key: str,
) -> dict | None:
    from tc_net_cert.storage.runs import find_run, list_archived_reports

    folder = find_run(root, run_id)
    if folder is None:
        return None
    paths = list_archived_reports(folder) + [folder / "report.json"]
    for path in reversed(paths):
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if sort_key and str(payload.get("finished_at_utc") or "") != sort_key:
            continue
        bench_duts = payload.get("bench_duts") if isinstance(payload.get("bench_duts"), dict) else {}
        dut = bench_duts.get(bench_id) if isinstance(bench_duts.get(bench_id), dict) else None
        if dut is None and str(payload.get("bench_scope") or "").upper() == bench_id:
            dut = payload.get("dut") if isinstance(payload.get("dut"), dict) else None
        if isinstance(dut, dict) and str(dut.get("serial") or "").strip():
            return dut
    return None


def _catalog_entries(
    report: dict,
    folder_run_id: str,
    stored: set[str] | None,
    root: Path,
    *,
    measurement: str,
    archived: bool,
) -> list[dict]:
    dual_host = str(report.get("dual_session_run") or "").strip()
    scope = str(report.get("bench_scope") or "").strip().upper() or _bench_scope_from_report(report)
    bench_duts = report.get("bench_duts") if isinstance(report.get("bench_duts"), dict) else {}
    bench_orders = report.get("bench_orders") if isinstance(report.get("bench_orders"), dict) else {}

    if dual_host and dual_host != folder_run_id and scope in {"A", "B"}:
        dut = bench_duts.get(scope) if isinstance(bench_duts.get(scope), dict) else report.get("dut")
        if not isinstance(dut, dict):
            dut = report.get("dut") if isinstance(report.get("dut"), dict) else {}
        order_id = str(bench_orders.get(scope) or folder_run_id)
        return [
            _catalog_row(
                report,
                order_id,
                stored,
                measurement=measurement,
                archived=archived,
                dut=dut if isinstance(dut, dict) else None,
                bench_scope=scope,
                verdict=_catalog_verdict_for_bench(report, scope),
            )
        ]

    split: list[dict] = []
    dual_orders = bench_orders.get("A") and bench_orders.get("B")
    inferred_dual = _bench_scope_from_report(report) == "A+B"
    if dual_orders or inferred_dual:
        sort_key = str(report.get("finished_at_utc") or report.get("started_at_utc") or "")
        for bench_id in ("A", "B"):
            dut = bench_duts.get(bench_id) if isinstance(bench_duts.get(bench_id), dict) else None
            if dut is None and bench_id == "A" and isinstance(report.get("dut"), dict):
                dut = report["dut"]
            if dut is None and isinstance(bench_orders, dict) and bench_orders.get(bench_id):
                dut = _dut_from_order_folder(root, str(bench_orders[bench_id]), bench_id, sort_key)
            if not isinstance(dut, dict):
                continue
            serial = str(dut.get("serial") or "").strip()
            if not serial:
                continue
            order_id = str(bench_orders.get(bench_id) or folder_run_id)
            verdict = _catalog_verdict_for_bench(report, bench_id)
            mirror_order = str(bench_orders.get(bench_id) or "")
            if mirror_order and mirror_order != folder_run_id:
                mirror = _report_snapshot(root, mirror_order, sort_key)
                if mirror is not None:
                    verdict = str(mirror.get("global_verdict") or verdict)
            split.append(
                _catalog_row(
                    report,
                    order_id,
                    stored,
                    measurement=measurement,
                    archived=archived,
                    dut=dut,
                    bench_scope=bench_id,
                    verdict=verdict,
                )
            )
        if split:
            return split

    if scope in {"A", "B"}:
        dut = bench_duts.get(scope) if isinstance(bench_duts.get(scope), dict) else report.get("dut")
        if isinstance(dut, dict) and str(dut.get("serial") or "").strip():
            order_id = str(bench_orders.get(scope) or folder_run_id)
            return [
                _catalog_row(
                    report,
                    order_id,
                    stored,
                    measurement=measurement,
                    archived=archived,
                    dut=dut,
                    bench_scope=scope,
                    verdict=_catalog_verdict_for_bench(report, scope),
                )
            ]

    fallback_scope = scope if scope in {"A", "B"} else ""
    return [
        _catalog_row(
            report,
            str(bench_orders.get(fallback_scope) or folder_run_id) if fallback_scope else folder_run_id,
            stored,
            measurement=measurement,
            archived=archived,
            bench_scope=fallback_scope,
            verdict=str(report.get("global_verdict") or "EN CURSO"),
        )
    ]


def _catalog_row(
    report: dict,
    run_id: str,
    stored: set[str] | None,
    *,
    measurement: str,
    archived: bool,
    dut: dict | None = None,
    bench_scope: str = "",
    verdict: str = "",
) -> dict:
    dut = dut if isinstance(dut, dict) else (report.get("dut") if isinstance(report.get("dut"), dict) else {})
    sort_key = str(report.get("finished_at_utc") or report.get("started_at_utc") or "")
    scope = bench_scope or _bench_scope_from_report(report)
    if scope == "A+B":
        scope = ""
    bench_label = _bench_label(scope) or "—"
    dut_type = str(dut.get("type") or (report.get("dut") or {}).get("type") or "")
    label = verdict or str(report.get("global_verdict") or "EN CURSO")
    return {
        "run_id": run_id,
        "bench_scope": scope,
        "bench_label": bench_label,
        "verdict": label,
        "brand": dut.get("brand") or "",
        "model": dut.get("model") or "",
        "technology": {"ont": "GPON", "emta": "DOCSIS"}.get(dut_type, ""),
        "serial": dut.get("serial") or "",
        "mac": (dut.get("mac") or "").upper(),
        "search": _serial_search(dut.get("serial") or ""),
        "job": report.get("job") or "",
        "started_at": human_clock(str(report.get("started_at_local") or "")),
        "has_report": True,
        "stored": None if stored is None else run_id in stored,
        "archived": archived,
        "measurement": measurement if archived else "",
        "sort_key": sort_key,
    }


def _measurement_query(query: str) -> str:
    from urllib.parse import parse_qs

    values = parse_qs(query or "").get("m", [])
    if not values:
        return ""
    name = str(values[0] or "").strip()
    if not name or ".." in name or "/" in name or "\\" in name:
        return ""
    if not name.startswith("report-") or not name.endswith(".json"):
        return ""
    return name


def _report_json_path(folder: Path, measurement: str) -> Path | None:
    if not measurement:
        path = folder / "report.json"
        return path if path.is_file() else None
    path = folder / "measurements" / measurement
    if not path.is_file():
        return None
    resolved = path.resolve()
    base = (folder / "measurements").resolve()
    if base not in resolved.parents:
        return None
    return path


def _printable_for_folder(folder: Path, measurement: str, *, order_id: str = "") -> str | None:
    from tc_net_cert.core.models import CertificationReport
    from tc_net_cert.reporting.bench_slice import report_for_order_view
    from tc_net_cert.reporting.render import render_html

    payload = _report_json_path(folder, measurement)
    if payload is None:
        stored = folder / "report.html"
        if measurement or not stored.is_file():
            return None
        return readable_clocks(stored.read_text(encoding="utf-8"))
    try:
        report = CertificationReport.model_validate_json(payload.read_text(encoding="utf-8"))
    except Exception:
        return None
    view_id = order_id or folder.name
    report = report_for_order_view(report, view_id)
    return render_html(report)


def _serial_search(serial: str) -> str:
    from tc_net_cert.storage.runs import serial_forms

    return " ".join(sorted(serial_forms(serial))).lower()


def _latest_finished(runs: list[dict]) -> str:
    for run in runs:
        if run["has_report"]:
            return run["run_id"]
    return runs[0]["run_id"] if runs else ""


def _read_report(root: Path, run_id: str) -> dict | None:
    if not run_id:
        return None
    folder = find_run(root, run_id)
    if folder is None:
        return None
    path = folder / "report.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _status_payload(
    root: Path,
    console: Console,
    monitor: TrafficMonitor,
    ports: dict[str, str],
) -> dict:
    payload = console.status()
    finished = [run["run_id"] for run in _catalog(root) if run["has_report"]]
    payload["latest_run_id"] = finished[0] if finished else ""
    try:
        config, _loaded = load_config(console.config_path)
        ports = live_ports_mac_map(config)
    except Exception:
        config = None
    active_ports: set[str] | None = None
    try:
        if config is None:
            config, _loaded = load_config(console.config_path)
        from tc_net_cert.config.benches import bench_port_list

        bench = str(payload.get("bench_id") or "").strip().upper()
        if bench:
            names = bench_port_list(config, bench)
            active_ports = set(names) if names else None
    except Exception:
        active_ports = None
    payload["live"] = monitor.sample(
        ports,
        running=bool(payload["running"]),
        log=str(payload.get("log") or ""),
        runs_root=root,
        active_ports=active_ports,
    )
    try:
        if config is None:
            config, _loaded = load_config(console.config_path)
        payload["bench_map"] = {
            key: bench.model_dump(mode="json") for key, bench in resolve_benches(config).items()
        }
        payload["dual_bench_pending"] = any(not mac for name, mac in ports.items() if name in {"LAN5", "LAN6", "LAN7", "LAN8"})
    except Exception:
        payload["bench_map"] = {}
        payload["dual_bench_pending"] = False
    payload["bench_identities"] = load_bench_identities()
    payload["bench_readings"] = load_all_inventories()
    if _PANEL_BOOT:
        payload["panel_boot"] = _PANEL_BOOT
    payload["station_note"] = (
        "LAN nativa = Internet y base de datos. Wi-Fi de la estación = prueba 2.4/5 GHz. "
        "Cables de certificación = bancos A/B."
    )
    return payload


def _shown_reason(reason: str, speed: object, duplex: str) -> str:
    if reason == "Link detected: no" and isinstance(speed, int):
        return explain_link_down(speed, duplex)
    return reason


def _shown_iperf(state: object, throughput: object, tx: object, rx: object) -> str:
    text = str(state or "").replace("_", " ")
    if text in {"", "NOT RUN"} and throughput is None and tx is None and rx is None:
        return "NOT RUN"
    return text or "NOT RUN"


def _certificate(report: dict) -> dict:
    dut = report.get("dut") or {}
    ports = []
    for port in report.get("ports") or []:
        link = port.get("link") or {}
        dhcp = port.get("dhcp") or {}
        ports.append(
            {
                "name": port.get("logical_name") or "",
                "verdict": port.get("verdict") or "",
                "throughput": port.get("throughput_avg_mbps"),
                "link": port.get("physical_verdict") or "",
                "speed": link.get("speed_mbps") or "",
                "duplex": link.get("duplex") or "",
                "address": port.get("address") or "",
                "dhcp": dhcp.get("verdict") if isinstance(dhcp, dict) else "",
                "iperf": _shown_iperf(port.get("iperf_state"), port.get("throughput_avg_mbps"), port.get("tx_mbps"), port.get("rx_mbps")),
                "tx": port.get("tx_mbps"),
                "rx": port.get("rx_mbps"),
                "reason": _shown_reason(
                    port.get("failure_reason") or "",
                    link.get("speed_mbps"),
                    link.get("duplex") or "",
                ),
            }
        )
    matrix = report.get("matrix") or {}
    names = list(matrix)
    cells = []
    for src in names:
        row = []
        for dst in names:
            cell = (matrix.get(src) or {}).get(dst) or {}
            row.append(
                {
                    "same": src == dst,
                    "mbps": cell.get("mbps") if isinstance(cell, dict) else None,
                    "verdict": cell.get("verdict") if isinstance(cell, dict) else "",
                }
            )
        cells.append({"name": src, "cells": row})
    return {
        "run_id": report.get("run_id") or "",
        "job": report.get("job") or "",
        "title": report.get("title") or "",
        "verdict": report.get("global_verdict") or "",
        "identity_note": report.get("dut_identity_note") or "",
        "reasons": list(report.get("global_reasons") or []),
        "dut_type": dut.get("type") or "",
        "dut_model": dut.get("model") or "",
        "dut_serial": dut.get("serial") or "",
        "dut_mac": dut.get("mac") or "",
        "network_mode": report.get("network_mode") or "",
        "started": human_clock(report.get("started_at_local") or ""),
        "finished": human_clock(report.get("finished_at_local") or ""),
        "simultaneous": report.get("simultaneous_verdict") or "",
        "bidirectional": report.get("bidirectional_verdict") or "",
        "errors": report.get("errors_verdict") or "",
        "stability": report.get("stability_verdict") or "",
        "ports": ports,
        "matrix_names": names,
        "matrix_rows": cells,
    }


def main_from_config(config_path: Path | None, host: str, port: int) -> None:
    config, path = load_config(config_path)
    serve(config, host, port, config_path=path)
