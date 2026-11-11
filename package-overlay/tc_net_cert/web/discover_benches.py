"""Portal discovery scoped to bench A/B."""

from __future__ import annotations

from pathlib import Path

from tc_net_cert.config.benches import bench_ports, resolve_benches
from tc_net_cert.config.loader import load_config, runs_directory
from tc_net_cert.core.models import AppConfig
from tc_net_cert.dut.huawei import allowed_host, discover_portal, dut_paths, portal_retryable, read_portal, valid_password, valid_user
from tc_net_cert.dut.portals import load_portals, portals_path
from tc_net_cert.network.management import cable_on_lab_plane, iface_mac, management_blocked_macs
from tc_net_cert.storage.bench_identity import store_bench
from tc_net_cert.storage.runs import order_for_serial


def _leftover_lab_namespaces() -> list[str]:
    netns = Path("/var/run/netns")
    if not netns.is_dir():
        return []
    return sorted(entry.name for entry in netns.iterdir() if entry.name.startswith("ns-lan"))


def _configured_bench_macs(config: AppConfig) -> set[str]:
    blocked = management_blocked_macs(config)
    macs: set[str] = set()
    for bench_id in resolve_benches(config):
        for mac in bench_ports(config, bench_id).values():
            if mac and mac.lower() not in blocked:
                macs.add(mac.lower())
    return macs


def ensure_bench_nics_in_root(config: AppConfig, executor) -> list[str]:
    """After a cert run, lab NICs can remain inside ns-lan*; discover needs them in root."""
    macs = _configured_bench_macs(config)
    if not macs or _linked_nics(macs):
        return []
    if not _leftover_lab_namespaces():
        return []
    from tc_net_cert.orchestration.recover import recover

    return recover(executor, force_prefixes=True)


def _dut_kind_from_reading(found: dict) -> str:
    kind = str(found.get("family") or "")
    if kind in {"ont", "emta"}:
        return kind
    model = str(found.get("model") or "").upper()
    brand = str(found.get("brand") or "").upper()
    if model in {"CGNV5", "CGNV5CLR"} or brand in {"HISTRON", "HITRON", "KAON"}:
        return "emta"
    if model.startswith("CG") or "CGNV" in model.replace("-", ""):
        return "emta"
    if model.startswith("HG") or brand == "HUAWEI":
        return "ont"
    return ""


def _expected_family(form_model: str) -> str:
    token = (form_model or "").strip().upper().replace(" ", "")
    if not token:
        return ""
    if token.startswith("HG") or token.startswith("HN"):
        return "ont"
    if token.startswith("CG") or "CGNV" in token.replace("-", "") or token in {"KAON", "HITRON", "HISTRON"}:
        return "emta"
    return ""


def _cross_family_mismatch(form_model: str, found: dict) -> str:
    expected = _expected_family(form_model)
    if not expected:
        return ""
    kind = _dut_kind_from_reading(found)
    if not kind or kind == expected:
        return ""
    label = str(found.get("model") or found.get("brand") or kind).strip() or kind
    return (
        f"El portal devolvió {label} ({kind.upper()}), pero el perfil del banco indica "
        f"{form_model} ({expected.upper()}). Usa la LAN del banco (Remapear LAN · Buscar), "
        "no la ruta de gestión de la PC."
    )


def portal_cables_for_macs(macs: set[str], portal_host: str = "") -> list[dict[str, str]]:
    cables = [item for item in dut_paths(macs) if allowed_host(item.get("gateway", ""))]
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
    return targets or cables


