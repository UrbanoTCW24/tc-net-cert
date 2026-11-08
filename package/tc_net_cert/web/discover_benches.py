"""Portal discovery scoped to bench A/B."""

from __future__ import annotations

from pathlib import Path

from tc_net_cert.config.benches import bench_ports, resolve_benches
from tc_net_cert.config.loader import load_config, runs_directory
from tc_net_cert.core.models import AppConfig
from tc_net_cert.dut.huawei import allowed_host, discover_portal, dut_paths, portal_retryable, read_portal, valid_password, valid_user
from tc_net_cert.dut.portals import load_portals, portals_path
from tc_net_cert.storage.bench_identity import store_bench
from tc_net_cert.storage.runs import order_for_serial


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
        except OSError:
            continue
        if mac not in wanted:
            continue
        rows.append(
            {
                "iface": iface,
                "mac": mac,
                "carrier": carrier,
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
            "Ninguna NIC de este banco tiene enlace físico. Conecta el cable al ONT y pulsa Reconfigurar LAN.",
        )
    if allowed_host(host):
        out = []
        for row in linked:
            if row.get("carrier") != "1":
                continue
            source = row.get("source") or ""
            out.append({"iface": row["iface"], "source": source, "gateway": host})
        if out:
            return out, ""
    live = [row for row in linked if row.get("carrier") == "1"]
    names = ", ".join(row["iface"] for row in live) or "—"
    if not live:
        return (
            [],
            "Ninguna LAN de este banco tiene cable con enlace. Conecta el ONT, pulsa Reconfigurar LAN "
            "del banco y vuelve a Buscar (solo ese banco).",
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
    if not with_ip:
        return (
            [],
            "Hay enlace (" + names + ") pero el ONT aún no entregó DHCP. "
            "Pulsa Reconfigurar LAN del banco, espera 10 s e indica Portal IP (ej. 192.168.1.1).",
        )
    return (
        [],
        "Ningún puerto de este banco tiene puerta de enlace DHCP. "
        "Reconfigura LAN del banco o escribe Portal IP en la tarjeta y vuelve a buscar.",
    )


def _run_order_hint(config: AppConfig, serial: str) -> tuple[str, bool]:
    if not serial:
        return "", False
    reused = order_for_serial(runs_directory(config), serial)
    if reused is not None:
        return reused[0], True
    return "", False


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
    macs = set(bench_ports(config, bench_id).values())
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
    cables, cable_error = resolve_bench_cables(macs, portal_host)
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

    portals = load_portals(portals_path())
    benches = resolve_benches(config)
    ids = list(benches.keys())
    if scope.upper() in benches:
        ids = [scope.upper()]
    refresh_map: dict[str, str] = {}
    for bench_id in ids:
        refresh_map.update(bench_ports(config, bench_id))
    if refresh_map:
        try:
            refresh_portal_links(SystemExecutor(), refresh_map)
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
    return {
        "ok": ok,
        "benches": results,
        "management_note": "La LAN nativa (Internet/sync BD) no se usa para leer el portal.",
    }
