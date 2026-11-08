"""Live iperf3 countdown and per-interface send/receive rates.

The panel is not root. It reads process command lines and the network
counters of those processes, including NICs that the test moved into a
namespace.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path

from tc_net_cert.orchestration.guard import traffic_led
from tc_net_cert.storage.runs import list_runs

_FLOW_RE = re.compile(r"(LAN\d+)\s+→\s+(LAN\d+)")
_PORT_RE = re.compile(r"PORT (LAN\d+) (.+)")
_ROLE_RE = re.compile(r"ROLES TX ([A-Z0-9,]+) RX ([A-Z0-9,]+) WINDOW (\S+)")
_STATE_RE = re.compile(r"STATE (LAN\d+) ([A-Z0-9_]+) ([A-Z0-9_]+)(?: (.*))?")
_IFACE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{0,15}$")
_PROC = Path("/proc")
_NET = Path("/sys/class/net")


class TrafficMonitor:
    def __init__(self) -> None:
        self._previous: dict[str, tuple[float, int, int]] = {}
        self._shown: dict[str, dict] = {}
        self._quiet: dict[str, float] = {}
        self._window = ""
        self._boot: float | None = None
        self._lock = threading.Lock()

    def sample(
        self,
        ports: dict[str, str],
        *,
        running: bool,
        log: str,
        runs_root: Path | None = None,
        active_ports: set[str] | None = None,
    ) -> dict:
        mac_to_port = {mac.lower(): name for name, mac in ports.items() if mac}
        clients = _iperf_clients(self._age)
        servers = _iperf_servers()
        flows = _FLOW_RE.findall(log or "")
        roles = list(_ROLE_RE.finditer(log or ""))
        tx_ports: list[str] = []
        rx_ports: list[str] = []
        window = ""
        if roles:
            tx_ports = [name for name in roles[-1].group(1).split(",") if name]
            rx_ports = [name for name in roles[-1].group(2).split(",") if name]
            window = roles[-1].group(3)
        current = f"{flows[-1][0]} → {flows[-1][1]}" if flows else ""
        measuring = bool(clients)
        duration = max((item["duration"] for item in clients), default=None)
        remaining = max((item["remaining"] for item in clients), default=None)
        if tx_ports and rx_ports:
            current = f"{' y '.join(tx_ports)} transmiten → {' y '.join(rx_ports)} en modo cliente"
        elif measuring and not current and len(clients) == 1:
            current = f"{clients[0]['source'] or '?'} → {clients[0]['destination']}"
        elif measuring and not current:
            current = f"{len(clients)} clientes iperf3"
        interfaces = self._rates(
            _interfaces(mac_to_port, clients + servers, _ip_ports(log)),
            active=set(tx_ports + rx_ports),
            window=window,
        )
        if not measuring:
            with self._lock:
                self._shown.clear()
                self._previous.clear()
            for row in interfaces:
                row["tx_mbps"] = None
                row["rx_mbps"] = None
        if measuring and tx_ports and rx_ports:
            label = "cruzada" if window == "cruzada" else f"ventana {window or '1'}"
            phase = (
                f"iperf3 simultáneo, {label}: {' y '.join(tx_ports)} transmiten y "
                f"{' y '.join(rx_ports)} están en modo cliente."
            )
        elif measuring and len(clients) >= 2:
            phase = "iperf3 simultáneo: dos puertos transmiten y dos están en modo cliente."
        elif measuring:
            phase = "iperf3 está transmitiendo."
        elif running and servers:
            phase = "Servidores iperf3 en espera. El cronómetro arranca cuando empieza a transmitir."
        elif running:
            phase = "Preparando enlace y DHCP. iperf3 todavía no transmite."
        else:
            phase = "Sin medición."
        return {
            "tool": "iperf3",
            "measuring": measuring,
            "duration": duration,
            "remaining": None if remaining is None else round(remaining, 1),
            "flow": current,
            "flow_index": len(flows),
            "window": window,
            "tx_ports": tx_ports,
            "rx_ports": rx_ports,
            "clients": len(clients),
            "servers": len(servers),
            "phase": phase,
            "interfaces": interfaces,
            "readiness": _readiness(ports),
            "wifi": detect_wifi(),
            "cards": port_cards(
                ports,
                log=log,
                running=running,
                measuring=measuring,
                duration=duration,
                remaining=remaining,
                tx_ports=tx_ports,
                rx_ports=rx_ports,
                interfaces=interfaces,
                runs_root=runs_root,
                active_ports=active_ports,
            ),
            "detail": _detail(
                ports,
                running=running,
                log=log,
                runs_root=runs_root,
                tx_ports=tx_ports,
                rx_ports=rx_ports,
                interfaces=interfaces,
                measuring=measuring,
            ),
        }

    def _rates(self, rows: list[dict], active: set[str] | None = None, window: str = "") -> list[dict]:
        """Bytes over at least 0.2 s. A closer poll keeps the last rate instead of looking idle."""
        with self._lock:
            if window != self._window:
                self._quiet.clear()
                self._shown.clear()
                self._previous.clear()
                self._window = window
            now = time.monotonic()
            seen: set[str] = set()
            rated: list[dict] = []
            for row in rows:
                seen.add(row["key"])
                previous = self._previous.get(row["key"])
                tx_mbps = None
                rx_mbps = None
                fresh = False
                if previous is None:
                    self._previous[row["key"]] = (now, row["rx"], row["tx"])
                else:
                    delta = now - previous[0]
                    if delta >= 0.2:
                        tx_mbps = max(0.0, (row["tx"] - previous[2]) * 8 / delta / 1_000_000)
                        rx_mbps = max(0.0, (row["rx"] - previous[1]) * 8 / delta / 1_000_000)
                        self._previous[row["key"]] = (now, row["rx"], row["tx"])
                        fresh = True
                if not fresh:
                    held = self._shown.get(row["key"])
                    if held is not None:
                        rated.append(dict(held))
                        continue
                label = row["label"]
                watched = active is None or label in active
                if not watched or tx_mbps is None or max(tx_mbps, rx_mbps or 0) > 1:
                    self._quiet.pop(label, None)
                    quiet_for = 0.0
                else:
                    quiet_for = now - self._quiet.setdefault(label, now)
                led = traffic_led(
                    None if tx_mbps is None else round(tx_mbps, 1),
                    None if rx_mbps is None else round(rx_mbps, 1),
                    quiet_for if watched else 0.0,
                )
                shown = {
                    "label": label,
                    "iface": row["iface"],
                    "tx_mbps": None if tx_mbps is None else round(tx_mbps, 1),
                    "rx_mbps": None if rx_mbps is None else round(rx_mbps, 1),
                    "led": led["led"],
                    "alarm": led["alarm"] if watched else "",
                }
                if fresh:
                    self._shown[row["key"]] = shown
                rated.append(dict(shown))
            for key in list(self._previous):
                if key not in seen:
                    self._previous.pop(key, None)
                    self._shown.pop(key, None)
                    self._quiet.pop(key, None)
            return rated

    def _age(self, pid: int) -> float | None:
        try:
            stat = (_PROC / str(pid) / "stat").read_text(encoding="utf-8", errors="replace")
            rest = stat[stat.rfind(")") + 2 :].split()
            start_ticks = int(rest[19])
        except (OSError, IndexError, ValueError):
            return None
        if self._boot is None:
            self._boot = _boot_time()
        if self._boot is None:
            return None
        started = self._boot + start_ticks / os.sysconf("SC_CLK_TCK")
        return max(0.0, time.time() - started)


def _detail(
    ports: dict[str, str],
    *,
    running: bool,
    log: str,
    runs_root: Path | None,
    tx_ports: list[str],
    rx_ports: list[str],
    interfaces: list[dict],
    measuring: bool,
) -> dict:
    events = {name: [] for name in ports}
    for match in _PORT_RE.finditer(log or ""):
        events.setdefault(match.group(1), []).append(match.group(2).strip())
    evidence, run_id, verdict = _run_evidence(runs_root)
    rates = _busiest_by_label(interfaces)
    rows = []
    for name in ports:
        saved = evidence.get(name, {})
        story = _story(
            name,
            events.get(name, []),
            saved,
            transmitting=name in tx_ports,
            client=name in rx_ports,
            measuring=measuring,
            running=running,
            rate=rates.get(name),
        )
        rows.append({"port": name, **story})
    return {
        "run_id": run_id,
        "verdict": verdict,
        "headline": (
            "Sin medición. Cada ficha muestra el enlace actual, no el resultado de la última corrida."
            if not running and not measuring
            else _headline(run_id, verdict, rows, running, measuring, log=log)
        ),
        "ports": rows,
    }


def _story(
    name: str,
    events: list[str],
    saved: dict,
    *,
    transmitting: bool,
    client: bool,
    measuring: bool,
    running: bool,
    rate: dict | None,
) -> dict:
    link = _link_text(events, saved)
    dhcp = _dhcp_text(events, saved, running)
    if transmitting:
        role = "Transmite"
    elif client:
        role = "Modo cliente"
    elif measuring:
        role = "En la medición"
    elif dhcp["state"] in {"fail", "nolink"}:
        role = "No llegó a medir"
    elif running:
        role = "Preparando"
    else:
        role = "En espera"
    tx = "—" if rate is None or rate.get("tx_mbps") is None else f"{rate['tx_mbps']:.1f} Mbps"
    rx = "—" if rate is None or rate.get("rx_mbps") is None else f"{rate['rx_mbps']:.1f} Mbps"
    return {
        "link": link["text"],
        "dhcp": dhcp["text"],
        "role": role,
        "traffic": f"envía {tx} · recibe {rx}",
        "story": _sentence(name, link, dhcp, role, measuring),
    }


def _sentence(name: str, link: dict, dhcp: dict, role: str, measuring: bool) -> str:
    if measuring and role == "Transmite":
        return f"{name} está transmitiendo con iperf3."
    if measuring and role == "Modo cliente":
        return f"{name} está en modo cliente y debe estar recibiendo."
    if link["state"] == "down":
        return f"{name} no tiene enlace. El ONT no puede darle IP y este puerto no transmite."
    if dhcp["state"] == "asking":
        return f"{name} tiene enlace y está pidiendo IP al ONT."
    if dhcp["state"] == "fail":
        return (
            f"{name} tiene enlace, pero el DHCP de la prueba no recibió IP. "
            "Por eso iperf3 no transmite en este puerto."
        )
    if dhcp["state"] == "ok" and not measuring:
        return f"{name} tiene enlace e IP {dhcp['text']}. Todavía no entra a la ventana de iperf3."
    if link["state"] == "up":
        return f"{name} tiene enlace {link['text']}."
    return f"{name} todavía no entra en la prueba."


def _link_text(events: list[str], saved: dict) -> dict:
    for event in reversed(events):
        if event == "enlace no":
            return {"state": "down", "text": "Sin enlace"}
        if event.startswith("enlace si"):
            return {"state": "up", "text": event.removeprefix("enlace si ").strip() or "Enlace activo"}
    detected = saved.get("link_detected")
    if detected is False:
        return {"state": "down", "text": "Sin enlace"}
    if detected is True:
        speed = saved.get("speed") or "Enlace activo"
        return {"state": "up", "text": speed}
    return {"state": "unknown", "text": "Aún no revisado"}


def _dhcp_text(events: list[str], saved: dict, running: bool) -> dict:
    for event in reversed(events):
        if event == "dhcp pidiendo":
            return {"state": "asking", "text": "Pidiendo IP"}
        if event.startswith("dhcp ok"):
            return {"state": "ok", "text": event.removeprefix("dhcp ok ").strip()}
        if event.startswith("dhcp fail"):
            return {"state": "fail", "text": "Sin respuesta del ONT"}
    if saved.get("dhcp_success") is True and saved.get("ip"):
        return {"state": "ok", "text": saved["ip"]}
    if saved.get("dhcp_success") is False:
        return {"state": "fail", "text": "Sin respuesta del ONT"}
    if running and saved.get("link_detected") is True:
        return {"state": "asking", "text": "Pidiendo IP"}
    return {"state": "unknown", "text": "—"}


def _headline(run_id: str, verdict: str, rows: list[dict], running: bool, measuring: bool, log: str = "") -> str:
    if measuring:
        transmitters = [row["port"] for row in rows if row["role"] == "Transmite"]
        clients = [row["port"] for row in rows if row["role"] == "Modo cliente"]
        if transmitters and clients:
            return (
                f"{' y '.join(transmitters)} transmiten. "
                f"{' y '.join(clients)} están en modo cliente."
            )
        return "iperf3 está transmitiendo."
    if running:
        asking = [row["port"] for row in rows if row["dhcp"] == "Pidiendo IP"]
        if asking:
            return f"La prueba está pidiendo IP al ONT en {', '.join(asking)}."
        return "La prueba está revisando enlace y DHCP. iperf3 todavía no transmite."
    for line in (log or "").splitlines():
        if "PREFLIGHT FAIL" in line:
            detail = line.split("PREFLIGHT FAIL", 1)[-1].strip().lstrip(":")
            label = f"{run_id}: " if run_id else ""
            return (
                f"{label}la medición no arrancó — preflight bloqueó la corrida ({detail}). "
                "Revisa el log abajo; si dice management-protection, pulsa Recuperar namespaces."
            )
    failed = [row for row in rows if row["dhcp"] == "Sin respuesta del ONT" or row["link"] == "Sin enlace"]
    if verdict == "FAIL" and failed:
        bits = []
        for row in failed:
            if row["link"] == "Sin enlace":
                bits.append(f"{row['port']} sin enlace")
            else:
                bits.append(f"{row['port']} sin IP")
        label = f"{run_id}: " if run_id else ""
        if "RESULT PASS" in (log or ""):
            return (
                label
                + "la corrida quedó en FAIL por "
                + ", ".join(bits)
                + ". Otros puertos sí transmitieron. En cada ficha, Enlace ahora es la lectura actual."
            )
        return label + "la prueba falló antes de transmitir. " + ", ".join(bits) + "."
    if verdict:
        return f"{run_id}: resultado {verdict}."
    return "Sin una medición en curso."


def _run_evidence(runs_root: Path | None) -> tuple[dict[str, dict], str, str]:
    if runs_root is None or not runs_root.is_dir():
        return {}, "", ""
    runs = list_runs(runs_root)
    if not runs:
        return {}, "", ""
    folder = runs[-1]
    evidence: dict[str, dict] = {}
    for dhcp_path in (folder / "raw" / "dhcp").glob("LAN*.json"):
        try:
            payload = json.loads(dhcp_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        evidence.setdefault(dhcp_path.stem, {}).update(
            {
                "dhcp_success": payload.get("success"),
                "ip": payload.get("ip") or "",
                "dhcp_message": payload.get("message") or "",
            }
        )
    ethtool = folder / "raw" / "ethtool"
    if ethtool.is_dir():
        for path in ethtool.glob("LAN*.txt"):
            text = path.read_text(encoding="utf-8", errors="replace")
            detected = None
            if "Link detected: yes" in text:
                detected = True
            elif "Link detected: no" in text:
                detected = False
            speed = ""
            duplex = ""
            for line in text.splitlines():
                if "Speed:" in line and "Unknown" not in line:
                    speed = line.split(":", 1)[1].strip()
                elif "Duplex:" in line:
                    duplex = line.split(":", 1)[1].strip()
            evidence.setdefault(path.stem, {}).update(
                {"link_detected": detected, "speed": speed, "duplex": duplex}
            )
    verdict = ""
    report = folder / "report.json"
    if report.is_file():
        try:
            payload = json.loads(report.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}
        verdict = str(payload.get("global_verdict") or "")
        for port in payload.get("ports") or []:
            logical = str(port.get("logical_name") or "")
            if not logical:
                continue
            link = port.get("link") or {}
            evidence.setdefault(logical, {}).update(
                {
                    "tx_mbps": port.get("tx_mbps"),
                    "rx_mbps": port.get("rx_mbps"),
                    "result": port.get("verdict") or "",
                    "failure": port.get("failure_reason") or "",
                    "duplex": link.get("duplex") or evidence.get(logical, {}).get("duplex", ""),
                    "speed_mbps": link.get("speed_mbps"),
                }
            )
    return evidence, folder.name, verdict


def _readiness(ports: dict[str, str]) -> list[dict]:
    """Each configured LAN as it is now, against the state required to measure."""
    addresses = _ipv4_by_iface()
    present: dict[str, str] = {}
    if _NET.is_dir():
        for entry in _NET.iterdir():
            try:
                mac = (entry / "address").read_text(encoding="utf-8").strip().lower()
            except OSError:
                continue
            present[mac] = entry.name
    rows = []
    for logical, mac in ports.items():
        if not mac:
            rows.append(
                {
                    "port": logical,
                    "iface": "—",
                    "state": "Sin NIC",
                    "detail": "Adaptador de esta PC no asignado — Remapear LAN",
                    "speed": "",
                    "duplex": "",
                }
            )
            continue
        iface = present.get(mac.lower(), "")
        carrier = _carrier(iface) if iface else None
        address = addresses.get(iface, "")
        if not iface:
            state = "Sin adaptador"
            detail = "Conecta el USB o la placa a esta PC"
        elif carrier != "1":
            state = "Sin enlace"
            detail = "El adaptador está en la PC; falta el cable al puerto LAN del ONT"
        elif not address:
            state = "Sin IP"
            detail = "Hay enlace; el ONT todavía no entregó DHCP"
        else:
            state = "Listo"
            detail = address
        rows.append(
            {
                "port": logical,
                "iface": iface or "—",
                "state": state,
                "detail": detail,
                "speed": _speed(iface) if iface and carrier == "1" else "",
                "duplex": _duplex(iface) if iface and carrier == "1" else "",
            }
        )
    return rows


def detect_wifi() -> dict:
    """The management radio, if Linux exposes one. Never one of the LAN ports."""
    if not _NET.is_dir():
        return {"iface": "", "ip": "", "state": "absent", "excluded": True}
    addresses = _ipv4_by_iface()
    for entry in sorted(_NET.iterdir(), key=lambda item: item.name):
        if not _IFACE_RE.fullmatch(entry.name):
            continue
        if not (entry / "wireless").exists() and not (entry / "phy80211").exists():
            continue
        try:
            oper = (entry / "operstate").read_text(encoding="utf-8").strip().lower()
        except OSError:
            oper = ""
        return {
            "iface": entry.name,
            "ip": addresses.get(entry.name, ""),
            "state": "UP" if oper == "up" else "OFF",
            "excluded": True,
        }
    return {"iface": "", "ip": "", "state": "absent", "excluded": True}


def _nmcli_fields(line: str) -> list[str]:
    parts: list[str] = []
    buf: list[str] = []
    escaped = False
    for char in line.rstrip("\n"):
        if escaped:
            buf.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == ":":
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(char)
    parts.append("".join(buf))
    return parts


def wifi_networks(rescan: bool = False) -> list[dict]:
    """Visible SSIDs from the station radio. Channel and band only; no keys."""
    info = detect_wifi()
    iface = info.get("iface") or ""
    if not iface or not _IFACE_RE.fullmatch(iface):
        return []
    if rescan:
        subprocess.run(
            ["nmcli", "device", "wifi", "rescan", "ifname", iface],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    completed = subprocess.run(
        ["nmcli", "-t", "-f", "IN-USE,SSID,CHAN,FREQ,SIGNAL", "device", "wifi", "list", "ifname", iface],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    rows = []
    seen: set[tuple[str, str, str]] = set()
    for line in (completed.stdout or "").splitlines():
        fields = _nmcli_fields(line)
        if len(fields) < 5:
            continue
        in_use, ssid, channel, freq, signal = fields[:5]
        name = ssid.strip()
        if not name or len(name) > 32:
            continue
        number = "".join(char for char in freq if char.isdigit())
        mhz = int(number) if number else 0
        band = "5 GHz" if mhz >= 5000 else "2.4 GHz" if 2000 <= mhz < 3000 else ""
        key = (name, channel.strip(), band)
        if key in seen:
            continue
        seen.add(key)
        rows.append(
            {
                "ssid": name,
                "channel": channel.strip(),
                "frequency": freq.strip(),
                "band": band,
                "signal": signal.strip(),
                "active": in_use.strip() == "*",
            }
        )
        if len(rows) >= 40:
            break
    rows.sort(key=lambda item: (not item["active"], item["band"], item["ssid"]))
    return rows


def port_cards(
    ports: dict[str, str],
    *,
    log: str,
    running: bool,
    measuring: bool,
    duration: int | None,
    remaining: float | None,
    tx_ports: list[str],
    rx_ports: list[str],
    interfaces: list[dict],
    runs_root: Path | None,
    active_ports: set[str] | None = None,
) -> list[dict]:
    # A finished run stays in the log file. The live cards must not keep that result.
    if running:
        evidence = _run_evidence(runs_root)[0] if "STATE " in (log or "") else {}
        states = _state_map(log)
    else:
        evidence = {}
        states = {}
    rates = _busiest_by_label(interfaces)
    ready = {row["port"]: row for row in _readiness(ports)}
    cards = []
    for name in sorted(ports.keys(), key=lambda item: int(item.replace("LAN", "") or "0")):
        mac = ports[name]
        if not mac:
            cards.append(_unassigned_port_card(name, ready.get(name, {})))
            continue
        if running and active_ports is not None and name not in active_ports:
            cards.append(_out_of_scope_port_card(name, ready.get(name, {})))
            continue
        cards.append(
            _port_card(
                name,
                states.get(name, {}),
                evidence.get(name, {}),
                ready.get(name, {}),
                measuring=measuring,
                running=running,
                duration=duration,
                remaining=remaining,
                transmitting=name in tx_ports,
                receiving=name in rx_ports,
                rate=rates.get(name),
            )
        )
    return cards


def _out_of_scope_port_card(name: str, ready: dict) -> dict:
    """Ports not in the active bench while a single-bench run is in progress."""
    now = _live_link(ready)
    return {
        "port": name,
        "iface": ready.get("iface") or "—",
        "link": "PASS" if ready.get("link") == "UP" else (ready.get("link") or "N/A"),
        "speed": ready.get("speed") or "N/A",
        "duplex": ready.get("duplex") or "N/A",
        "dhcp": "NOT RUN",
        "ip": ready.get("detail") or "N/A",
        "state": "NO EN ESTA CORRIDA",
        "result": "NOT RUN",
        "progress": "—",
        "tx": "N/A",
        "rx": "N/A",
        "iperf": "NOT RUN",
        "reason": "Este banco no participa en la corrida actual. Usa «Certificar ambos bancos» para medir A y B a la vez.",
        "led": "off",
        "alarm": False,
        "now_link": now.get("now_link") or "N/A",
        "now_speed": now.get("now_speed") or "N/A",
    }


def _unassigned_port_card(name: str, ready: dict) -> dict:
    return {
        "port": name,
        "iface": "—",
        "link": "N/A",
        "speed": "N/A",
        "duplex": "N/A",
        "dhcp": "NOT RUN",
        "ip": "N/A",
        "state": "SIN_NIC",
        "result": "REMAPEAR",
        "progress": "—",
        "tx": "N/A",
        "rx": "N/A",
        "iperf": "NOT RUN",
        "reason": ready.get("detail") or "Sin tarjeta Ethernet de la PC en este puerto lógico",
        "result": "NOT RUN",
        "led": "off",
        "alarm": False,
        "now_link": "N/A",
        "now_speed": "N/A",
    }


def _state_map(log: str) -> dict[str, dict[str, tuple[str, str]]]:
    found: dict[str, dict[str, tuple[str, str]]] = {}
    for match in _STATE_RE.finditer(log or ""):
        found.setdefault(match.group(1), {})[match.group(2)] = (match.group(3), (match.group(4) or "").strip())
    return found


def _port_card(
    name: str,
    stages: dict[str, tuple[str, str]],
    saved: dict,
    ready: dict,
    *,
    measuring: bool,
    running: bool,
    duration: int | None,
    remaining: float | None,
    transmitting: bool,
    receiving: bool,
    rate: dict | None,
) -> dict:
    link_stage = stages.get("LINK_CHECK")
    dhcp_stage = stages.get("DHCP")
    iperf_stage = stages.get("IPERF3")
    reason = ""
    if link_stage and link_stage[0] == "FAIL":
        link = "FAIL"
        reason = link_stage[1]
    elif link_stage and link_stage[0] == "PASS":
        link = "PASS"
    elif saved.get("link_detected") is False:
        link = "FAIL"
        reason = "Link detected: no"
    elif saved.get("link_detected") is True:
        link = "PASS"
    elif ready.get("state") == "Sin enlace":
        link = "DOWN"
    elif ready.get("state") in {"Listo", "Sin IP"}:
        link = "UP"
    else:
        link = "WAITING"
    if dhcp_stage and dhcp_stage[0] == "SKIPPED":
        dhcp = "SKIPPED"
        ip = "N/A"
        reason = reason or dhcp_stage[1]
    elif dhcp_stage and dhcp_stage[0] == "FAIL":
        dhcp = "FAIL"
        ip = "N/A"
        reason = dhcp_stage[1] or reason
    elif dhcp_stage and dhcp_stage[0] == "PASS":
        dhcp = "PASS"
        ip = dhcp_stage[1] or saved.get("ip") or "N/A"
    elif dhcp_stage and dhcp_stage[0] == "REQUESTING":
        dhcp = "REQUESTING"
        ip = "N/A"
    elif saved.get("dhcp_success") is True and saved.get("ip"):
        dhcp = "PASS"
        ip = saved["ip"]
    elif saved.get("dhcp_success") is False:
        message = str(saved.get("dhcp_message") or "")
        dhcp = "SKIPPED" if message.startswith("SKIPPED") else "FAIL"
        ip = "N/A"
        reason = reason or message or "DHCP timeout"
    elif ready.get("state") == "Listo":
        dhcp = "NOT RUN"
        ip = ready.get("detail") or "N/A"
    else:
        dhcp = "NOT RUN"
        ip = "N/A"
    result_stage = stages.get("RESULT")
    result_override = ""
    if rate and "led" not in rate:
        level = max(float(rate.get("tx_mbps") or 0), float(rate.get("rx_mbps") or 0))
        led = "on" if level > 1 else "off"
    else:
        led = str((rate or {}).get("led") or "off")
    alarm = str((rate or {}).get("alarm") or "")
    guard_codes = (
        "INTERFACE_DOWN",
        "NO_IP",
        "BIND_ERROR",
        "FAIL_NO_TRAFFIC",
        "UNIT_POWER_OFF_OR_REBOOT",
        "TEST_ABORTED_BY_CARRIER",
        "EHOSTUNREACH",
        "ECONNREFUSED",
        "ECONNRESET",
    )
    guard_hit = next((code for code in guard_codes if code in stages), "")
    carrier_down = guard_hit in {"INTERFACE_DOWN", "UNIT_POWER_OFF_OR_REBOOT", "TEST_ABORTED_BY_CARRIER"}
    if carrier_down:
        activity = "TEST_ABORTED_BY_CARRIER"
        iperf = "NOT RUN"
        reason = "FALLA: Caída de Enlace / Reinicio del DUT"
        result_override = "FAIL"
        led = "off"
    elif guard_hit:
        activity = guard_hit
        iperf = "NOT RUN"
        reason = stages[guard_hit][1] or guard_hit
        result_override = "FAIL"
        led = "off"
    elif alarm == "FAIL_NO_TRAFFIC":
        activity = "FAIL_NO_TRAFFIC"
        iperf = "FAIL_NO_TRAFFIC"
        reason = "Sin tráfico real: 0 Mbps por más de 2 s"
        result_override = "FAIL"
    elif measuring and transmitting and led == "on":
        activity = "TRANSMITTING"
        iperf = "TRANSMITTING"
        result_override = ""
    elif measuring and receiving and led == "on":
        activity = "RECEIVING"
        iperf = "RECEIVING"
        result_override = ""
    elif measuring and (transmitting or receiving):
        activity = "ESPERANDO TRAFICO"
        iperf = "NOT RUN"
        result_override = ""
        reason = reason or "iperf3 aún no mueve bytes en el cable"
    elif result_stage and result_stage[0] == "PASS":
        activity = "COMPLETED"
        iperf = "PASS"
        reason = ""
    elif iperf_stage and iperf_stage[0] == "STARTING" and running:
        activity = "IPERF3 STARTING"
        iperf = "STARTING"
    elif iperf_stage and iperf_stage[0] == "NOT_RUN":
        activity = "COMPLETED"
        iperf = "NOT RUN"
        borrowed = link == "PASS" and iperf_stage[1] == "Link detected: no"
        reason = reason or ("" if borrowed else iperf_stage[1])
        if borrowed and not reason:
            reason = "Su par no tuvo enlace"
    elif link == "FAIL" or dhcp in {"FAIL", "SKIPPED"}:
        activity = "COMPLETED"
        iperf = "NOT RUN"
    elif running and dhcp == "REQUESTING":
        activity = "REQUESTING DHCP"
        iperf = "NOT RUN"
    elif running and link in {"WAITING", "UP", "PASS"} and dhcp == "NOT RUN":
        activity = "CHECKING LINK"
        iperf = "NOT RUN"
    else:
        activity = "WAITING"
        iperf = "NOT RUN"
    if result_stage and result_stage[0] == "FAIL" and result_stage[1] and link == "PASS" and dhcp == "PASS":
        reason = result_stage[1]
    if result_override:
        result = result_override
    elif link == "FAIL" or dhcp == "FAIL":
        result = "FAIL"
    elif stages.get("RESULT", ("", ""))[0] in {"PASS", "FAIL", "SKIPPED"}:
        result = stages["RESULT"][0]
    elif not running and saved.get("result") in {"PASS", "FAIL", "SKIPPED"} and iperf != "NOT RUN":
        result = saved["result"]
    else:
        result = "NOT RUN" if iperf == "NOT RUN" and link != "FAIL" and dhcp != "FAIL" else result_from(link, dhcp)
    progress = "—"
    if measuring and (transmitting or receiving) and duration and remaining is not None:
        elapsed = max(0, int(round(duration - remaining)))
        progress = f"{elapsed} / {int(duration)} s"
    tx_text = "N/A"
    rx_text = "N/A"
    if iperf in {"TRANSMITTING", "RECEIVING"} and rate is not None:
        if rate.get("tx_mbps") is not None:
            tx_text = f"{rate['tx_mbps']:.0f} Mbps"
        if rate.get("rx_mbps") is not None:
            rx_text = f"{rate['rx_mbps']:.0f} Mbps"
    elif iperf != "NOT RUN":
        if saved.get("tx_mbps") is not None:
            tx_text = f"{float(saved['tx_mbps']):.0f} Mbps"
        if saved.get("rx_mbps") is not None:
            rx_text = f"{float(saved['rx_mbps']):.0f} Mbps"
    if carrier_down:
        led = "off"
    elif measuring and (transmitting or receiving):
        led = "on"
    speed = ready.get("speed") or ""
    if not speed and saved.get("speed"):
        speed = str(saved["speed"])
    if not speed and saved.get("speed_mbps"):
        speed = f"{saved['speed_mbps']} Mbps"
    duplex = ready.get("duplex") or saved.get("duplex") or "N/A"
    if link == "FAIL":
        speed = "N/A"
        duplex = "N/A"
    now = _live_link(ready)
    return {
        "port": name,
        "iface": ready.get("iface") or "—",
        "link": link,
        "speed": speed or "N/A",
        "duplex": duplex or "N/A",
        "dhcp": dhcp,
        "ip": ip or "N/A",
        "state": activity,
        "progress": progress,
        "tx": tx_text,
        "rx": rx_text,
        "iperf": iperf,
        "reason": reason,
        "result": result,
        "led": led,
        "alarm": alarm,
        **now,
    }


def _live_link(ready: dict) -> dict:
    state = ready.get("state") or ""
    if state in {"Listo", "Sin IP"}:
        link = "UP"
    elif state == "Sin enlace":
        link = "DOWN"
    elif state == "Sin adaptador":
        link = "ABSENT"
    else:
        link = "WAITING"
    return {
        "now_link": link,
        "now_speed": ready.get("speed") or "N/A",
        "now_duplex": ready.get("duplex") or "N/A",
        "now_ip": ready.get("detail") if state == "Listo" else "N/A",
        "now_iface": ready.get("iface") or "—",
    }


def probe_port(ports: dict[str, str], logical: str) -> dict:
    """Read one configured LAN from sysfs. The browser cannot name an arbitrary interface."""
    if logical not in ports:
        raise ValueError("Puerto no configurado")
    ready = next(iter(_readiness({logical: ports[logical]})), {})
    return {"port": logical, **_live_link(ready)}


def result_from(link: str, dhcp: str) -> str:
    if link == "FAIL" or dhcp == "FAIL":
        return "FAIL"
    if dhcp == "SKIPPED":
        return "FAIL" if link == "FAIL" else "SKIPPED"
    return "NOT RUN"


def _carrier(iface: str) -> str:
    try:
        return (_NET / iface / "carrier").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _speed(iface: str) -> str:
    try:
        speed = (_NET / iface / "speed").read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    return "" if not speed.isdigit() or speed == "-1" else f"{speed} Mbps"


def _duplex(iface: str) -> str:
    try:
        text = (_NET / iface / "duplex").read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    if text.lower() not in {"full", "half"}:
        return ""
    return text.capitalize()


def _ipv4_by_iface() -> dict[str, str]:
    try:
        result = subprocess.run(
            ["ip", "-4", "-br", "addr"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    found: dict[str, str] = {}
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[2] != "-":
            found[parts[0]] = parts[2]
    return found


def _busiest_by_label(rows: list[dict]) -> dict[str, dict]:
    """Two readings can share a LAN. The card keeps the one that is moving bytes."""
    chosen: dict[str, dict] = {}
    for row in rows:
        label = str(row.get("label") or "")
        current = chosen.get(label)
        if current is None or _rate_level(row) >= _rate_level(current):
            chosen[label] = row
    return chosen


def _rate_level(row: dict) -> float:
    values = [float(value) for value in (row.get("tx_mbps"), row.get("rx_mbps")) if value is not None]
    if not values:
        return -1.0
    return max(values)


def _ip_ports(log: str) -> dict[str, str]:
    found = {}
    for match in re.finditer(r"PORT (LAN\d+) dhcp ok (\d+\.\d+\.\d+\.\d+)", log or ""):
        found[match.group(2)] = match.group(1)
    return found


def _interfaces(mac_to_port: dict[str, str], peers: list[dict], ip_to_port: dict[str, str] | None = None) -> list[dict]:
    found: dict[str, dict] = {}
    if _NET.is_dir():
        for entry in _NET.iterdir():
            if entry.name == "lo":
                continue
            try:
                mac = (entry / "address").read_text(encoding="utf-8").strip().lower()
                rx = int((entry / "statistics" / "rx_bytes").read_text())
                tx = int((entry / "statistics" / "tx_bytes").read_text())
            except (OSError, ValueError):
                continue
            logical = mac_to_port.get(mac)
            if logical is None:
                continue
            found[f"host:{entry.name}"] = {
                "key": f"host:{entry.name}",
                "label": logical,
                "iface": entry.name,
                "rx": rx,
                "tx": tx,
            }
    try:
        host_net = os.stat("/proc/self/ns/net").st_ino
    except OSError:
        host_net = None
    for peer in peers:
        dev = _PROC / str(peer["pid"]) / "net" / "dev"
        try:
            inode = os.stat(f"/proc/{peer['pid']}/ns/net").st_ino
            if host_net is not None and inode == host_net:
                continue
            text = dev.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for row in _parse_dev(text):
            if row["iface"] == "lo":
                continue
            key = f"ns:{inode}:{row['iface']}"
            if key in found:
                continue
            address = str(peer.get("address") or peer.get("source") or "")
            label = (ip_to_port or {}).get(address) or row["iface"]
            found[key] = {
                "key": key,
                "label": label,
                "iface": row["iface"],
                "rx": row["rx"],
                "tx": row["tx"],
            }
    return sorted(found.values(), key=lambda item: item["label"])


def _iperf_clients(age_of) -> list[dict]:
    found = []
    for pid, args in _iperf_processes():
        if "-c" not in args or "-t" not in args:
            continue
        duration = _int_after(args, "-t")
        if duration is None:
            continue
        age = age_of(pid)
        if age is None:
            continue
        found.append(
            {
                "pid": pid,
                "duration": duration,
                "remaining": max(0.0, duration - age),
                "source": _text_after(args, "-B"),
                "destination": _text_after(args, "-c"),
            }
        )
    return found


def _iperf_servers() -> list[dict]:
    return [
        {"pid": pid, "address": _text_after(args, "-B")}
        for pid, args in _iperf_processes()
        if "-s" in args and "-c" not in args
    ]


def _iperf_processes() -> list[tuple[int, list[str]]]:
    found = []
    if not _PROC.is_dir():
        return found
    for entry in _PROC.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        args = [part for part in raw.decode("utf-8", "replace").split("\0") if part]
        if not any(part == "iperf3" or part.endswith("/iperf3") for part in args):
            continue
        found.append((int(entry.name), args))
    return found


def _parse_dev(text: str) -> list[dict]:
    rows = []
    for line in text.splitlines():
        if ":" not in line:
            continue
        name, _, rest = line.partition(":")
        fields = rest.split()
        if len(fields) < 9 or not name.strip():
            continue
        try:
            rows.append({"iface": name.strip(), "rx": int(fields[0]), "tx": int(fields[8])})
        except ValueError:
            continue
    return rows


def _text_after(args: list[str], flag: str) -> str:
    if flag not in args:
        return ""
    index = args.index(flag) + 1
    return args[index] if index < len(args) else ""


def _int_after(args: list[str], flag: str) -> int | None:
    text = _text_after(args, flag)
    return int(text) if text.isdigit() else None


def _boot_time() -> float | None:
    try:
        for line in (_PROC / "stat").read_text(encoding="utf-8").splitlines():
            if line.startswith("btime "):
                return float(line.split()[1])
    except (OSError, IndexError, ValueError):
        return None
    return None
