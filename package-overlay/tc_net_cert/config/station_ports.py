"""Assign or validate LAN ports during USB install (no destructive network changes)."""

from __future__ import annotations

import argparse
import shutil
import socket
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from tc_net_cert.config.benches import DEFAULT_BENCHES
from tc_net_cert.config.loader import (
    config_to_document,
    default_simultaneous_pairs,
    load_config,
    normalize_mac,
    parse_config,
    save_config,
)
from tc_net_cert.core.executor import SystemExecutor
from tc_net_cert.core.models import AppConfig, InterfaceRecord
from tc_net_cert.network.discovery import candidates, discover_interfaces, index_by_mac
from tc_net_cert.network.management import detect_management, mark_protected, pin_management_mac


@dataclass
class PortLayoutResult:
    changed: bool
    kept: bool
    backup: Path | None = None
    messages: list[str] = field(default_factory=list)
    exit_code: int = 0


def backup_config(path: Path) -> Path | None:
    if not path.is_file():
        return None
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    dest = path.with_name(f"config.yaml.bak-{stamp}")
    shutil.copy2(path, dest)
    return dest


def _mark_lab_interfaces(
    executor: SystemExecutor,
    interfaces: list[InterfaceRecord],
    *,
    management_mac: str = "",
) -> list[InterfaceRecord]:
    plane = detect_management(executor, interfaces)
    marked = mark_protected(interfaces, plane)
    return pin_management_mac(marked, management_mac)


def _certification_candidates(
    executor: SystemExecutor,
    *,
    limit: int = 8,
    management_mac: str = "",
) -> list[InterfaceRecord]:
    found = discover_interfaces(executor)
    marked = _mark_lab_interfaces(executor, found, management_mac=management_mac)
    usable = candidates(marked)
    return usable[:limit]


def _interface_table(records: list[InterfaceRecord]) -> str:
    lines = ["Interfaces detectadas (Ethernet de prueba, sin gestión/Wi‑Fi):"]
    if not records:
        lines.append("  (ninguna apta para certificación)")
        return "\n".join(lines)
    for record in records:
        flags = []
        if record.protected:
            flags.append("gestión")
        if record.excluded:
            flags.append(record.exclusion_reason or "excluida")
        if record.is_wireless:
            flags.append("inalámbrica")
        note = f" [{', '.join(flags)}]" if flags else ""
        lines.append(f"  {record.name}  {record.mac}{note}")
    return "\n".join(lines)


def _configured_port_issues(config: AppConfig, by_mac: dict[str, InterfaceRecord]) -> list[str]:
    issues: list[str] = []
    for logical, port in config.ports.items():
        try:
            mac = normalize_mac(port.mac)
        except Exception:
            issues.append(f"{logical}: MAC inválida")
            continue
        record = by_mac.get(mac)
        if record is None:
            issues.append(f"{logical}: MAC {mac} no está en esta PC")
            continue
        try:
            cfg_mac = normalize_mac(getattr(config, "management_mac", "") or "")
            if cfg_mac and mac == cfg_mac:
                issues.append(
                    f"{logical}: {mac} ({record.name}) es LAN de administración; no debe estar en ports"
                )
                continue
        except Exception:
            pass
        if record.is_wireless:
            issues.append(f"{logical}: {mac} ({record.name}) es inalámbrica")
        elif record.excluded:
            issues.append(f"{logical}: {mac} ({record.name}) excluida ({record.exclusion_reason})")
        elif record.protected:
            why = ", ".join(record.protection_reasons) or "gestión"
            issues.append(f"{logical}: {mac} ({record.name}) es de gestión ({why})")
    return issues


