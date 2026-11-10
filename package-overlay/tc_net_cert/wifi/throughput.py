"""Connect to the DUT SSIDs and measure iperf3, then restore the station Wi-Fi.

The radio stays on the host. It never moves into a namespace and never becomes
the default route. Keys stay out of argv and the command log. The certificate
prints the key that was read from the portal.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import time
import uuid
from pathlib import Path

from tc_net_cert.core.models import Endpoint, FlowResult, Verdict
from tc_net_cert.iperf.parser import parse_iperf_json

SSID_24 = "TC_NET_CERT_WIFI_SSID_24"
PSK_24 = "TC_NET_CERT_WIFI_PSK_24"
SSID_50 = "TC_NET_CERT_WIFI_SSID_50"
PSK_50 = "TC_NET_CERT_WIFI_PSK_50"
READING_ENV = "TC_NET_CERT_WIFI_READING"
SECONDS = 60
NM_PROFILES = Path("/run/NetworkManager/system-connections")
_IFACE = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{0,15}$")
_IPV4 = re.compile(r"^(?:\d{1,3}\.){3}\d{1,3}$")


def reading_path() -> Path:
    return Path.home() / ".local" / "state" / "tc-net-cert" / "wifi-reading.json"


def _pair(ssid: object, psk: object) -> tuple[str, str] | None:
    name = str(ssid or "").strip()
    secret = str(psk or "").strip()
    if (
        not name
        or not secret
        or len(name) > 32
        or not 8 <= len(secret) <= 63
        or "\n" in name
        or "\n" in secret
        or "\x00" in name
        or "\x00" in secret
    ):
        return None
    return name, secret


def store_reading(found: dict, path: Path | None = None) -> bool:
    """Remember the last portal Wi-Fi reading. Keys stay out of the log and the report."""
    pairs = {}
    first = _pair(found.get("ssid_24"), found.get("wifi_password_24"))
    second = _pair(found.get("ssid_50"), found.get("wifi_password_50"))
    if first:
        pairs["24"] = {"ssid": first[0], "psk": first[1]}
    if second:
        pairs["50"] = {"ssid": second[0], "psk": second[1]}
    if not pairs:
        return False
    file = path or reading_path()
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(json.dumps(pairs), encoding="utf-8")
    file.chmod(0o600)
    return True


def stored_env(path: Path | None = None) -> dict[str, str]:
    file = path or reading_path()
    try:
        payload = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    found = {}
    mapping = {"24": (SSID_24, PSK_24), "50": (SSID_50, PSK_50)}
    for band, (ssid_key, psk_key) in mapping.items():
        item = payload.get(band)
        if not isinstance(item, dict):
            continue
        pair = _pair(item.get("ssid"), item.get("psk"))
        if pair is None:
            continue
        found[ssid_key] = pair[0]
        found[psk_key] = pair[1]
    return found


def take_credentials() -> list[tuple[str, str, str]]:
    """Read the two bands from the environment and drop them from this process."""
    reading = os.environ.pop(READING_ENV, "").strip()
    if reading:
        for key, value in stored_env(Path(reading)).items():
            os.environ.setdefault(key, value)
    bands = [
        ("50", os.environ.pop(SSID_50, ""), os.environ.pop(PSK_50, "")),
        ("24", os.environ.pop(SSID_24, ""), os.environ.pop(PSK_24, "")),
    ]
    ready = []
    for band, ssid, psk in bands:
        pair = _pair(ssid, psk)
        if pair is not None:
            ready.append((band, pair[0], pair[1]))
    return ready


def credentials_from_reading(reading: Path | None) -> list[tuple[str, str, str]]:
    """SSID/PSK for both bands from a bench-specific wifi-reading file (no env mutation)."""
    env: dict[str, str] = {}
    bench_file = reading is not None and reading.is_file()
    if bench_file:
        env.update(stored_env(reading))
    else:
        path = os.environ.get(READING_ENV, "").strip()
        if path:
            env.update(stored_env(Path(path)))
        for key in (SSID_50, PSK_50, SSID_24, PSK_24):
            override = os.environ.get(key, "").strip()
            if override:
                env[key] = override
    ready: list[tuple[str, str, str]] = []
    for band, ssid_key, psk_key in (("50", SSID_50, PSK_50), ("24", SSID_24, PSK_24)):
        pair = _pair(env.get(ssid_key), env.get(psk_key))
        if pair is not None:
            ready.append((band, pair[0], pair[1]))
    return ready


def choose_lan(endpoints: list[Endpoint], bench_id: str = "") -> Endpoint | None:
    """Pick the DUT LAN port for Wi-Fi iperf: bank A uses LAN2/LAN3, bank B uses LAN6/LAN7."""
    ready = {
        item.logical_name: item
        for item in endpoints
        if item.address and not str(item.address).startswith("169.254.")
    }
    bench = (bench_id or os.environ.get("TC_NET_CERT_BENCH_ID", "")).strip().upper()
    if bench == "B":
        order = ("LAN6", "LAN7", "LAN5", "LAN8")
    else:
        order = ("LAN2", "LAN3", "LAN1", "LAN4")
    for name in order:
        if name in ready:
            return ready[name]
    for name in sorted(ready):
        return ready[name]
    return None


def _wake_radio(iface: str, notify) -> None:
    """Turn Wi-Fi on and attach the lab profile before joining the ONT SSID."""
    if not _IFACE.fullmatch(iface or ""):
        return
    _run(["nmcli", "radio", "wifi", "on"], 15)
    state = _run(["nmcli", "-g", "GENERAL.STATE", "device", "show", iface], 15)
    lowered = (state.stdout or "").lower()
    if "connected" not in lowered and "activating" not in lowered:
        link = _run(["nmcli", "device", "connect", iface], 45)
        if link.returncode != 0:
            detail = (link.stderr or link.stdout or "").strip().replace("\n", " ")[:140]
            notify(f"WIFI {iface} sin enlace de gestión ({detail or 'conecta el USB Wi‑Fi'})")


def measure_bands(
    endpoints: list[Endpoint],
    notify,
    audit=None,
    seconds: int = SECONDS,
    *,
    bench_id: str = "",
    reading: Path | None = None,
) -> list[FlowResult]:
    bands = credentials_from_reading(reading) if reading is not None else take_credentials()
    if not bands and reading is None:
        bands = credentials_from_reading(None)
    if not bands:
        notify("WIFI 5 y 2.4 no iniciaron: esta corrida no trae las claves leídas del portal.")
        return []
    bench = (bench_id or os.environ.get("TC_NET_CERT_BENCH_ID", "")).strip().upper() or "A"
    server = choose_lan(endpoints, bench)
    iface = _wifi_iface()
    if server is None or not iface:
        target = "LAN6 o LAN7" if bench == "B" else "LAN2 o LAN3"
        notify(f"WIFI 5 y 2.4 no iniciaron: falta {target} con IP del equipo, o el radio Wi-Fi.")
        return [
            _skipped(band, ssid, f"Wi-Fi no inició: falta {target} con IP, o el radio.", server.logical_name if server else "LAN2")
            for band, ssid, _psk in bands
        ]
    _wake_radio(iface, notify)
    radio = _prepare(iface)
    station = radio["ssid"] or "la red de la estación"
    notify(f"WIFI el radio deja {station} para medir 5 y después 2.4 contra {server.logical_name}")
    flows: list[FlowResult] = []
    try:
        for band, ssid, psk in bands:
            flows.append(_one_band(band, ssid, psk, radio, server, notify, audit, seconds))
    finally:
        _restore_radio(radio, notify, [psk for _band, _ssid, psk in bands])
    return flows


def _one_band(
    band: str,
    ssid: str,
    psk: str,
    radio: dict[str, str],
    server: Endpoint,
    notify,
    audit=None,
    seconds: int = SECONDS,
) -> FlowResult:
    label = "2.4" if band == "24" else "5"
    iface = radio["iface"]
    notify(f"WIFI {label} buscando {ssid} para medir contra {server.logical_name}")
    freqs = _wait_freqs(iface, ssid, 15)
    if not freqs:
        error = f"el radio no ve {ssid}"
    else:
        wanted = "bg" if band == "24" else "a"
        on_band = _on_band(freqs, wanted)
        if not on_band:
            notify(
                f"WIFI {label} {ssid} está visible en {min(freqs)} MHz. "
                "Se mide esa red aunque el anuncio no esté en la banda esperada."
            )
        error = _join(radio, ssid, psk, wanted if on_band else "")
    if error:
        notify(f"WIFI {label} NO INICIÓ: {error}")
        return _skipped(band, ssid, f"Wi-Fi {label} no inició: {error}", server.logical_name)
    if radio.get("mode") != "nmcli":
        _run(["ip", "-4", "addr", "flush", "dev", iface], 10)
        _run(["networkctl", "reconfigure", iface], 20)
    address = _wait_address(iface, 25)
    if not address:
        notify(f"WIFI {label} NO INICIÓ: {ssid} no entregó IP")
        return _skipped(band, ssid, f"Wi-Fi {label} se asoció a {ssid}, pero el equipo no entregó IP.", server.logical_name)
    notify(f"WIFI {label} {address} → {server.logical_name} {server.address}")
    return _iperf(band, ssid, address, server, radio, notify, audit, seconds)


def _iperf(band: str, ssid: str, address: str, server: Endpoint, radio: dict[str, str], notify, audit, seconds: int) -> FlowResult:
    label = "2.4" if band == "24" else "5"
    iface = radio["iface"]
    route = _route(server.address, address)
    notify(f"WIFI {label} ruta {route or 'sin ruta hacia el LAN'}")
    _pin(server.address, iface, address)
    try:
        upload = _host_client(band, ssid, address, iface, server, notify, audit, seconds)
        if upload is not None:
            return upload
        download = _lan_client(band, ssid, address, iface, server, notify, audit, seconds)
        if download is not None:
            return download
    finally:
        _unpin(server.address, iface)
    reason = _no_traffic(label, ssid, address, server, route)
    notify(f"WIFI {label} SIN MEDICION: {reason}")
    return _skipped(band, ssid, reason, server.logical_name)


def _host_client(band, ssid, address, iface, server, notify, audit, seconds: int) -> FlowResult | None:
    """Station radio sends to the iperf3 server already running in the LAN namespace."""
    label = "2.4" if band == "24" else "5"
    if not _is_ipv4(address) or not _is_ipv4(server.address) or not _IFACE.fullmatch(iface):
        return None
    command = _client_argv(server.address, address, iface, server.iperf_port, seconds)
    measured = _capture(command, f"WIFI-{label}", audit, seconds)
    if not _usable(measured):
        notify(f"WIFI {label} el radio no entregó tráfico hacia {server.logical_name}: {_short(measured)}")
        return None
    notify(f"WIFI {label} {measured.mbps_avg:.1f} Mbps")
    return _success(band, ssid, measured, address, server.address, f"WIFI-{label}", server.logical_name, seconds)


def _lan_client(band, ssid, address, iface, server, notify, audit, seconds: int) -> FlowResult | None:
    """LAN namespace sends to an iperf3 server bound to the station radio."""
    label = "2.4" if band == "24" else "5"
    if not server.namespace or not server.interface or not _IFACE.fullmatch(server.interface):
        return None
    if not _is_ipv4(address) or not _is_ipv4(server.address) or not _IFACE.fullmatch(iface):
        return None
    port = 5910 if band == "24" else 5911
    notify(f"WIFI {label} midiendo {server.logical_name} → radio")
    _pin(address, server.interface, server.address, server.namespace)
    process = None
    measured = None
    try:
        process = _serve(address, iface, port)
        command = [
            "ip",
            "netns",
            "exec",
            server.namespace,
            *_client_argv(address, server.address, server.interface, port, seconds),
        ]
        measured = _capture(command, f"WIFI-{label}-lan", audit, seconds)
    except OSError:
        measured = None
    finally:
        if process is not None:
            _stop(process)
        _unpin(address, server.interface, server.namespace)
    if measured is None or not _usable(measured):
        detail = _short(measured) if measured is not None else "no arrancó el servidor en el radio"
        notify(f"WIFI {label} {server.logical_name} tampoco entregó tráfico: {detail}")
        return None
    notify(f"WIFI {label} {measured.mbps_avg:.1f} Mbps")
    return _success(band, ssid, measured, server.address, address, f"WIFI-{label}-lan", server.logical_name, seconds)


def _success(
    band: str,
    ssid: str,
    measured,
    source_ip: str,
    destination_ip: str,
    artifact: str,
    lan: str,
    seconds: int,
) -> FlowResult:
    label = "2.4" if band == "24" else "5"
    return FlowResult(
        case_id=f"WIFI-{label}",
        kind="wifi",
        source=f"WIFI{label}",
        destination=lan,
        streams=4,
        requested_duration_s=float(seconds),
        actual_duration_s=measured.seconds,
        mbps_avg=measured.mbps_avg,
        mbps_min=measured.mbps_min,
        mbps_max=measured.mbps_max,
        retransmits=measured.retransmits,
        verdict=Verdict.PASS,
        assessment=ssid,
        raw_json=f"raw/iperf3/{artifact}.json",
        source_ip=source_ip,
        destination_ip=destination_ip,
    )


def _client_argv(destination: str, source: str, iface: str, port: int, seconds: int) -> list[str]:
    return [
        "iperf3",
        "-c",
        destination,
        "-B",
        source,
        "--bind-dev",
        iface,
        "-p",
        str(port),
        "-t",
        str(seconds),
        "-P",
        "4",
        "-J",
        "--forceflush",
        "--connect-timeout",
        "8000",
    ]


def _capture(command: list[str], name: str, audit, seconds: int = SECONDS):
    from tc_net_cert.iperf.runner import capture_command

    stdout, stderr, _code = capture_command(command, seconds + 20)
    if audit is not None:
        try:
            audit.write_raw(f"iperf3/{name}.json", f"{stdout or ''}\n{stderr or ''}")
        except Exception:
            pass
    return parse_iperf_json(stdout or stderr)


def _usable(measured) -> bool:
    return bool(
        measured.ok
        and measured.mbps_avg is not None
        and measured.mbps_avg > 0
        and measured.seconds is not None
        and measured.seconds >= 1
    )


def _short(measured) -> str:
    text = (measured.error or "iperf3 transferred 0 bytes").replace("\n", " ")
    return text[:160]


def _no_traffic(label: str, ssid: str, address: str, server: Endpoint, route: str) -> str:
    if _carrier(server) is False:
        return f"Se cortó {server.logical_name}. Por eso Wi-Fi {label} no midió."
    where = route or "sin ruta hacia el LAN"
    return (
        f"Wi-Fi {label} se asoció a {ssid} con {address}, pero no hubo tráfico "
        f"contra {server.logical_name} {server.address}. Ruta: {where}. "
        f"El equipo no pasó datos entre el Wi-Fi y ese puerto."
    )


def _is_ipv4(value: str) -> bool:
    return bool(_IPV4.fullmatch(value or ""))


def _route(destination: str, source: str) -> str:
    if not _is_ipv4(destination) or not _is_ipv4(source):
        return ""
    completed = _run(["ip", "route", "get", destination, "from", source], 8)
    return " ".join((completed.stdout or completed.stderr or "").split())[:160]


def _pin(destination: str, iface: str, source: str, namespace: str = "") -> None:
    if not _is_ipv4(destination) or not _is_ipv4(source) or not _IFACE.fullmatch(iface or ""):
        return
    command = ["ip", "route", "replace", f"{destination}/32", "dev", iface, "src", source]
    if namespace:
        command = ["ip", "netns", "exec", namespace, *command]
    _run(command, 8)


def _unpin(destination: str, iface: str, namespace: str = "") -> None:
    if not _is_ipv4(destination) or not _IFACE.fullmatch(iface or ""):
        return
    command = ["ip", "route", "del", f"{destination}/32", "dev", iface]
    if namespace:
        command = ["ip", "netns", "exec", namespace, *command]
    _run(command, 8)


def _serve(address: str, iface: str, port: int) -> subprocess.Popen:
    process = subprocess.Popen(
        ["iperf3", "-s", "-1", "-B", address, "--bind-dev", iface, "-p", str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    time.sleep(0.3)
    return process


def _stop(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()


def _skipped(band: str, ssid: str, reason: str, lan: str = "LAN2") -> FlowResult:
    label = "2.4" if band == "24" else "5"
    return FlowResult(
        case_id=f"WIFI-{label}",
        kind="wifi",
        source=f"WIFI{label}",
        destination=lan,
        requested_duration_s=float(SECONDS),
        mbps_avg=None,
        verdict=Verdict.SKIPPED,
        reasons=[reason],
        assessment=ssid,
    )


def _profile_body(name: str, iface: str, ssid: str, psk: str, profile_id: str, band_limit: str = "") -> str:
    wifi = [
        "[wifi]",
        f"ssid={_ini(ssid)}",
        "mode=infrastructure",
    ]
    if band_limit in {"bg", "a"}:
        wifi.append(f"band={band_limit}")
    return "\n".join(
        [
            "[connection]",
            f"id={name}",
            f"uuid={profile_id}",
            "type=wifi",
            f"interface-name={iface}",
            "autoconnect=false",
            "",
            *wifi,
            "",
            "[wifi-security]",
            "key-mgmt=wpa-psk",
            f"psk={_ini(psk)}",
            "psk-flags=0",
            "",
            "[ipv4]",
            "method=auto",
            "never-default=true",
            "route-metric=40000",
            "",
            "[ipv6]",
            "method=disabled",
            "",
        ]
    )


def _keyfile(name: str, iface: str, ssid: str, psk: str, band_limit: str) -> Path:
    NM_PROFILES.mkdir(parents=True, exist_ok=True)
    path = NM_PROFILES / f"{name}.nmconnection"
    path.write_text(
        _profile_body(name, iface, ssid, psk, str(uuid.uuid4()), band_limit),
        encoding="utf-8",
    )
    path.chmod(0o600)
    return path


def _ini(value: str) -> str:
    """NetworkManager keeps quotation marks as part of the SSID. Escape instead."""
    escaped: list[str] = []
    for char in value:
        if char == "\\":
            escaped.append("\\\\")
        elif char == " ":
            escaped.append("\\s")
        elif char == '"':
            escaped.append('\\"')
        elif char == "\n":
            escaped.append("\\n")
        else:
            escaped.append(char)
    return "".join(escaped)


def _wifi_iface() -> str:
    root = Path("/sys/class/net")
    if not root.is_dir():
        return ""
    for item in sorted(root.iterdir()):
        if (item / "wireless").exists() or (item / "phy80211").exists():
            name = item.name
            if _IFACE.fullmatch(name):
                return name
    return ""


def _active_wifi(iface: str) -> str:
    completed = _run(["nmcli", "-t", "-f", "UUID,TYPE,DEVICE", "connection", "show", "--active"], 15)
    for line in (completed.stdout or "").splitlines():
        parts = line.split(":")
        if len(parts) >= 3 and parts[1] == "802-11-wireless" and parts[2] == iface:
            return parts[0]
    return ""


def _wait_address(iface: str, seconds: int) -> str:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        address = _ipv4(iface)
        if address:
            return address
        time.sleep(1)
    return ""


def _ipv4(iface: str) -> str:
    completed = _run(["ip", "-4", "-o", "addr", "show", "dev", iface], 10)
    for line in (completed.stdout or "").splitlines():
        pieces = line.split()
        if "inet" in pieces:
            return pieces[pieces.index("inet") + 1].split("/")[0]
    return ""


def _prepare(iface: str) -> dict[str, str]:
    conf = Path(f"/run/netplan/wpa-{iface}.conf")
    unit = f"netplan-wpa-{iface}.service"
    radio = {"iface": iface, "ssid": _linked_ssid(iface), "mode": "", "unit": unit, "backup": ""}
    active = _run(["systemctl", "is-active", "--quiet", unit], 15)
    if conf.is_file() and active.returncode == 0:
        folder = Path("/run/tc-net-cert")
        folder.mkdir(parents=True, exist_ok=True)
        backup = folder / f"station-{iface}.wpa"
        shutil.copyfile(conf, backup)
        backup.chmod(0o600)
        radio["mode"] = "netplan"
        radio["backup"] = str(backup)
        return radio
    radio["mode"] = "nmcli"
    radio["nm"] = _active_wifi(iface)
    return radio


def _join(radio: dict[str, str], ssid: str, psk: str, band_limit: str) -> str:
    iface = radio["iface"]
    if radio.get("mode") == "netplan":
        conf = Path(f"/run/netplan/wpa-{iface}.conf")
        conf.write_text(_wpa_conf(ssid, psk), encoding="utf-8")
        conf.chmod(0o600)
        restarted = _run(["systemctl", "restart", radio["unit"]], 25)
        if restarted.returncode != 0:
            return "no se pudo cambiar el radio de la estación"
        if not _wait_ssid(iface, ssid, 25):
            current = _linked_ssid(iface)
            extra = f" Sigue en {current}." if current else ""
            return f"no se asoció a {ssid}.{extra}"
        return ""
    name = "tc-ont-wifi"
    _run(["nmcli", "connection", "delete", name], 15)
    _run(["nmcli", "device", "disconnect", iface], 15)
    path = _keyfile(name, iface, ssid, psk, band_limit)
    radio["keyfile"] = str(path)
    loaded = _run(["nmcli", "connection", "load", str(path)], 20)
    if loaded.returncode != 0:
        detail = _public(loaded.stderr or loaded.stdout or "", [psk])
        return "NetworkManager no cargó el perfil Wi-Fi" + (f" ({detail})" if detail else "")
    up = _run(["nmcli", "connection", "up", name, "ifname", iface], 45)
    if up.returncode != 0 and "Secrets were required" in (up.stderr or up.stdout or ""):
        _run(["nmcli", "connection", "load", str(path)], 20)
        up = _run(["nmcli", "connection", "up", name, "ifname", iface], 45)
    if up.returncode != 0:
        detail = _public(up.stderr or up.stdout or "", [psk])
        return f"no se asoció a {ssid}" + (f" ({detail})" if detail else "")
    return ""


def parse_scan_line(line: str) -> tuple[str, int] | None:
    text = (line or "").strip()
    if not text or text == "SSID:FREQ" or ":" not in text:
        return None
    ssid, freq = text.rsplit(":", 1)
    ssid = ssid.replace("\\:", ":").strip()
    match = re.search(r"\d+", freq)
    if not ssid or match is None:
        return None
    return ssid, int(match.group(0))


def _on_band(freqs: list[int], wanted: str) -> bool:
    if wanted == "a":
        return any(freq >= 5000 for freq in freqs)
    if wanted == "bg":
        return any(2000 <= freq < 3000 for freq in freqs)
    return False


def _wait_freqs(iface: str, ssid: str, seconds: int) -> list[int]:
    if not _IFACE.fullmatch(iface or ""):
        return []
    _run(["nmcli", "device", "wifi", "rescan", "ifname", iface], 20)
    deadline = time.monotonic() + seconds
    found: list[int] = []
    while time.monotonic() < deadline:
        found = _freqs(iface, ssid)
        if found:
            return found
        time.sleep(1)
    return _freqs(iface, ssid)


def _freqs(iface: str, ssid: str) -> list[int]:
    completed = _run(["nmcli", "-t", "-f", "SSID,FREQ", "device", "wifi", "list", "ifname", iface], 15)
    found = []
    for line in (completed.stdout or "").splitlines():
        item = parse_scan_line(line)
        if item is not None and item[0] == ssid:
            found.append(item[1])
    return found


def _restore_radio(radio: dict[str, str], notify, secrets: list[str]) -> None:
    iface = radio.get("iface") or ""
    keyfile = radio.get("keyfile") or ""
    if keyfile:
        Path(keyfile).unlink(missing_ok=True)
        _run(["nmcli", "connection", "delete", "tc-ont-wifi"], 15)
    backup = Path(radio["backup"]) if radio.get("backup") else None
    if radio.get("mode") == "netplan" and backup is not None and backup.is_file():
        conf = Path(f"/run/netplan/wpa-{iface}.conf")
        shutil.copyfile(backup, conf)
        conf.chmod(0o600)
        restored = _run(["systemctl", "restart", radio["unit"]], 25)
        backup.unlink(missing_ok=True)
        if restored.returncode != 0:
            notify(f"WIFI {iface} no volvió a la red de la estación.")
            return
        if radio.get("ssid"):
            _wait_ssid(iface, radio["ssid"], 25)
        _run(["networkctl", "reconfigure", iface], 20)
        notify(f"WIFI {iface} volvió a {radio.get('ssid') or 'la red de la estación'}")
        return
    previous = radio.get("nm") or ""
    if previous:
        restored = _run(["nmcli", "connection", "up", "uuid", previous], 40)
        if restored.returncode == 0:
            notify(f"WIFI {iface} restaurado a la red de la estación")
            return
        notify(f"WIFI {iface} no volvió a la red anterior: {_public(restored.stderr, secrets)}")
        return
    if iface:
        notify(f"WIFI {iface} quedó sin la red anterior. Vuelve a conectar la estación.")


def _wpa_conf(ssid: str, psk: str) -> str:
    return "\n".join(
        [
            "ctrl_interface=/run/wpa_supplicant",
            "update_config=0",
            "network={",
            f"    ssid={_wpa_quote(ssid)}",
            f"    psk={_wpa_quote(psk)}",
            "    key_mgmt=WPA-PSK",
            "    scan_ssid=1",
            "}",
            "",
        ]
    )


def _wpa_quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _linked_ssid(iface: str) -> str:
    completed = _run(["iw", "dev", iface, "link"], 10)
    for line in (completed.stdout or "").splitlines():
        if "SSID:" in line:
            return line.split("SSID:", 1)[1].strip()
    return ""


def _wait_ssid(iface: str, ssid: str, seconds: int) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if _linked_ssid(iface) == ssid:
            return True
        time.sleep(1)
    return _linked_ssid(iface) == ssid


def _carrier(endpoint: Endpoint) -> bool | None:
    completed = _run(
        ["ip", "netns", "exec", endpoint.namespace, "cat", f"/sys/class/net/{endpoint.interface}/carrier"],
        8,
    )
    text = (completed.stdout or "").strip()
    if text == "1":
        return True
    if text == "0":
        return False
    return None


def _public(text: str, secrets: list[str]) -> str:
    cleaned = text or ""
    for secret in secrets:
        if secret:
            cleaned = cleaned.replace(secret, "****")
    return " ".join(cleaned.split())[:160]


def _run(command: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