def _ethtool_linked(iface: str) -> bool:
    import subprocess

    try:
        result = subprocess.run(
            ["ethtool", iface],
            capture_output=True,
            text=True,
            timeout=4,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    for line in (result.stdout or "").splitlines():
        if "Link detected" in line:
            return line.rsplit(":", 1)[-1].strip().lower() == "yes"
    return False


def _route_cable_to_portal(host: str) -> dict[str, str] | None:
    """When the ONT portal is reachable only via the station default route (e.g. ens8f3)."""
    import re
    import subprocess
    from pathlib import Path

    if not allowed_host(host):
        return None
    try:
        result = subprocess.run(
            ["ip", "route", "get", host],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    line = (result.stdout or "").strip().splitlines()
    if not line:
        return None
    text = line[0]
    device = re.search(r"\bdev\s+(\S+)", text)
    source = re.search(r"\bsrc\s+(\S+)", text)
    if not device:
        return None
    iface = device.group(1)
    if (Path("/sys/class/net") / iface / "wireless").exists():
        return None
    if not _ethtool_linked(iface) and (Path("/sys/class/net") / iface / "carrier").read_text(encoding="utf-8").strip() != "1":
        return None
    src = source.group(1) if source else ""
    return {"iface": iface, "source": src, "gateway": host}


def _misplaced_ont_hint(macs: set[str]) -> str:
    """ONT cabled to the lab/management NIC instead of LAN5–LAN8."""
    from pathlib import Path

    wanted = {mac.lower() for mac in macs if mac}
    net = Path("/sys/class/net")
    linked_elsewhere: list[str] = []
    for entry in sorted(net.iterdir()):
        if not entry.is_dir():
            continue
        iface = entry.name
        if (entry / "wireless").exists():
            continue
        try:
            mac = (entry / "address").read_text(encoding="utf-8").strip().lower()
        except OSError:
            continue
        if mac in wanted:
            continue
        if _ethtool_linked(iface) or (entry / "carrier").read_text(encoding="utf-8").strip() == "1":
            linked_elsewhere.append(iface)
    if not linked_elsewhere:
        return ""
    names = ", ".join(linked_elsewhere[:3])
    return (
        f" Hay enlace en {names} (no es LAN del banco); "
        "conecta el módem solo a los puertos LAN1–LAN8 del banco."
    )


def _row_has_link(row: dict[str, str]) -> bool:
    """Some drivers keep sysfs carrier at 0 while operstate or DHCP already show a live ONT."""
    iface = str(row.get("iface") or "")
    if iface and _ethtool_linked(iface):
        return True
    if str(row.get("carrier") or "").strip() == "1":
        return True
    source = str(row.get("source") or "").strip()
    if source.startswith("192.168."):
        return True
    oper = str(row.get("operstate") or "").lower()
    return oper in {"up", "unknown"}


def _linked_nics(macs: set[str]) -> list[dict[str, str]]:
    """Bench NICs that are present with carrier (even before DHCP completes)."""
    import json as _json
    import subprocess
    from pathlib import Path

    wanted = {mac.lower() for mac in macs if mac}
    if not wanted:
        return []
    try:
        raw = subprocess.run(
            ["ip", "-4", "-j", "addr"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        ).stdout
        records = _json.loads(raw or "[]")
    except (OSError, subprocess.SubprocessError, _json.JSONDecodeError):
        records = []
    by_iface: dict[str, str] = {}
    for record in records:
        iface = str(record.get("ifname") or "")
        for item in record.get("addr_info") or []:
            address = str(item.get("local") or "")
            if address:
                by_iface[iface] = address
    net = Path("/sys/class/net")
    rows: list[dict[str, str]] = []
    for entry in sorted(net.iterdir()):
        if not entry.is_dir():
            continue
        iface = entry.name
        if (entry / "wireless").exists() or (entry / "phy80211").exists():
            continue
        try:
            mac = (entry / "address").read_text(encoding="utf-8").strip().lower()
            carrier = (entry / "carrier").read_text(encoding="utf-8").strip()
            operstate = (entry / "operstate").read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if mac not in wanted:
            continue
        rows.append(
            {
                "iface": iface,
                "mac": mac,
                "carrier": carrier,
                "operstate": operstate,
                "source": by_iface.get(iface, ""),
            }
        )
    return rows


def _gateway_for_scrape(source: str, portal_host: str) -> str:
    """Gateway used by the portal scraper when DHCP did not publish a default route."""
    host = portal_host.strip()
    if allowed_host(host):
        return host
    address = (source or "").strip()
    if not address.startswith("192.168."):
        return ""
    parts = address.split(".")
    if len(parts) == 4:
        return f"{parts[0]}.{parts[1]}.{parts[2]}.1"
    return "192.168.1.1"


def _sort_bench_cables(cables: list[dict[str, str]]) -> list[dict[str, str]]:
    """Try ONT-facing NICs before the station uplink (.221) when several LANs share a subnet."""

    def weight(row: dict[str, str]) -> tuple[int, str]:
        source = str(row.get("source") or "")
        if source.endswith(".221") or source.endswith(".222"):
            return (2, row.get("iface") or "")
        if source.startswith("192.168."):
            return (0, row.get("iface") or "")
        return (1, row.get("iface") or "")

    return sorted(cables, key=weight)


def _bench_static_portal_paths(macs: set[str], portal_host: str) -> list[dict[str, str]]:
    """CG/EMTA at 192.168.1.1 without DHCP: scrape bound to the bench NIC."""
    host = portal_host.strip()
    if not allowed_host(host):
        return []
    paths: list[dict[str, str]] = []
    for row in _linked_nics(macs):
        iface = str(row.get("iface") or "")
        if not iface:
            continue
        source = str(row.get("source") or "").strip()
        carrier = str(row.get("carrier") or "").strip() == "1"
        oper = str(row.get("operstate") or "").lower()
        up = oper in {"up", "unknown"} or carrier or _ethtool_linked(iface) or source
        if not up:
            continue
        paths.append({"iface": iface, "source": source, "gateway": host})
    if paths:
        return paths
    for row in _linked_nics(macs):
        iface = str(row.get("iface") or "")
        if iface:
            paths.append(
                {
                    "iface": iface,
                    "source": str(row.get("source") or "").strip(),
                    "gateway": host,
                }
            )
    return paths


def resolve_bench_cables(macs: set[str], portal_host: str) -> tuple[list[dict[str, str]], str]:
    """DHCP paths first; then portal IP manual on linked NICs; then inferred gateway for scrape."""
    cables = portal_cables_for_macs(macs, portal_host)
    if cables:
        return cables, ""
    host = portal_host.strip()
    linked = _linked_nics(macs)
    if not linked:
        return (
            [],
            "No hay adaptador de certificación mapeado en este banco. Pulsa Remapear LAN y vuelve a Buscar.",
        )
    if allowed_host(host):
        out = []
        for row in linked:
            if not _row_has_link(row):
                continue
            source = row.get("source") or ""
            out.append({"iface": row["iface"], "source": source, "gateway": host})
        if out:
            return out, ""
    live = [row for row in linked if _row_has_link(row)]
    names = ", ".join(row["iface"] for row in live) or "—"
    if not live:
        names = ", ".join(row["iface"] for row in linked) or "—"
        static = _bench_static_portal_paths(macs, host)
        if static:
            return static, ""
        extra = _misplaced_ont_hint(macs)
        return (
            [],
            "Sin carrier en " + names + ". Cable del módem a LAN del banco, Remapear LAN y Buscar."
            + extra,
        )
    with_ip = [row for row in live if row.get("source")]
    scrape_paths: list[dict[str, str]] = []
    for row in with_ip:
        gateway = _gateway_for_scrape(str(row.get("source") or ""), host)
        if not allowed_host(gateway):
            continue
        scrape_paths.append(
            {
                "iface": row["iface"],
                "source": row.get("source") or "",
                "gateway": gateway,
            }
        )
    if scrape_paths:
        return scrape_paths, ""
    static = _bench_static_portal_paths(macs, host)
    if static:
        return static, ""
    if not with_ip:
        return (
            [],
            "Hay enlace (" + names + ") pero el módem aún no entregó DHCP. "
            "Indica Portal IP 192.168.1.1 en la tarjeta y pulsa Buscar, o Remapear LAN del banco.",
        )
    return (
        [],
        "Ningún puerto de este banco tiene enlace útil ni DHCP. "
        "Conecta el CG por Ethernet a LAN del banco, Remapear LAN y Buscar de nuevo.",
    )


def _run_order_hint(config: AppConfig, serial: str) -> tuple[str, bool]:
    if not serial:
        return "", False
    reused = order_for_serial(runs_directory(config), serial)
    if reused is not None:
        return reused[0], True
    return "", False


def _filter_bench_cables(config: AppConfig, cables: list[dict[str, str]], bench_macs: set[str]) -> list[dict[str, str]]:
    return [row for row in cables if cable_on_lab_plane(config, row, bench_macs=bench_macs)]


def discover_one_bench(
    config: AppConfig,
    bench_id: str,
    *,
    dut_user: str,
    dut_password: str,
    portal_host: str,
    form_model: str,
    portals: dict,
) -> dict:
    blocked = management_blocked_macs(config)
    macs = {
        mac.lower()
        for mac in bench_ports(config, bench_id).values()
        if mac and mac.lower() not in blocked
    }
    if not macs:
        hint = (
            "Conecta 4 adaptadores Ethernet más y pulsa Remapear LAN."
            if bench_id.upper() == "B"
            else "No hay NIC de certificación en LAN1–LAN4. Revisa cables o Remapear LAN."
        )
        return {
            "bench_id": bench_id.upper(),
            "ok": False,
            "error": f"Banco {bench_id}: sin adaptador de PC en este banco. {hint}",
        }
    if not _linked_nics(macs):
        leftovers = _leftover_lab_namespaces()
        if leftovers:
            names = ", ".join(leftovers[:6])
            return {
                "bench_id": bench_id.upper(),
                "ok": False,
                "error": (
                    f"Banco {bench_id}: las NIC del laboratorio no están en el host (namespaces {names}). "
                    "Pulsa Recuperar namespaces, espera 5 s y vuelve a Buscar."
                ),
            }
    host = portal_host.strip() or "192.168.1.1"
    cables, cable_error = resolve_bench_cables(macs, host)
    cables = _filter_bench_cables(config, _sort_bench_cables(cables), macs)
    if not cables and allowed_host(host):
        cables = _filter_bench_cables(config, _bench_static_portal_paths(macs, host), macs)
    if not cables:
        route = _route_cable_to_portal(host)
        route_mac = iface_mac(str(route.get("iface") or "")) if route else ""
        if route and route_mac and route_mac in blocked:
            return {
                "bench_id": bench_id.upper(),
                "ok": False,
                "error": (
                    f"Solo la NIC de administración ({route['iface']}) alcanza {host}. "
                    "No se usa para el scraper. Cable del CG a LAN del banco "
                    + ("LAN1–LAN4" if bench_id.upper() == "A" else "LAN5–LAN8")
                    + ", enlace activo, Remapear LAN y Buscar."
                ),
            }
    if not cables:
        return {
            "bench_id": bench_id.upper(),
            "ok": False,
            "error": cable_error or "Ningún puerto de este banco tiene puerta de enlace DHCP.",
        }
    found: dict = {"ok": False, "error": "El portal no respondió por el puerto de prueba."}
    if valid_user(dut_user) and valid_password(dut_password):
        for cable in cables:
            found = read_portal(
                cable["gateway"],
                dut_user,
                dut_password,
                portals,
                source=cable["source"],
                device=cable["iface"],
                form_model=form_model,
                inventory=True,
            )
            found.setdefault("gateway", cable.get("gateway", ""))
            found.setdefault("iface", cable.get("iface", ""))
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
    found["bench_id"] = bench_id.upper()
    if found.get("ok"):
        kind = _dut_kind_from_reading(found)
        if kind:
            found["family"] = kind
        mismatch = _cross_family_mismatch(form_model, found)
        if mismatch:
            found["ok"] = False
            found["error"] = mismatch
        else:
            found["error"] = ""
        iface = str(found.get("iface") or cables[0].get("iface") or "")
        macs_set = {mac.lower() for mac in macs if mac}
        route_mac = iface_mac(str(cables[0].get("iface") or ""))
        if found.get("ok") and route_mac and route_mac in management_blocked_macs(config):
            found["ok"] = False
            found["error"] = (
                f"El portal se leyó por {iface} (administración/Internet). "
                "Esa NIC no pertenece al banco de certificación."
            )
        if found.get("ok") and route_mac and route_mac not in macs_set:
            found["ok"] = False
            found["error"] = (
                f"El portal respondió por {iface}, que no pertenece a este banco. "
                "Conecta el módem a una LAN del banco (no al uplink de gestión) y pulsa Buscar."
            )
        if found.get("ok"):
            serial = str(found.get("serial") or "")
            run_id, reused = _run_order_hint(config, serial)
            found["run_id_hint"] = run_id
            found["run_reused"] = reused
            store_bench(bench_id, found)
    return found


def _bench_credentials(
    bench_id: str,
    bench_credentials: dict | None,
    *,
    dut_user: str,
    dut_password: str,
    portal_host: str,
    form_model: str,
) -> tuple[str, str, str, str]:
    block: dict = {}
    if isinstance(bench_credentials, dict):
        raw = bench_credentials.get(bench_id.upper())
        if isinstance(raw, dict):
            block = raw
    user = str(block.get("dut_user") or dut_user).strip()
    password = block.get("dut_password") if isinstance(block.get("dut_password"), str) else dut_password
    host = str(block.get("portal_host") or portal_host).strip()
    model = str(block.get("portal_model") or form_model).strip()
    return user, password, host, model


def discover_benches(
    config_path: Path,
    *,
    scope: str,
    dut_user: str,
    dut_password: str,
    portal_host: str,
    form_model: str,
    bench_credentials: dict | None = None,
) -> dict:
    config, _path = load_config(config_path)
    from tc_net_cert.core.executor import SystemExecutor
    from tc_net_cert.network.management import refresh_portal_links

    executor = SystemExecutor()
    recovery_notes = ensure_bench_nics_in_root(config, executor)
    portals = load_portals(portals_path())
    benches = resolve_benches(config)
    ids = list(benches.keys())
    if scope.upper() in benches:
        ids = [scope.upper()]
    blocked = management_blocked_macs(config)
    refresh_map: dict[str, str] = {}
    for bench_id in ids:
        for name, mac in bench_ports(config, bench_id).items():
            if mac and mac.lower() not in blocked:
                refresh_map[name] = mac
    if refresh_map:
        try:
            refresh_portal_links(executor, refresh_map)
        except Exception:
            pass
    results = []
    for bench_id in ids:
        user, password, host, model = _bench_credentials(
            bench_id,
            bench_credentials,
            dut_user=dut_user,
            dut_password=dut_password,
            portal_host=portal_host,
            form_model=form_model,
        )
        results.append(
            discover_one_bench(
                config,
                bench_id,
                dut_user=user,
                dut_password=password,
                portal_host=host,
                form_model=model,
                portals=portals,
            )
        )
    ok = any(item.get("ok") for item in results)
    payload: dict = {
        "ok": ok,
        "benches": results,
        "management_note": "La LAN nativa (Internet/sync BD) no se usa para leer el portal.",
    }
    if recovery_notes:
        payload["recovery"] = recovery_notes
    return payload
