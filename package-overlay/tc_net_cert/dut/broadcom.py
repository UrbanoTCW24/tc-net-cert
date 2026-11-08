"""Read a Kaon / Broadcom EMTA portal with HTTP Basic auth.

One password is sent. The Huawei session cookie is not used. The Wi-Fi key
is returned to the local panel only; the certificate does not print it.
This reader does not claim DOCSIS provisioning, CMTS registration, or TFTP.
"""

from __future__ import annotations

import base64
import re
import socket

from tc_net_cert.dut.huawei import _result, allowed_host, valid_password, valid_user

_PAGES = (
    "/RgSwInfo.asp",
    "/cmswinfo.html",
    "/RgConnect.asp",
    "/cmconnectionstatus.html",
    "/wlanPrimaryNetwork.asp",
    "/wlswitchint1wlsecurity.wl",
    "/wlswitchint0wlsecurity.wl",
)
_KAON_MODEL = "CG-2200"
_LABELS = {
    "hardware": ("versión de hardware", "version de hardware", "version del hardware", "hardware version"),
    "software": ("versión de software", "version de software", "version del software", "software version"),
    "mac": (
        "dirección mac cable modem",
        "direccion mac cable modem",
        "direccion mac del modem",
        "mac address",
    ),
    "serial": (
        "número de serie cable modem",
        "numero de serie cable modem",
        "numero de serie del cable modem",
        "serial number",
    ),
    "docsis": (
        "estándar soportado",
        "estandar soportado",
        "cumple con las especificaciones estandar",
    ),
    "model": ("modelo", "modelo del equipo", "model name", "product model"),
    "access": ("acceso a red", "acceso a la red"),
    "ssid": ("nombre de red (ssid)", "ssid"),
    "temperature": ("temperatura de la tarjeta",),
    "uptime": ("tiempo de funcionamiento",),
    "cm_ip": (
        "dirección ip de cable modem",
        "direccion ip de cable modem",
        "dirección ip del cable modem",
        "direccion ip del cable modem",
    ),
}


