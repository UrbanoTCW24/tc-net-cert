"""TC-NET-CERT command line."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from tc_net_cert.config.loader import (
    config_hash,
    config_to_document,
    default_config_path,
    load_config,
    parse_config,
    save_config,
)
from tc_net_cert.core.exceptions import ConfigError, ToolError
from tc_net_cert.core.executor import SystemExecutor
from tc_net_cert.core.models import AppConfig, Verdict
from tc_net_cert.network.discovery import candidates, discover_interfaces
from tc_net_cert.network.management import detect_management, mark_protected, persist_lab_routing
from tc_net_cert.orchestration.preflight import blocking, run_preflight
from tc_net_cert.orchestration.recover import recover
from tc_net_cert.orchestration.selftest import all_passed, logical_checks, privileged_checks
from tc_net_cert.orchestration.session import CertificationSession, _expand_stages, exit_code
from tc_net_cert.storage.runs import find_run, list_runs
from tc_net_cert.version import __version__
from tc_net_cert.web.server import serve

app = typer.Typer(add_completion=False, no_args_is_help=True, help="Ethernet port certification station")
test_app = typer.Typer(add_completion=False, no_args_is_help=True, help="Run a certification stage")
app.add_typer(test_app, name="test")
console = Console()


@app.callback()
def main_callback(
    ctx: typer.Context,
    config: Path | None = typer.Option(None, "--config", help="Configuration YAML"),
    debug: bool = typer.Option(False, "--debug", help="Show unexpected tracebacks"),
) -> None:
    ctx.ensure_object(dict)
    ctx.obj["config"] = config
    ctx.obj["debug"] = debug


@app.command()
def version() -> None:
    """Show the tool version."""
    console.print(f"TECHCORP LAB {__version__}")


@app.command()
def discover(ctx: typer.Context) -> None:
    """List physical Ethernet NICs and the protected management interface."""
    _guard(ctx, _discover)


@app.command()
def configure(
    ctx: typer.Context,
    port: list[str] = typer.Option([], "--port", help="LAN1=aa:bb:cc:dd:ee:ff"),
    dut_type: str | None = typer.Option(None, "--dut-type"),
    mode: str | None = typer.Option(None, "--mode", help="dhcp or static"),
    operator: str | None = typer.Option(None, "--operator"),
    station: str | None = typer.Option(None, "--station"),
    model: str | None = typer.Option(None, "--model"),
    serial: str | None = typer.Option(None, "--serial"),
    dut_mac: str | None = typer.Option(None, "--dut-mac"),
    firmware: str | None = typer.Option(None, "--firmware"),
    lot: str | None = typer.Option(None, "--lot"),
    work_order: str | None = typer.Option(None, "--work-order"),
    profile: str | None = typer.Option(None, "--profile"),
    save: Path | None = typer.Option(None, "--save", help="Destination YAML"),
) -> None:
    """Assign logical LAN names to NIC MACs and save the station configuration."""
    _guard(
        ctx,
        lambda: _configure(
            ctx,
            port=port,
            dut_type=dut_type,
            mode=mode,
            operator=operator,
            station=station,
            model=model,
            serial=serial,
            dut_mac=dut_mac,
            firmware=firmware,
            lot=lot,
            work_order=work_order,
            profile=profile,
            save=save,
        ),
    )


@app.command()
def preflight(ctx: typer.Context) -> None:
    """Check tools, port identity, and management protection before a run."""
    _guard(ctx, lambda: _preflight(ctx))


@app.command()
def status(ctx: typer.Context) -> None:
    """Show the configured logical ports and their current link."""
    _guard(ctx, lambda: _status(ctx))


@app.command("recover")
def recover_cmd(
    ctx: typer.Context,
    force: bool = typer.Option(False, "--force", help="Also delete leftover ns-lan* and ns-tcnc-* namespaces"),
) -> None:
    """Return test NICs to the root namespace after an interrupted run."""
    _guard(ctx, lambda: _recover(force))


@app.command("sync")
def sync_cmd(ctx: typer.Context) -> None:
    """Upload finished local runs that are not yet in Supabase."""
    _guard(ctx, lambda: _sync_runs(ctx))


@app.command("import-runs")
def import_runs_cmd(
    ctx: typer.Context,
    source: Path = typer.Argument(..., help="Carpeta runs, backup o .tar.gz"),
    dest: Path | None = typer.Option(None, "--dest", help="Destino (default: runs del lab)"),
    dry_run: bool = typer.Option(True, "--dry-run/--apply", help="Simular o copiar"),
    force: bool = typer.Option(False, "--force", help="Sobrescribir órdenes distintas (respaldo .bak-import-)"),
    reindex: bool = typer.Option(True, "--reindex/--no-reindex", help="Actualizar index.sqlite"),
) -> None:
    """Merge certification history from another machine without deleting the source."""
    _guard(ctx, lambda: _import_runs(ctx, source, dest, dry_run, force, reindex))


@app.command("fix-routes")
def fix_routes_cmd(
    ctx: typer.Context,
    wifi_connection: str = typer.Option("", "--wifi", help="NetworkManager Wi-Fi profile name (optional)"),
) -> None:
    """Make Wi-Fi the default route again and stop lab Ethernet from taking Internet."""
    _guard(ctx, lambda: _fix_routes(wifi_connection))


@app.command("self-test")
def self_test_cmd() -> None:
    """Validate parsers, reports, hashes, and, as root, namespaces plus SIGINT cleanup."""
    checks = logical_checks() + privileged_checks()
    _print_checks(checks)
    if not all_passed(checks):
        raise typer.Exit(code=1)


@app.command("verify")
def verify_cmd(
    ctx: typer.Context,
    operator: str = typer.Option("", "--operator", help="Lab user for sudo/NOPASSWD check"),
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8080, "--port"),
) -> None:
    """Print the post-install verification report (no secrets)."""
    _guard(ctx, lambda: _verify_install(operator, host, port))


@app.command("doctor")
def doctor_cmd(
    ctx: typer.Context,
    operator: str = typer.Option("", "--operator", help="Lab user for sudo/NOPASSWD check"),
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8080, "--port"),
) -> None:
    """Same as verify: installation and station health checks."""
    _guard(ctx, lambda: _verify_install(operator, host, port))


@app.command("register-station")
def register_station_cmd(ctx: typer.Context) -> None:
    """Claim LAB-NNN from Supabase (versionador) for this PC."""
    _guard(ctx, lambda: _register_station(ctx))


@app.command("register-tool")
def register_tool_cmd(ctx: typer.Context) -> None:
    """Assign TOOL-NNN in Supabase for this certifier PC (uses management_mac)."""
    _guard(ctx, lambda: _register_tool(ctx))


@app.command("schema-lab-tools")
def schema_lab_tools_cmd(ctx: typer.Context) -> None:
    """Create public.lab_tools in Supabase (DDL) if missing."""
    _guard(ctx, lambda: _schema_lab_tools(ctx))


@app.command()
def report(ctx: typer.Context, run_id: str = typer.Argument("")) -> None:
    """Print the stored verdict for a run. The files themselves are not rewritten."""
    _guard(ctx, lambda: _report(ctx, run_id))


@app.command()
def web(
    ctx: typer.Context,
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8080, "--port"),
) -> None:
    """Serve the local operator console."""
    _guard(ctx, lambda: _web(ctx, host, port))


@test_app.command("dhcp")
def test_dhcp(ctx: typer.Context) -> None:
    """Acquire one DHCP lease per namespace."""
    _guard(ctx, lambda: _test(ctx, {"dhcp"}))


@test_app.command("physical")
def test_physical(ctx: typer.Context) -> None:
    """Validate link, speed, duplex, and counters."""
    _guard(ctx, lambda: _test(ctx, {"physical"}))


@test_app.command("cross")
def test_cross(ctx: typer.Context) -> None:
    """Ping every ordered port pair."""
    _guard(ctx, lambda: _test(ctx, {"cross"}))


@test_app.command("throughput")
def test_throughput(
    ctx: typer.Context,
    duration: int | None = typer.Option(None, "--duration"),
    streams: int | None = typer.Option(None, "--streams"),
    single_stream: bool = typer.Option(False, "--single-stream"),
    allow_partial: bool = typer.Option(False, "--allow-partial-ports"),
    confirm_management_move: str = typer.Option("", "--confirm-management-move"),
) -> None:
    """Run the full directional iPerf3 matrix."""
    _guard(
        ctx,
        lambda: _test(
            ctx,
            {"throughput"},
            duration=duration,
            streams=streams,
            single_stream=single_stream,
            allow_partial=allow_partial,
            confirm_management_move=confirm_management_move,
        ),
    )


@test_app.command("simultaneous")
def test_simultaneous(
    ctx: typer.Context,
    duration: int | None = typer.Option(None, "--duration"),
    streams: int | None = typer.Option(None, "--streams"),
    allow_partial: bool = typer.Option(False, "--allow-partial-ports"),
    confirm_management_move: str = typer.Option("", "--confirm-management-move"),
) -> None:
    """Run the configured 2x2 pairs in both directions at the same time."""
    _guard(
        ctx,
        lambda: _test(
            ctx,
            {"simultaneous"},
            duration=duration,
            streams=streams,
            allow_partial=allow_partial,
            confirm_management_move=confirm_management_move,
        ),
    )


@test_app.command("wifi")
def test_wifi(
    ctx: typer.Context,
    duration: int | None = typer.Option(None, "--duration"),
    streams: int | None = typer.Option(None, "--streams"),
    allow_partial: bool = typer.Option(False, "--allow-partial-ports"),
    confirm_management_move: str = typer.Option("", "--confirm-management-move"),
) -> None:
    """Measure DUT Wi-Fi 5 GHz first, then 2.4 GHz, toward LAN2 or LAN3 if LAN2 has no address."""
    _guard(
        ctx,
        lambda: _test(
            ctx,
            {"wifi"},
            duration=duration,
            streams=streams,
            allow_partial=allow_partial,
            confirm_management_move=confirm_management_move,
        ),
    )


@test_app.command("bidirectional")
def test_bidirectional(
    ctx: typer.Context,
    duration: int | None = typer.Option(None, "--duration"),
    streams: int | None = typer.Option(None, "--streams"),
    allow_partial: bool = typer.Option(False, "--allow-partial-ports"),
    confirm_management_move: str = typer.Option("", "--confirm-management-move"),
) -> None:
    """Run both directions of each configured pair in one window."""
    _guard(
        ctx,
        lambda: _test(
            ctx,
            {"bidirectional"},
            duration=duration,
            streams=streams,
            allow_partial=allow_partial,
            confirm_management_move=confirm_management_move,
        ),
    )


@test_app.command("all")
def test_all(
    ctx: typer.Context,
    duration: int | None = typer.Option(None, "--duration"),
    streams: int | None = typer.Option(None, "--streams"),
    single_stream: bool = typer.Option(False, "--single-stream"),
    allow_partial: bool = typer.Option(False, "--allow-partial-ports"),
    confirm_management_move: str = typer.Option("", "--confirm-management-move"),
) -> None:
    """Run the DUT profile's full certification chain."""
    _guard(
        ctx,
        lambda: _test(
            ctx,
            {"all"},
            duration=duration,
            streams=streams,
            single_stream=single_stream,
            allow_partial=allow_partial,
            confirm_management_move=confirm_management_move,
        ),
    )