def _apply_port_layout(
    document: dict,
    visible: list[InterfaceRecord],
    *,
    operator: str,
    station: str,
) -> None:
    port_names = [f"LAN{index}" for index in range(1, len(visible) + 1)]
    document["ports"] = {name: {"mac": record.mac} for name, record in zip(port_names, visible, strict=True)}
    document["simultaneous_pairs"] = [list(pair) for pair in default_simultaneous_pairs(port_names)]
    document["require_profile_port_count"] = len(port_names) >= 4 and len(port_names) < 8
    if len(port_names) >= 8:
        document.setdefault("benches", {})
        if isinstance(document["benches"], dict):
            document["benches"]["A"] = {"label": "Banco A", "ports": port_names[:4]}
            document["benches"]["B"] = {"label": "Banco B", "ports": port_names[4:8]}
    document.setdefault("network", {})
    if isinstance(document["network"], dict):
        document["network"]["mode"] = document["network"].get("mode") or "dhcp"
    document["safe_mode"] = True
    document.setdefault("dhcp", {})
    if isinstance(document["dhcp"], dict):
        document["dhcp"]["backend"] = document["dhcp"].get("backend") or "dhcpcd"
    if not document.get("operator"):
        document["operator"] = operator or "operador"
    if not document.get("station"):
        document["station"] = (station or socket.gethostname() or "LAB")[:32]


def plan_and_apply_ports(
    config_path: Path,
    *,
    operator: str = "",
    station: str = "",
    auto_confirm: bool = False,
) -> PortLayoutResult:
    result = PortLayoutResult(changed=False, kept=False)
    config, _loaded = load_config(config_path)
    executor = SystemExecutor()
    found = discover_interfaces(executor)
    marked = _mark_lab_interfaces(executor, found, management_mac=config.management_mac)
    try:
        by_mac = index_by_mac(marked)
    except RuntimeError as exc:
        result.messages.append(f"Ambigüedad de red: {exc}")
        result.messages.append(_interface_table(candidates(marked)))
        result.messages.append(
            "Desconecta interfaces duplicadas o confirma con --yes tras revisar la tabla."
        )
        result.exit_code = 2 if not auto_confirm else 3
        return result

    visible = _certification_candidates(executor, management_mac=config.management_mac)
    first_time = not config.ports
    issues = _configured_port_issues(config, by_mac) if config.ports else []

    if config.ports and not issues:
        document = config_to_document(config)
        document["simultaneous_pairs"] = [list(pair) for pair in default_simultaneous_pairs(list(config.ports.keys()))]
        if len(config.ports) < 4:
            document["require_profile_port_count"] = False
        save_config(parse_config(document), config_path)
        result.kept = True
        result.messages.append("Puertos conservados: todas las MAC configuradas existen y son aptas.")
        for logical, port in config.ports.items():
            record = by_mac[normalize_mac(port.mac)]
            result.messages.append(f"  {logical}  {port.mac}  {record.name}")
        result.messages.append(f"Pares simultáneos: {document['simultaneous_pairs']}")
        if len(config.ports) < 4:
            result.messages.append(
                f"Advertencia: solo {len(config.ports)} LAN. "
                "La certificación completa de 4 puertos no aplica hasta conectar más Ethernet."
            )
        return result

    if not visible:
        result.messages.append("No hay Ethernet apta para certificación (gestión/Wi‑Fi/virtual excluidas).")
        result.messages.append(_interface_table(marked))
        result.exit_code = 1
        return result

    if first_time:
        result.messages.append("Primera asignación de puertos en esta PC.")
    else:
        result.messages.append("Se reasignarán los puertos de prueba en esta PC.")
        if issues:
            result.messages.append("Motivo:")
            result.messages.extend(f"  - {item}" for item in issues)
    result.messages.append(_interface_table(visible))
    if len(visible) < 4:
        result.messages.append(
            f"Advertencia: {len(visible)} interfaz(es) de prueba. "
            "require_profile_port_count=false; no se declara certificación 4-LAN completa."
        )

    if not first_time and not auto_confirm:
        result.messages.append(
            "Confirma la asignación ejecutando de nuevo: sudo bash instalar.sh --yes"
        )
        result.exit_code = 2
        return result

    backup = backup_config(config_path)
    result.backup = backup
    if backup:
        result.messages.append(f"Respaldo: {backup}")

    document = config_to_document(config)
    _apply_port_layout(document, visible, operator=operator, station=station)
    save_config(parse_config(document), config_path)
    result.changed = True
    result.messages.append("Tarjetas asignadas:")
    for name, payload in document["ports"].items():
        mac = str(payload.get("mac") or "")
        record = by_mac.get(normalize_mac(mac))
        kernel = record.name if record else "?"
        result.messages.append(f"  {name}  {mac}  {kernel}")
    result.messages.append(f"Pares simultáneos: {document['simultaneous_pairs']}")
    if result.changed:
        ports = document.get("ports") or {}
        if len(ports) >= 8:
            for bid in ("A", "B"):
                result.messages.extend(
                    reconfigure_bench_lan(
                        executor,
                        bid,
                        config_path=config_path,
                        by_mac=by_mac,
                    )
                )
        elif len(ports) >= 4:
            result.messages.extend(
                reconfigure_bench_lan(
                    executor,
                    "A",
                    config_path=config_path,
                    by_mac=by_mac,
                )
            )
    return result