def read_basic(
    host: str,
    username: str,
    password: str,
    source: str = "",
    device: str = "",
) -> dict[str, object]:
    if not allowed_host(host) or not valid_user(username) or not valid_password(password):
        return _result(False, error="Revisa el usuario y la contraseña del EMTA.")
    pages: dict[str, str] = {}
    software = ""
    status = 0
    denied = False
    for path in ("/RgSwInfo.asp", "/cmswinfo.html", "/"):
        status, software = _get(host, path, username, password, source, device, timeout=12)
        if status == 401:
            denied = True
            software = ""
            continue
        if status == 200 and _is_status_page(software):
            pages[path] = software
            denied = False
            break
        software = ""
        if status == 0:
            break
    if not software:
        if denied and status == 401:
            return _result(False, error="El EMTA no aceptó el usuario o la contraseña.", model="Kaon")
        if status == 0:
            return _result(False, error="El portal no respondió por el puerto de prueba. No se intentó el ingreso.")
        return _result(False, error="El portal del EMTA no entregó la página de estado.", model="Kaon")
    for path in _PAGES[1:]:
        status, body = _get(host, path, username, password, source, device, timeout=12)
        if path in {"/RgConnect.asp", "/cmconnectionstatus.html"}:
            for _attempt in range(2):
                if status == 200 and _is_channel_page(body):
                    break
                status, body = _get(host, path, username, password, source, device, timeout=12)
            if not _is_channel_page(body):
                continue
        elif status == 200 and "update in progress" in (body or "").lower():
            status, body = _get(host, path, username, password, source, device, timeout=12)
        if status == 200 and body.strip() and "update in progress" not in body.lower():
            pages[path] = body
    found = parse_status(software)
    channels = parse_channels(pages.get("/RgConnect.asp", "") or pages.get("/cmconnectionstatus.html", ""))
    radios = _band_ssids(host, username, password, source, device) or _switch_ssids(pages) or _ssids(list(pages.values()))
    if not found.get("serial") and not found.get("mac"):
        return _result(
            False,
            error="El EMTA abrió la sesión, pero la página no mostró el SN ni la MAC.",
            model="Kaon",
        )
    brand = parse_brand(software)
    if not brand:
        _status, footer = _get(host, "/footer.html", username, password, source, device, timeout=8)
        brand = parse_brand(footer)
    result = _result(
        True,
        serial=found.get("serial", ""),
        mac=found.get("mac", ""),
        model=commercial_model(brand, found.get("model") or found.get("hardware", "")),
        software=found.get("software", ""),
        hardware=found.get("hardware", ""),
        device_type=found.get("docsis") or "EMTA",
        ssid_24=radios[0],
        ssid_50=radios[1],
        wifi_password_24=radios[2],
        wifi_password_50=radios[3],
    )
    result["brand"] = brand
    from tc_net_cert.dut.portals import canonical_profile_model

    commercial = commercial_model(brand, found.get("model") or found.get("hardware", ""))
    result["profile"] = canonical_profile_model(commercial) or commercial or _KAON_MODEL
    connect = pages.get("/RgConnect.asp", "") or pages.get("/cmconnectionstatus.html", "")
    link_state = _row_comment(connect, "conectividad")
    result["ds_channels"] = channels["ds_channels"]
    result["ds_power"] = channels["ds_power"]
    result["us_channels"] = channels["us_channels"]
    result["us_power"] = channels["us_power"]
    result["mer"] = channels["mer"]
    result["ds_rows"] = channels["ds_rows"]
    result["us_rows"] = channels["us_rows"]
    result["ds_correctable_total"] = channels["ds_correctable_total"]
    result["ds_uncorrectable_total"] = channels["ds_uncorrectable_total"]
    result["temperature"] = found.get("temperature", "")
    result["uptime"] = found.get("uptime", "")
    result["cm_ip"] = found.get("cm_ip", "")
    result["docsis_status"] = _docsis(found, link_state)
    _status, mta = _get(host, "/MtaStatus.asp", username, password, source, device, timeout=8)
    result["mta_dhcp"] = _row_comment(mta, "dhcp")
    _status, setup = _get(host, "/RgSetup.asp", username, password, source, device, timeout=8)
    result["wan_dhcp"] = _row_comment(setup, "conexión wan")
    return result


def _is_status_page(html: str) -> bool:
    text = (html or "").lower()
    return "serie" in text or "serial number" in text


def commercial_model(brand: str, parsed: str) -> str:
    """Use the model printed on the page. A hardware version such as v1.0 is not the model."""
    hitron = re.search(r"CGNV\d+", parsed or "", flags=re.I)
    if hitron:
        return hitron.group(0).upper()
    if (brand or "").upper() in {"HITRON", "HISTRON", "HISTRIN"} and not (parsed or "").strip():
        return ""
    match = re.search(r"CG-?\d{4}", parsed or "", flags=re.I)
    if match:
        token = match.group(0).upper()
        if not token.startswith("CG-"):
            token = "CG-" + token[2:]
        return token
    text = (parsed or "").strip()
    if re.fullmatch(r"v?\d+(?:\.\d+)+", text, flags=re.I):
        text = ""
    if text:
        return text
    if brand == "KAON":
        return _KAON_MODEL
    return ""


def parse_brand(html: str) -> str:
    """The commercial brand is the logo text. The profile name is not the brand."""
    for value in re.findall(r'alt="([^"]+)"', html or "", flags=re.I):
        if "kaon" in value.lower():
            return "KAON"
    if "kaonmedia" in (html or "").lower():
        return "KAON"
    return ""


def parse_status(html: str) -> dict[str, str]:
    found = {key: "" for key in _LABELS}
    for cells in _rows(html):
        for index, text in enumerate(cells[:-1]):
            key = _label(text)
            if not key or found[key]:
                continue
            value = cells[index + 1]
            if value and _label(value) is None:
                found[key] = value[:80]
    return found