def main() -> None:
    app()


def _guard(ctx: typer.Context, action) -> None:
    try:
        action()
    except typer.Exit:
        raise
    except (ToolError, ConfigError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    except Exception as exc:
        if ctx.obj and ctx.obj.get("debug"):
            raise
        console.print(f"[red]{type(exc).__name__}: {exc}[/red]")
        raise typer.Exit(code=1) from exc


def _load(ctx: typer.Context) -> tuple[AppConfig, Path]:
    return load_config(ctx.obj.get("config"))


def _discover() -> None:
    executor = SystemExecutor()
    interfaces = discover_interfaces(executor)
    plane = detect_management(executor, interfaces)
    interfaces = mark_protected(interfaces, plane)
    table = Table(title="Physical Ethernet candidates")
    table.add_column("ID")
    table.add_column("Interface")
    table.add_column("MAC")
    table.add_column("Vendor/Model")
    table.add_column("State")
    visible = candidates(interfaces)
    for index, record in enumerate(visible, start=1):
        table.add_row(str(index), record.name, record.mac, record.display_vendor, record.operstate)
    console.print(table)
    if not visible:
        console.print("No eligible physical Ethernet NIC was found.")
    excluded = Table(title="Excluded or protected")
    excluded.add_column("Interface")
    excluded.add_column("Reason")
    for record in interfaces:
        if record.excluded or record.protected:
            reason = record.exclusion_reason or ", ".join(record.protection_reasons)
            excluded.add_row(record.name, reason)
    console.print(excluded)
    if plane.default_route_interface:
        console.print(f"Management route: {plane.default_route_interface} via {plane.default_route_gateway}")


def _configure(ctx: typer.Context, **options) -> None:
    try:
        config, path = _load(ctx)
    except ConfigError:
        config, path = load_config(_packaged_default())
    if options["port"]:
        mapping = dict(config.ports)
        raw_ports = {name: {"mac": item.mac} for name, item in mapping.items()}
        for item in options["port"]:
            if "=" not in item:
                raise ConfigError("--port must look like LAN1=aa:bb:cc:dd:ee:ff")
            name, mac = item.split("=", 1)
            raw_ports[name.upper()] = {"mac": mac}
        document = config_to_document(config)
        document["ports"] = raw_ports
        config = parse_config(document)
    elif sys.stdin.isatty():
        config = _configure_interactive(config)
    if options["dut_type"]:
        if options["dut_type"] not in config.dut_profiles:
            raise ConfigError(f"Unknown DUT profile '{options['dut_type']}'")
        config.dut.type = options["dut_type"]
        config.network_mode = config.dut_profiles[options["dut_type"]].mode
    if options["mode"]:
        config.network_mode = options["mode"].lower()
    for field, value in (
        ("operator", options["operator"]),
        ("station", options["station"]),
    ):
        if value is not None:
            setattr(config, field, value)
    for field, value in (
        ("model", options["model"]),
        ("serial", options["serial"]),
        ("mac", options["dut_mac"]),
        ("firmware", options["firmware"]),
        ("lot", options["lot"]),
        ("work_order", options["work_order"]),
    ):
        if value is not None:
            setattr(config.dut, field, value)
    if options["profile"]:
        aliases = {"1G": "gigabit", "gigabit": "gigabit", "100M": "100M", "2.5G": "2.5G"}
        speeds = {"gigabit": "1G", "100M": "100M", "2.5G": "2.5G"}
        selected = aliases.get(options["profile"], options["profile"])
        config.active_profile = selected
        if selected in speeds:
            config.active_speed_profile = speeds[selected]
    config = parse_config(config_to_document(config))
    destination = options["save"] or _save_target(ctx, path)
    save_config(config, destination)
    console.print(f"Saved {destination}")
    console.print(f"config_hash {config_hash(config)}")
    for name, item in config.ports.items():
        console.print(f"{name} {item.mac}")


def _configure_interactive(config: AppConfig) -> AppConfig:
    executor = SystemExecutor()
    found = candidates(mark_protected(discover_interfaces(executor), detect_management(executor, discover_interfaces(executor))))
    for index, record in enumerate(found, start=1):
        console.print(f"{index}  {record.name}  {record.mac}  {record.display_vendor}")
    from tc_net_cert.config.loader import config_to_document, normalize_mac

    document = config_to_document(config)
    ports = dict(document["ports"])
    count = typer.prompt("How many DUT ports will be certified?", default=len(ports) or 1, type=int)
    for index in range(1, count + 1):
        choice = typer.prompt(f"Candidate ID or MAC for LAN{index}")
        if choice.isdigit() and 1 <= int(choice) <= len(found):
            mac = found[int(choice) - 1].mac
        else:
            mac = normalize_mac(choice)
        ports[f"LAN{index}"] = {"mac": mac}
    document["ports"] = ports
    dut_type = typer.prompt("DUT type", default=config.dut.type)
    document["dut"]["type"] = dut_type
    document["network"]["mode"] = config.dut_profiles[dut_type].mode
    document["operator"] = typer.prompt("Operator", default=config.operator)
    document["station"] = typer.prompt("Station", default=config.station)
    document["dut"]["model"] = typer.prompt("DUT model", default=config.dut.model)
    document["dut"]["serial"] = typer.prompt("DUT serial", default=config.dut.serial)
    return parse_config(document)


def _preflight(ctx: typer.Context) -> None:
    config, _path = _load(ctx)
    executor = SystemExecutor()
    interfaces = discover_interfaces(executor)
    plane = detect_management(executor, interfaces)
    interfaces = mark_protected(interfaces, plane)
    stages = _expand_stages({"all"}, config)
    checks = run_preflight(config, executor, interfaces, plane, stages=stages, allow_partial=False)
    _print_preflight(checks)
    if blocking(checks):
        raise typer.Exit(code=1)


def _status(ctx: typer.Context) -> None:
    config, _path = _load(ctx)
    executor = SystemExecutor()
    interfaces = discover_interfaces(executor)
    by_mac = {item.mac: item for item in interfaces}
    console.print("TECHCORP LAB")
    console.print("======================================")
    if not config.ports:
        console.print("No ports configured. Run: tc-net-cert discover")
        return
    for name, port in config.ports.items():
        record = by_mac.get(port.mac)
        if record is None:
            console.print(f"{name}  {port.mac}  NOT PRESENT")
            continue
        link = record.operstate
        address = record.addresses[0] if record.addresses else "-"
        console.print(f"{name}  {record.name}  {link}  {address}  {record.mac}")


def _recover(force: bool) -> None:
    for line in recover(SystemExecutor(), force_prefixes=force):
        console.print(line)


def _import_runs(
    ctx: typer.Context,
    source: Path,
    dest: Path | None,
    dry_run: bool,
    force: bool,
    reindex: bool,
) -> None:
    from tc_net_cert.config.loader import runs_directory
    from tc_net_cert.storage.import_runs import format_import_report, import_runs

    config, _path = _load(ctx)
    dest_root = dest or runs_directory(config)
    result = import_runs(source, dest_root, dry_run=dry_run, force=force, reindex=reindex)
    console.print(format_import_report(result))
    if not result.ok:
        raise typer.Exit(code=1)


def _verify_install(operator: str, host: str, port: int) -> None:
    from tc_net_cert.diagnostics.install_verify import format_report, run_install_verify

    op = operator.strip() or os.environ.get("SUDO_USER", "")
    lines, state, code = run_install_verify(operator=op, panel_host=host, panel_port=port)
    console.print(format_report(lines, state))
    if code:
        raise typer.Exit(code=code)


def _fix_routes(wifi_connection: str) -> None:
    for line in persist_lab_routing(SystemExecutor(), wifi_connection=wifi_connection.strip()):
        console.print(line)
    console.print("Comprueba: ip route show default")


def _schema_lab_tools(ctx: typer.Context) -> None:
    from tc_net_cert.config.tool_registry import apply_lab_tools_schema
    from tc_net_cert.storage.supabase_runs import config_path

    result = apply_lab_tools_schema(config_path())
    if result.get("ok"):
        if result.get("already_present"):
            console.print("lab_tools ya existe en Supabase.")
        else:
            console.print("lab_tools creado en Supabase.")
        return
    console.print(result.get("error") or "No se pudo crear el esquema.")
    console.print("Pegue lab_tools_paso1_minimo.sql en Supabase SQL Editor (no el SELECT de verificación).")
    raise typer.Exit(code=1)


def _register_tool(ctx: typer.Context) -> None:
    config, path = _load(ctx)
    from tc_net_cert.config.remote_sync import get_or_register_tool_identity, resolve_country_code
    from tc_net_cert.storage.supabase_runs import config_path
    from tc_net_cert.version import __version__

    if not config.management_mac:
        console.print("Falta network.management_mac en la config (interfaz de gestión, SAFE MODE).")
        raise typer.Exit(code=1)
    result = get_or_register_tool_identity(
        station_id=config.station,
        country_code=resolve_country_code(config.station),
        installed_version=__version__,
        mac_host=config.management_mac,
        env_path=config_path(),
    )
    if result.get("ok"):
        console.print(f"Herramienta: {result.get('tool_number')} · versión {result.get('tool_version')}")
        if result.get("error"):
            console.print(str(result["error"]))
        return
    console.print(result.get("error") or "No se pudo registrar la herramienta.")
    raise typer.Exit(code=1)


def _register_station(ctx: typer.Context) -> None:
    config, path = _load(ctx)
    from tc_net_cert.config.station_provision import provision_station
    from tc_net_cert.storage.supabase_runs import config_path

    result = provision_station(config_path=path, env_path=config_path())
    if result.get("ok"):
        console.print(f"Estación: {result.get('station_code')}")
        if result.get("config_updated"):
            console.print(f"Actualizado station en {path}")
        elif result.get("error"):
            console.print(str(result["error"]))
        return
    console.print(result.get("error") or "No se pudo registrar.")
    raise typer.Exit(code=1)


def _sync_runs(ctx: typer.Context) -> None:
    config, _path = _load(ctx)
    from tc_net_cert.config.loader import runs_directory
    from tc_net_cert.storage.supabase_runs import config_path, sync_unsent_runs

    result = sync_unsent_runs(
        runs_directory(config),
        env_file=config_path(),
        station=config.station,
        operator=config.operator,
    )
    sent = result.get("sent") or []
    failed = int(result.get("failed") or 0)
    if sent:
        console.print("Enviadas: " + ", ".join(sent))
    if failed:
        console.print(f"Fallidas al subir: {failed}")
    if result.get("error"):
        console.print(str(result["error"]))
    if not sent and not failed:
        console.print(
            "Nada pendiente, ya están en nube, o no hay conexión "
            "(ejecuta «doctor» y revisa listed_run_ids / red)."
        )
    if not result.get("ok"):
        raise typer.Exit(code=1)


def _report(ctx: typer.Context, run_id: str) -> None:
    config, _path = _load(ctx)
    from tc_net_cert.config.loader import runs_directory
    import json

    root = runs_directory(config)
    folder = find_run(root, run_id) if run_id else None
    if folder is None:
        runs = list_runs(root)
        if not runs:
            raise ToolError(f"No runs under {root}")
        folder = runs[-1]
    payload = json.loads((folder / "report.json").read_text(encoding="utf-8"))
    console.print(payload.get("title", ""))
    console.print(payload["run_id"])
    console.print(f"NETWORK MODE: {payload['network_mode']}")
    console.print(f"FINAL RESULT: {payload['global_verdict']}")
    for port in payload.get("ports", []):
        console.print(f"{port['logical_name']}: {port['verdict']}")
    console.print(folder)


def _web(ctx: typer.Context, host: str, port: int) -> None:
    config, path = _load(ctx)
    serve(config, host, port, config_path=path)


def _test(
    ctx: typer.Context,
    stages: set[str],
    duration: int | None = None,
    streams: int | None = None,
    single_stream: bool = False,
    allow_partial: bool = False,
    confirm_management_move: str = "",
) -> None:
    config, _path = _load(ctx)
    if duration is not None:
        if duration not in config.iperf.allowed_durations:
            raise ConfigError(f"Duration must be one of {config.iperf.allowed_durations}")
        config.iperf.duration_seconds = duration
    if streams is not None:
        if streams not in config.iperf.allowed_streams:
            raise ConfigError(f"Streams must be one of {config.iperf.allowed_streams}")
        config.iperf.streams = streams
    if single_stream:
        config.iperf.run_single_stream = True
    if duration is not None and duration > config.long_duration_seconds:
        console.print("LONG DURATION TEST")
        console.print("Thermal/stability test enabled")
    session = CertificationSession(
        config,
        SystemExecutor(),
        stages,
        allow_partial=allow_partial,
        override_token=confirm_management_move,
        notify=console.print,
    )
    result = session.execute()
    code = exit_code(result)
    style = "green" if result.global_verdict == Verdict.PASS else "red"
    console.print(f"[{style}]FINAL RESULT: {result.global_verdict.value}[/{style}]")
    if result.cleanup.verdict != Verdict.PASS:
        console.print(f"[red]STATION CLEANUP: {result.cleanup.verdict.value}[/red]")
    raise typer.Exit(code=code)


def _print_preflight(checks) -> None:
    table = Table(title="Preflight")
    table.add_column("Check")
    table.add_column("Result")
    table.add_column("Detail")
    for check in checks:
        table.add_row(check.name, check.verdict.value, check.detail)
    console.print(table)


def _print_checks(checks: list[tuple[str, bool, str]]) -> None:
    table = Table(title="Self-test")
    table.add_column("Check")
    table.add_column("Result")
    table.add_column("Detail")
    for name, ok, detail in checks:
        table.add_row(name, "PASS" if ok else "FAIL", detail)
    console.print(table)


def _packaged_default() -> Path:
    return Path(__file__).resolve().parents[1] / "config" / "defaults" / "config.yaml"


def _save_target(ctx: typer.Context, loaded: Path) -> Path:
    explicit = ctx.obj.get("config")
    if explicit is not None:
        return explicit
    if "defaults" in loaded.parts:
        return default_config_path()
    return loaded