def _lan_order(name: str) -> int:
    digits = "".join(ch for ch in name if ch.isdigit())
    return int(digits or "0")


def plan_and_apply_bench_ports(
    config_path: Path,
    bench_id: str,
    *,
    operator: str = "",
    station: str = "",
    auto_confirm: bool = True,
) -> PortLayoutResult:
    """Assign LAN1–4 or LAN5–8 without disturbing the other bank when possible."""
    bench_id = bench_id.upper()
    if bench_id not in DEFAULT_BENCHES:
        return PortLayoutResult(
            changed=False,
            kept=False,
            messages=[f"Banco no reconocido: {bench_id}"],
            exit_code=1,
        )
    target_names = list(DEFAULT_BENCHES[bench_id].ports)
    other_id = "B" if bench_id == "A" else "A"
    other_names = list(DEFAULT_BENCHES[other_id].ports)
    result = PortLayoutResult(changed=False, kept=False)
    config, _loaded = load_config(config_path)
    executor = SystemExecutor()
    found = discover_interfaces(executor)
    marked = _mark_lab_interfaces(executor, found, management_mac=config.management_mac)
    try:
        by_mac = index_by_mac(marked)
    except RuntimeError as exc:
        result.messages.append(f"Ambigüedad de red: {exc}")
        result.exit_code = 2
        return result
    visible = _certification_candidates(executor, management_mac=config.management_mac)
    visible_macs = {normalize_mac(record.mac) for record in visible}

    def mac_ok(mac: str) -> bool:
        try:
            normalized = normalize_mac(mac)
        except Exception:
            return False
        return normalized in visible_macs

    ports_doc: dict[str, dict[str, str]] = {}
    used: set[str] = set()

    for name in other_names:
        port = config.ports.get(name)
        if port and mac_ok(port.mac):
            normalized = normalize_mac(port.mac)
            if normalized not in used:
                ports_doc[name] = {"mac": normalized}
                used.add(normalized)

    for name in target_names:
        port = config.ports.get(name)
        if port and mac_ok(port.mac):
            normalized = normalize_mac(port.mac)
            if normalized not in used:
                ports_doc[name] = {"mac": normalized}
                used.add(normalized)

    try:
        blocked_mgmt = normalize_mac(config.management_mac or "")
    except Exception:
        blocked_mgmt = ""

    def usable(record: InterfaceRecord) -> bool:
        mac = normalize_mac(record.mac)
        if mac in used:
            return False
        if blocked_mgmt and mac == blocked_mgmt:
            return False
        return True

    pool = [record for record in visible if usable(record)]
    for name in target_names:
        if name in ports_doc:
            continue
        if not pool:
            result.messages.append(
                f"Banco {bench_id}: faltan adaptadores Ethernet. "
                f"Conecta cables al hub y vuelve a remapear ({name} sin NIC)."
            )
            result.exit_code = 1
            return result
        record = pool.pop(0)
        normalized = normalize_mac(record.mac)
        ports_doc[name] = {"mac": normalized}
        used.add(normalized)

    if not auto_confirm and config.ports:
        result.messages.append(f"Confirma con --yes para remapear banco {bench_id}.")
        result.exit_code = 2
        return result

    backup = backup_config(config_path)
    result.backup = backup
    if backup:
        result.messages.append(f"Respaldo: {backup}")

    document = config_to_document(config)
    merged: dict[str, dict[str, str]] = {
        name: {"mac": normalize_mac(port.mac)} for name, port in config.ports.items()
    }
    for name, entry in ports_doc.items():
        merged[name] = {"mac": entry["mac"]}
    ordered = sorted(merged.keys(), key=_lan_order)
    document["ports"] = {name: merged[name] for name in ordered}
    document["simultaneous_pairs"] = [list(pair) for pair in default_simultaneous_pairs(ordered)]
    document["require_profile_port_count"] = len(ordered) >= 4 and len(ordered) < 8
    document.setdefault("benches", {})
    if isinstance(document["benches"], dict):
        bank_a = [name for name in ordered if _lan_order(name) <= 4]
        bank_b = [name for name in ordered if _lan_order(name) >= 5]
        if bank_a:
            document["benches"]["A"] = {"label": "Banco A", "ports": bank_a}
        if bank_b:
            document["benches"]["B"] = {"label": "Banco B", "ports": bank_b}
    save_config(parse_config(document), config_path)
    result.changed = True
    result.messages.append(f"Banco {bench_id} remapeado:")
    for name in target_names:
        mac = ports_doc[name]["mac"]
        record = by_mac.get(mac)
        kernel = record.name if record else "?"
        result.messages.append(f"  {name}  {mac}  {kernel}")
    result.messages.append(f"Pares simultáneos: {document['simultaneous_pairs']}")
    result.messages.extend(
        reconfigure_bench_lan(
            executor,
            bench_id,
            config_path=config_path,
            by_mac=by_mac,
        )
    )
    return result