def parse_channels(html: str) -> dict[str, str | list]:
    """Summaries plus every DS and US row the connection page prints."""
    section = ""
    expect_totals = False
    correctable_total = ""
    uncorrectable_total = ""
    groups: dict[str, list[tuple[bool, str, str]]] = {"ds": [], "us": []}
    tables: dict[str, list[dict[str, str]]] = {"ds": [], "us": []}
    for cells in _rows(html):
        joined = " ".join(cells).lower()
        if "total corregible" in joined:
            expect_totals = True
            continue
        if expect_totals:
            expect_totals = False
            if len(cells) >= 2 and cells[0].replace(",", "").isdigit() and cells[1].replace(",", "").isdigit():
                correctable_total = cells[0][:16]
                uncorrectable_total = cells[1][:16]
            continue
        header = _header_section(cells)
        if header:
            section = header
            continue
        if not section or not cells or not cells[0].isdigit():
            continue
        locked = _locked(cells)
        if locked is None:
            continue
        power = next((item for item in cells if "dbmv" in item.lower()), "")
        snr = next((item for item in cells if re.search(r"\d(?:\.\d+)?\s*dB$", item)), "")
        groups[section].append((locked, power, snr))
        tables[section].append(_channel_row(section, cells))
    return {
        "ds_channels": _count(groups["ds"]),
        "ds_power": _mean_power(groups["ds"]),
        "us_channels": _count(groups["us"]),
        "us_power": _mean_power(groups["us"]),
        "mer": _mean_snr(groups["ds"]),
        "ds_rows": tables["ds"],
        "us_rows": tables["us"],
        "ds_correctable_total": correctable_total,
        "ds_uncorrectable_total": uncorrectable_total,
    }


def _band_ssids(
    host: str,
    username: str,
    password: str,
    source: str,
    device: str,
) -> tuple[str, str, str, str] | None:
    """CG-2200 keeps one primary page and switches the band with wlan24G / wlan5G."""
    page_24 = _armed_primary(host, username, password, source, device, "/wlan24G.asp")
    page_50 = _armed_primary(host, username, password, source, device, "/wlan5G.asp")
    ssid_24, key_24 = _radio(page_24)
    ssid_50, key_50 = _radio(page_50)
    if ssid_24 and ssid_24 == ssid_50:
        ssid_50, key_50 = "", ""
    if not ssid_24 and not ssid_50:
        return None
    return ssid_24, ssid_50, key_24, key_50


def _armed_primary(
    host: str,
    username: str,
    password: str,
    source: str,
    device: str,
    switch: str,
) -> str:
    _get(host, switch, username, password, source, device, timeout=8)
    status, body = _get(host, "/wlanPrimaryNetwork.asp", username, password, source, device, timeout=12)
    if status == 200 and "update in progress" in (body or "").lower():
        status, body = _get(host, "/wlanPrimaryNetwork.asp", username, password, source, device, timeout=12)
    if status != 200:
        return ""
    return body


def _radio(page: str) -> tuple[str, str]:
    return parse_status(page).get("ssid", ""), _psk(page)


def _switch_ssids(pages: dict[str, str]) -> tuple[str, str, str, str] | None:
    """CG-3000 keeps each band on its own security page. Guest SSIDs are not the primary name."""
    band_24 = pages.get("/wlswitchint1wlsecurity.wl", "")
    band_50 = pages.get("/wlswitchint0wlsecurity.wl", "")
    ssid_24 = _js_string(band_24, "ssid")
    ssid_50 = _js_string(band_50, "ssid")
    if not ssid_24 and not ssid_50:
        return None
    return ssid_24, ssid_50, _js_string(band_24, "wpaPskKey"), _js_string(band_50, "wpaPskKey")


def _js_string(html: str, name: str) -> str:
    match = re.search(rf"var\s+{re.escape(name)}\s*=\s*'([^']*)'", html or "")
    if not match:
        return ""
    value = match.group(1).strip()
    if not value or value.lower() in {"clave", "password"}:
        return ""
    return value[:64]


def _ssids(pages: list[str]) -> tuple[str, str, str, str]:
    found: list[tuple[str, str]] = []
    for page in pages:
        status = parse_status(page)
        ssid = status.get("ssid") or ""
        key = _psk(page)
        if ssid and (ssid, key) not in found:
            found.append((ssid, key))
    while len(found) < 2:
        found.append(("", ""))
    return found[0][0], found[1][0], found[0][1], found[1][1]


def _psk(html: str) -> str:
    match = re.search(
        r"Pre-Shared Key[\s\S]{0,400}?value=\"([^\"]*)\"",
        html or "",
        flags=re.I,
    )
    if not match:
        return ""
    value = match.group(1).strip()
    if not value or value.lower() in {"clave", "password"}:
        return ""
    return value[:64]


def _docsis(found: dict[str, str], link_state: str = "") -> str:
    parts = [found.get("docsis") or "", found.get("access") or "", link_state]
    return " · ".join(part for part in parts if part)


def _row_comment(html: str, label: str) -> str:
    needle = label.lower()
    for cells in _rows(html):
        if cells and needle in cells[0].lower():
            return cells[-1][:40]
    return ""


def _header_section(cells: list[str]) -> str:
    """A title row starts a table. A channel row starts with the channel number."""
    if not cells or cells[0].isdigit():
        return ""
    joined = " ".join(cells).lower()
    if "snr" in joined and "potencia" in joined:
        return "ds"
    if "tipo de canal" in joined or "simbolo" in joined or "símbolo" in joined:
        return "us"
    if "downstream" in joined or "descendente" in joined:
        return "ds"
    if "upstream" in joined or "ascendente" in joined:
        return "us"
    return ""


def _channel_row(section: str, cells: list[str]) -> dict[str, str]:
    """Keep the columns the portal prints. CG-2200 DS has nine cells and US has seven."""
    blank = {
        "channel": cells[0][:8],
        "state": "",
        "modulation": "",
        "channel_id": "",
        "frequency": "",
        "power": "",
        "snr": "",
        "correctable": "",
        "uncorrectable": "",
        "symbol_rate": "",
        "bandwidth": "",
    }
    if any("ofdm" in item.lower() for item in cells):
        blank["modulation"] = "OFDM"
        blank["state"] = (next((item for item in cells if _locked([item]) is not None), "") or "")[:40]
        hz = next((item for item in cells if re.search(r"\bhz\b", item, flags=re.I) and "khz" not in item.lower()), "")
        width = next((item for item in cells if "khz" in item.lower()), "")
        blank["frequency"] = hz[:24]
        blank["bandwidth"] = width[:24]
        blank["power"] = next((item for item in cells if "dbmv" in item.lower()), "")[:24]
        blank["snr"] = next((item for item in cells if re.search(r"\d(?:\.\d+)?\s*dB$", item)), "")[:16]
        return blank
    state = next((item for item in cells if _locked([item]) is not None), "")
    blank["state"] = state[:40]
    if section == "ds" and len(cells) >= 9:
        blank["modulation"] = cells[2][:40]
        blank["channel_id"] = cells[3][:16]
        blank["frequency"] = cells[4][:24]
        blank["power"] = cells[5][:24]
        blank["snr"] = cells[6][:16]
        blank["correctable"] = cells[7][:16]
        blank["uncorrectable"] = cells[8][:16]
        return blank
    if section == "us" and len(cells) >= 7:
        blank["modulation"] = cells[2][:40]
        blank["channel_id"] = cells[3][:16]
        blank["symbol_rate"] = cells[4][:24]
        blank["frequency"] = cells[5][:24]
        blank["power"] = cells[6][:24]
        return blank
    if section == "us" and len(cells) >= 6:
        blank["modulation"] = cells[2][:40]
        blank["channel_id"] = cells[3][:16]
        blank["frequency"] = cells[4][:24]
        blank["power"] = cells[5][:24]
        return blank
    blank["power"] = next((item for item in cells if "dbmv" in item.lower()), "")[:24]
    blank["snr"] = next((item for item in cells if re.search(r"\d(?:\.\d+)?\s*dB$", item)), "")[:16]
    blank["frequency"] = next((item for item in cells if re.search(r"\bhz\b", item, flags=re.I)), "")[:24]
    return blank