def reconfigure_bench_lan(
    executor: SystemExecutor,
    bench_id: str,
    *,
    config_path: Path,
    by_mac: dict | None = None,
) -> list[str]:
    """After remap: drop stale namespaces, link up, renew DHCP on that bench only."""
    from tc_net_cert.config.benches import DEFAULT_BENCHES
    from tc_net_cert.network.management import refresh_portal_links
    from tc_net_cert.orchestration.bench_recover import recover_bench

    bench_id = bench_id.upper()
    lines: list[str] = [f"Reconfigurando LAN del banco {bench_id}…"]
    lines.extend(recover_bench(executor, bench_id, config_path=config_path))
    config, _loaded = load_config(config_path)
    target_names = list(DEFAULT_BENCHES.get(bench_id, DEFAULT_BENCHES["A"]).ports)
    if by_mac is None:
        found = discover_interfaces(executor)
        by_mac = index_by_mac(
            _mark_lab_interfaces(executor, found, management_mac=config.management_mac)
        )
    port_macs: dict[str, str] = {}
    for logical in target_names:
        port = config.ports.get(logical)
        if port is None:
            continue
        port_macs[logical] = port.mac
        record = by_mac.get(normalize_mac(port.mac))
        iface = record.name if record else ""
        if not iface:
            lines.append(f"{logical}: sin interfaz en el kernel")
            continue
        up = executor.run(["ip", "link", "set", iface, "up"], timeout=10)
        if up.ok:
            lines.append(f"{logical}: {iface} enlace UP")
    try:
        lines.extend(refresh_portal_links(executor, port_macs))
    except Exception as exc:
        lines.append(f"DHCP: {exc}")
    lines.append(f"Banco {bench_id}: reconfiguración LAN terminada.")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate or assign certification LAN ports.")
    parser.add_argument("--config", type=Path, default=Path("/etc/tc-net-cert/config.yaml"))
    parser.add_argument("--operator", default="")
    parser.add_argument("--station", default="")
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Confirm port reassignment when MAC from another PC are missing.",
    )
    parser.add_argument(
        "--bench",
        choices=("A", "B"),
        default="",
        help="Remap only LAN1–4 (A) or LAN5–8 (B), keeping the other bank when possible.",
    )
    args = parser.parse_args(argv)
    if args.bench:
        outcome = plan_and_apply_bench_ports(
            args.config,
            args.bench,
            operator=args.operator,
            station=args.station,
            auto_confirm=args.yes,
        )
    else:
        outcome = plan_and_apply_ports(
            args.config,
            operator=args.operator,
            station=args.station,
            auto_confirm=args.yes,
        )
    for line in outcome.messages:
        print(line)
    return outcome.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