def _count(rows: list[tuple[bool, str, str]]) -> str:
    if not rows:
        return ""
    locked = sum(1 for item in rows if item[0])
    return f"{locked} sintonizados de {len(rows)}"


def _locked(cells: list[str]) -> bool | None:
    for cell in cells:
        folded = cell.lower()
        if "sintonizado" in folded or re.search(r"\blocked\b", folded):
            return "no sintonizado" not in folded and "not locked" not in folded
    return None


def _measured(rows: list[tuple[bool, str, str]]) -> list[tuple[bool, str, str]]:
    locked = [item for item in rows if item[0]]
    return locked or rows


def _mean_power(rows: list[tuple[bool, str, str]]) -> str:
    numbers = [_number(power) for _locked_row, power, _snr in _measured(rows) if _number(power) is not None]
    if not numbers:
        return ""
    return f"{sum(numbers) / len(numbers):.1f} dBmV"


def _mean_snr(rows: list[tuple[bool, str, str]]) -> str:
    """Average SNR. A locked channel that prints 0.0 dB stays visible."""
    skip_zero = any(item[0] for item in rows)
    numbers = []
    zeros = False
    for _locked_row, _power, snr in _measured(rows):
        value = _number(snr)
        if value is None:
            continue
        if skip_zero and value == 0.0:
            zeros = True
            continue
        numbers.append(value)
    if not numbers:
        return "0.0 dB" if zeros else ""
    return f"{sum(numbers) / len(numbers):.1f} dB"


def _number(text: str) -> float | None:
    match = re.search(r"-?\d+(?:\.\d+)?", text or "")
    if not match:
        return None
    return float(match.group(0))


def _rows(html: str) -> list[list[str]]:
    rows = []
    for raw in re.findall(r"<tr\b[\s\S]*?</tr>", html or "", flags=re.I):
        cells = [_cell_text(item) for item in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", raw, flags=re.I | re.S)]
        cells = [item for item in cells if item]
        if cells:
            rows.append(cells)
    return rows


def _label(text: str) -> str | None:
    folded = " ".join(text.lower().split()).rstrip(":")
    for key, names in _LABELS.items():
        if folded in names:
            return key
    return None


def _cell_text(value: str) -> str:
    text = " ".join(re.sub(r"<[^>]+>", " ", value).split())
    if text:
        return text
    match = re.search(r'value="([^"]*)"', value or "")
    if not match:
        return ""
    return match.group(1).strip()[:80]


def read_counters(
    host: str,
    username: str,
    password: str,
    source: str = "",
    device: str = "",
) -> dict[str, object]:
    """One connection page. The sum is the lifetime counter, not a test-window delta."""
    if not allowed_host(host) or not valid_user(username) or not valid_password(password):
        return {"ok": False, "ds_uncorrectables": None}
    status, body = _get(host, "/RgConnect.asp", username, password, source, device, timeout=12)
    if status != 200 or not body:
        return {"ok": False, "ds_uncorrectables": None}
    total = uncorrectable_total(parse_channels(body)["ds_rows"])
    return {"ok": total is not None, "ds_uncorrectables": total}


def uncorrectable_total(rows: list) -> int | None:
    numbers = []
    for row in rows:
        if isinstance(row, dict):
            text = str(row.get("uncorrectable") or "")
        else:
            text = str(getattr(row, "uncorrectable", "") or "")
        if text.isdigit():
            numbers.append(int(text))
    if not numbers:
        return None
    return sum(numbers)


def probe(host: str, source: str = "", device: str = "") -> tuple[int, str, str]:
    """HTTP/1.0 with no Huawei cookie. Eight seconds covers a cold ARP on the test cable."""
    status, headers, body = _exchange(host, "/", "", source, device, timeout=8)
    return status, headers.get("www-authenticate", ""), body


def _get(
    host: str,
    path: str,
    username: str,
    password: str,
    source: str,
    device: str,
    timeout: float = 6,
    referer: str = "",
) -> tuple[int, str]:
    token = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
    status, headers, body = _exchange(
        host, path, token, source, device, timeout=timeout, referer=referer
    )
    location = headers.get("location", "")
    target = location.split("?", 1)[0]
    if status in {301, 302, 303} and target in {"/", "/index.html"}:
        status, _headers, body = _exchange(
            host, path, token, source, device, timeout=timeout, referer=referer
        )
    elif status in {301, 302, 303} and target.startswith("/") and target != path:
        status, _headers, body = _exchange(
            host, target, token, source, device, timeout=timeout, referer=referer
        )
    return status, body


def _is_channel_page(html: str) -> bool:
    """The connection table. A redirect to the status home is not this page."""
    text = (html or "").lower()
    return "dbmv" in text or ("potencia" in text and "snr" in text)


def _exchange(
    host: str,
    path: str,
    token: str,
    source: str,
    device: str,
    timeout: float = 6,
    referer: str = "",
) -> tuple[int, dict[str, str], str]:
    if not path.startswith("/"):
        path = "/" + path
    data = b""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        if re.fullmatch(r"[A-Za-z0-9_.:-]{1,15}", device or ""):
            sock.setsockopt(socket.SOL_SOCKET, 25, device.encode("ascii") + b"\0")
        if source:
            sock.bind((source, 0))
        sock.connect((host, 80))
        header = f"GET {path} HTTP/1.0\r\nHost: {host}\r\nConnection: close\r\n"
        if token:
            header += f"Authorization: Basic {token}\r\n"
        if referer:
            header += f"Referer: {referer}\r\n"
        sock.sendall((header + "\r\n").encode("ascii"))
        while len(data) < 400_000:
            try:
                chunk = sock.recv(65536)
            except socket.timeout:
                break
            if not chunk:
                break
            data += chunk
            if _http_complete(data):
                break
    except OSError:
        if b"\r\n\r\n" not in data:
            return 0, {}, ""
    finally:
        sock.close()
    if b"\r\n\r\n" not in data:
        return 0, {}, ""
    head, _, raw = data.partition(b"\r\n\r\n")
    lines = head.decode("latin-1", "replace").split("\r\n")
    status = 0
    headers: dict[str, str] = {}
    if lines and len(lines[0].split()) >= 2 and lines[0].split()[1].isdigit():
        status = int(lines[0].split()[1])
    for line in lines[1:]:
        if ":" in line:
            name, value = line.split(":", 1)
            headers[name.strip().lower()] = value.strip()
    if "chunked" in headers.get("transfer-encoding", "").lower():
        raw = _unchunk(raw)
    return status, headers, raw.decode("utf-8", "replace")


def _http_complete(data: bytes) -> bool:
    head, sep, body = data.partition(b"\r\n\r\n")
    if not sep:
        return False
    headers = {}
    for line in head.decode("latin-1", "replace").split("\r\n")[1:]:
        if ":" in line:
            name, value = line.split(":", 1)
            headers[name.strip().lower()] = value.strip()
    length = headers.get("content-length", "")
    if length.isdigit():
        return len(body) >= int(length)
    if "chunked" in headers.get("transfer-encoding", "").lower():
        return body.endswith(b"0\r\n\r\n") or b"\r\n0\r\n\r\n" in body
    return False


def _unchunk(raw: bytes) -> bytes:
    parts = []
    view = raw
    while view:
        line, sep, rest = view.partition(b"\r\n")
        if not sep:
            break
        size_text = line.split(b";", 1)[0].strip()
        try:
            size = int(size_text, 16)
        except ValueError:
            return raw
        if size == 0:
            break
        if len(rest) < size:
            parts.append(rest)
            break
        parts.append(rest[:size])
        view = rest[size:]
        if view.startswith(b"\r\n"):
            view = view[2:]
    return b"".join(parts) if parts else raw
