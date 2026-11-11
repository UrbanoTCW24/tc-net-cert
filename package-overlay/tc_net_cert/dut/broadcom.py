"""Read a Kaon / Broadcom EMTA portal with HTTP Basic auth.

One password is sent. The Huawei session cookie is not used. The Wi-Fi key
is returned to the local panel only; the certificate does not print it.
This reader does not claim DOCSIS provisioning, CMTS registration, or TFTP.
"""

from __future__ import annotations

import base64
import re
import socket
from dataclasses import dataclass
from urllib.parse import urlencode

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
_CONNECT_PATHS = (
    "/cmconnectionstatus.html",
    "/RgConnect.asp",
    "/RgCableStatus.asp",
    "/cmcablestatus.html",
)
# CG-3000 can take ~15s before the first byte on cmconnectionstatus.html.
_CONNECT_PAGE_TIMEOUT = 26.0
# Lab throughput pairs LAN1 with the modem's rear ETH1 on CG-3000.
_THROUGHPUT_MODEM_PORT = "ETH1"
# CG-3000 cable_pwrmgt.cgi: getPortName maps eth3 → label ETH1 (see util.js on the modem).
_CG3000_ETH1_PHY_TOKEN = "eth3"
_ENERGY_CGI_PATH = "/cable_pwrmgt.cgi"
_ENERGY_PAGE_TAILS = (
    "cable_pwrmgt.cgi",
    "cable_pwrmgt.html",
    "cable_pwrmgt.wl",
    "ethportpowersave.wl",
    "energymanagement.wl",
)
_ENERGY_APPLY_HINTS = ("aplicar/guardar", "aplicar", "guardar", "apply", "save")
_ENERGY_SECTION_PREFIXES = ("", "/section/")
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
    for path in ("/cmswinfo.html", "/RgSwInfo.asp", "/"):
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
    brand = parse_brand(software)
    early = parse_status(software)
    commercial = commercial_model(brand, early.get("model") or early.get("hardware", ""))
    throughput_lan_prepare = ""
    for path in _PAGES[1:]:
        if path in _CONNECT_PATHS:
            continue
        status, body = _get(host, path, username, password, source, device, timeout=12)
        if status == 200 and "update in progress" in (body or "").lower():
            status, body = _get(host, path, username, password, source, device, timeout=12)
        if status == 200 and body.strip() and "update in progress" not in body.lower() and not _is_waiting_stub(body):
            pages[path] = body
    connect_html = _fetch_connection_html(host, username, password, source, device)
    if commercial == "CG-3000" or re.search(r"CG-?3000", software or "", flags=re.I):
        throughput_lan_prepare = _prepare_cg3000_throughput_lan1(
            host, username, password, source, device
        )
    if connect_html:
        for path in _CONNECT_PATHS:
            pages.setdefault(path, connect_html)
    referer = f"http://{host}/main.html"
    for path in _CONNECT_PATHS:
        if path in pages:
            continue
        status, body = _get(
            host,
            path,
            username,
            password,
            source,
            device,
            timeout=_CONNECT_PAGE_TIMEOUT if path in _CONNECT_PATHS else 10,
            referer=referer if path.endswith(".html") else "",
        )
        if status == 200 and _is_channel_page(body) and not _is_waiting_stub(body):
            pages[path] = body
    found = parse_status(software)
    channels = parse_channels(_best_connection_html(pages))
    radios = _band_ssids(host, username, password, source, device) or _switch_ssids(pages) or _ssids(list(pages.values()))
    if not found.get("serial") and not found.get("mac"):
        return _result(
            False,
            error="El EMTA abrió la sesión, pero la página no mostró el SN ni la MAC.",
            model="Kaon",
        )
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
    connect = _best_connection_html(pages)
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
    result["docsis_status"] = _docsis(found, link_state, channels)
    _status, mta = _get(host, "/MtaStatus.asp", username, password, source, device, timeout=8)
    result["mta_dhcp"] = _row_comment(mta, "dhcp")
    _status, setup = _get(host, "/RgSetup.asp", username, password, source, device, timeout=8)
    result["wan_dhcp"] = _row_comment(setup, "conexión wan")
    if throughput_lan_prepare:
        result["throughput_lan_prepare"] = throughput_lan_prepare
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
    if re.search(r"CG-?3000", parsed or "", flags=re.I):
        return "CG-3000"
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
        if not section or not cells:
            continue
        ofdm_row = any("ofdm" in item.lower() for item in cells)
        if not cells[0].isdigit() and not ofdm_row:
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


def _docsis(found: dict[str, str], link_state: str = "", channels: dict | None = None) -> str:
    parts = [found.get("docsis") or "", found.get("access") or "", link_state]
    ch = channels or {}
    ds = str(ch.get("ds_channels") or "").strip()
    us = str(ch.get("us_channels") or "").strip()
    if ds or us:
        bond = " · ".join(
            piece for piece in (f"DS {ds}" if ds else "", f"US {us}" if us else "") if piece
        )
        if bond:
            parts.append(bond)
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
    first = cells[0].lower()
    if first in ("indice de canales", "índice de canales", "channel index"):
        return ""
    if len(cells) >= 4 and ("tipo de canal" in joined or "channel type" in joined):
        return ""
    if len(cells) > 2 and "estado" not in joined and not any(
        token in joined for token in ("downstream", "upstream", "descendente", "ascendente")
    ):
        return ""
    if "snr" in joined and "potencia" in joined:
        return "ds"
    if "tipo de canal" in joined or "simbolo" in joined or "símbolo" in joined:
        return "us"
    if "downstream" in joined or "descendente" in joined or "bajada" in joined or "bajada cm" in joined:
        return "ds"
    if "upstream" in joined or "ascendente" in joined or "subida" in joined or "subida cm" in joined:
        return "us"
    if "canal" in joined and "baj" in joined:
        return "ds"
    if "canal" in joined and "sub" in joined:
        return "us"
    return ""


def _best_connection_html(pages: dict[str, str]) -> str:
    for path in _CONNECT_PATHS:
        body = pages.get(path, "")
        if body and _is_channel_page(body):
            return body
    for body in pages.values():
        if _is_channel_page(body):
            return body
    return pages.get("/RgConnect.asp", "") or pages.get("/cmconnectionstatus.html", "")


def _is_waiting_stub(html: str) -> bool:
    text = html or ""
    return "Waiting..." in text and "top.location.replace" in text


def _fetch_connection_html(host: str, username: str, password: str, source: str, device: str) -> str:
    """CG-3000 serves channels at cmconnectionstatus.html inside the framed UI (Referer required)."""
    referer = f"http://{host}/main.html"
    for attempt in range(2):
        if attempt:
            _get(host, "/cmswinfo.html", username, password, source, device, timeout=4)
        status, body = _get(
            host,
            "/cmconnectionstatus.html",
            username,
            password,
            source,
            device,
            timeout=_CONNECT_PAGE_TIMEOUT,
            referer=referer,
        )
        if status == 200 and _is_channel_page(body) and not _is_waiting_stub(body):
            return body
    status, body = _get(
        host,
        "/RgConnect.asp",
        username,
        password,
        source,
        device,
        timeout=_CONNECT_PAGE_TIMEOUT,
        referer=referer,
    )
    if status == 200 and _is_channel_page(body) and not _is_waiting_stub(body):
        return body
    for probe in ("/RgConnect.asp?",):
        status, body = _get(
            host,
            probe,
            username,
            password,
            source,
            device,
            timeout=5,
            extra_headers={"X-Requested-With": "XMLHttpRequest", "Accept": "*/*"},
        )
        if status != 200 or not body:
            continue
        cleaned = body.strip()
        if _is_channel_page(cleaned):
            return cleaned
        if cleaned and not cleaned.lstrip().startswith("<") and len(cleaned) < 200:
            cookie = f"credential={cleaned.splitlines()[0].strip()}"
            status, body = _get(
                host,
                "/RgConnect.asp",
                username,
                password,
                source,
                device,
                timeout=8,
                referer=referer,
                cookie=cookie,
            )
            if status == 200 and _is_channel_page(body) and not _is_waiting_stub(body):
                return body
    return ""


@dataclass(frozen=True)
class _PortToggle:
    label: str
    name: str
    enabled: bool
    value: str


def prepare_cg3000_throughput_lan(
    host: str,
    username: str,
    password: str,
    source: str,
    device: str,
) -> str:
    """Enable modem ETH1 (CG-3000 power saving). Lab LAN1 is cabled to that port."""
    return _prepare_cg3000_throughput_lan1(host, username, password, source, device)


def _prepare_cg3000_throughput_lan1(
    host: str,
    username: str,
    password: str,
    source: str,
    device: str,
) -> str:
    """Ensure modem ETH1 is enabled; lab throughput LAN1 is cabled to that port."""
    referer = f"http://{host}/main.html"
    status, html = _get(host, _ENERGY_CGI_PATH, username, password, source, device, timeout=10, referer=referer)
    if status == 200 and _parse_cg3000_enabled_ports(html):
        return _prepare_cg3000_energy_cgi(
            host, username, password, source, device, html, path=_ENERGY_CGI_PATH, referer=referer
        )
    path, html = _fetch_energy_page(host, username, password, source, device)
    if not path or not html:
        return "No se encontró Administración de energía; revisa ETH1 manualmente."
    if _parse_cg3000_enabled_ports(html):
        return _prepare_cg3000_energy_cgi(host, username, password, source, device, html, path=path, referer=referer)
    toggles, action, hidden, submit = _parse_energy_form(html)
    if not toggles:
        return "La página de energía no mostró puertos ETH; revisa ETH1 manualmente."
    eth1 = _find_port_toggle(toggles, _THROUGHPUT_MODEM_PORT)
    if not eth1:
        return "No se identificó ETH1 en energía; revisa LAN1 del throughput manualmente."
    if eth1.enabled and verify_cg3000_eth1_enabled(html):
        return f"{_THROUGHPUT_MODEM_PORT} verificado activo — listo para LAN1 del throughput."
    body = _energy_apply_body(
        toggles,
        force_enabled={_THROUGHPUT_MODEM_PORT},
        hidden=hidden,
        submit=submit,
        html=html,
    )
    if not body:
        return "No se pudo armar el formulario de energía; marca ETH1 y pulsa Aplicar/Guardar en el portal."
    post_path = action or path
    post_referer = f"http://{host}{path}"
    status, response = _post(host, post_path, username, password, source, device, body, referer=post_referer)
    if status not in {200, 204, 302}:
        return f"No se pudo aplicar energía (HTTP {status}); marca {_THROUGHPUT_MODEM_PORT} y Aplicar/Guardar."
    _status, verify_html = _get(host, path, username, password, source, device, timeout=12, referer=post_referer)
    if verify_cg3000_eth1_enabled(verify_html or response or ""):
        return (
            f"{_THROUGHPUT_MODEM_PORT} activado y verificado (casilla marcada tras Aplicar/Guardar). "
            "LAN1 del banco puede negociar enlace."
        )
    return (
        f"FALLO: {_THROUGHPUT_MODEM_PORT} sigue desmarcado tras Aplicar/Guardar. "
        "En Configuración avanzada → Administración de energía, marca ETH1 y guarda."
    )


def _parse_cg3000_enabled_ports(html: str) -> str:
    match = re.search(r"var\s+enabledPorts\s*=\s*'([^']*)'", html or "", flags=re.I)
    return match.group(1).strip() if match else ""


def _cg3000_phy_enabled(html: str, phy_token: str) -> bool:
    raw = _parse_cg3000_enabled_ports(html)
    if not raw:
        return False
    for segment in raw.split("|"):
        key, _, val = segment.partition(":")
        if key.strip().lower() == phy_token.lower():
            return val.strip() == "1"
    return False


def _cg3000_patch_enabled_ports(raw: str, phy_token: str, enabled: bool) -> str:
    parts: list[str] = []
    for segment in raw.split("|"):
        if not segment.strip():
            continue
        key, sep, val = segment.partition(":")
        if not sep:
            parts.append(segment)
            continue
        if key.strip().lower() == phy_token.lower():
            val = "1" if enabled else "0"
        parts.append(f"{key.strip()}:{val.strip()}")
    return "|".join(parts)


def _prepare_cg3000_energy_cgi(
    host: str,
    username: str,
    password: str,
    source: str,
    device: str,
    html: str,
    *,
    path: str,
    referer: str,
) -> str:
    """POST enabledPorts to cable_pwrmgt.cgi (checkboxes are built by document.write in the browser)."""
    raw = _parse_cg3000_enabled_ports(html)
    if not raw:
        return "No se leyó enabledPorts en Administración de energía."
    if _cg3000_phy_enabled(html, _CG3000_ETH1_PHY_TOKEN):
        return (
            f"{_THROUGHPUT_MODEM_PORT} verificado activo (eth3:1 en cable_pwrmgt.cgi) — listo para LAN1."
        )
    patched = _cg3000_patch_enabled_ports(raw, _CG3000_ETH1_PHY_TOKEN, True)
    fields = {"enabledPorts": patched, "path_name": "cable_pwrmgt.html"}
    body = urlencode(fields).encode("latin-1", errors="replace")
    post_path = path if path.startswith("/") else f"/{path}"
    if not post_path.endswith(".cgi"):
        post_path = _ENERGY_CGI_PATH
    headers = {"Content-Type": "application/x-www-form-urlencoded; charset=windows-1252"}
    referer_cgi = f"http://{host}{_ENERGY_CGI_PATH}"
    status, response = _post(
        host,
        post_path,
        username,
        password,
        source,
        device,
        body,
        referer=referer_cgi,
        extra_headers=headers,
    )
    if status not in {200, 204, 302}:
        return f"No se pudo aplicar energía en cable_pwrmgt.cgi (HTTP {status})."
    _status, verify_html = _get(
        host, _ENERGY_CGI_PATH, username, password, source, device, timeout=12, referer=referer_cgi
    )
    if _cg3000_phy_enabled(verify_html or response, _CG3000_ETH1_PHY_TOKEN):
        return (
            f"{_THROUGHPUT_MODEM_PORT} activado y verificado (eth3:1 tras Aplicar/Guardar vía CGI). "
            "LAN1 del banco puede negociar enlace."
        )
    return (
        f"FALLO: {_THROUGHPUT_MODEM_PORT} sigue apagado (eth3:0). "
        "Marca ETH1 en Administración de energía y guarda manualmente."
    )


def _fetch_energy_page(
    host: str,
    username: str,
    password: str,
    source: str,
    device: str,
) -> tuple[str, str]:
    referer = f"http://{host}/main.html"
    for prefix in _ENERGY_SECTION_PREFIXES:
        for tail in _ENERGY_PAGE_TAILS:
            path = f"{prefix}{tail}" if prefix else f"/{tail}"
            if not path.startswith("/"):
                path = "/" + path
            status, body = _get(host, path, username, password, source, device, timeout=6, referer=referer)
            if status == 200 and _is_energy_page(body):
                return path, body
    return "", ""


def verify_cg3000_eth1_enabled(html: str) -> bool:
    """True when ETH1 is enabled (CG-3000 CGI eth3:1 or static checkbox)."""
    if _parse_cg3000_enabled_ports(html):
        return _cg3000_phy_enabled(html, _CG3000_ETH1_PHY_TOKEN)
    toggles, _, _, _ = _parse_energy_form(html or "")
    eth1 = _find_port_toggle(toggles, _THROUGHPUT_MODEM_PORT)
    return bool(eth1 and eth1.enabled)


def _energy_disable_when_checked(html: str) -> bool:
    """When True, a checked box means the port is OFF. Kaon CG-3000 is the opposite."""
    text = re.sub(r"\s+", " ", (html or "").lower())
    if "desmarque" in text and "deshabilitar" in text:
        return False
    if "uncheck" in text and "disable" in text:
        return False
    if re.search(r"marque.*habilit|check.*enable", text):
        return False
    if re.search(r"casilla.*para\s+deshabilit|check.*to\s+disable", text):
        return True
    return False


def _is_energy_page(html: str) -> bool:
    text = (html or "").lower()
    if _parse_cg3000_enabled_ports(html):
        return True
    if _THROUGHPUT_MODEM_PORT.lower() not in text:
        return False
    if any(hint in text for hint in ("administracion de energia", "administração de energia", "energy management")):
        return True
    if "deshabilitar puertos" in text or "disable ports" in text:
        return True
    return "eth2" in text and ("wifi" in text or "wi-fi" in text) and "aplicar" in text


def _parse_energy_form(html: str) -> tuple[list[_PortToggle], str, dict[str, str], tuple[str, str] | None]:
    forms = re.findall(r"<form\b([\s\S]*?)</form>", html or "", flags=re.I)
    blocks = forms or [html or ""]
    for block in blocks:
        action = ""
        match = re.search(r'action\s*=\s*"([^"]*)"', block, flags=re.I)
        if match:
            action = match.group(1).strip()
        hidden: dict[str, str] = {}
        toggles: list[_PortToggle] = []
        for tag in re.findall(r"<input\b[^>]*>", block, flags=re.I):
            attrs = dict(re.findall(r'(\w+)\s*=\s*"([^"]*)"', tag, flags=re.I))
            name = (attrs.get("name") or "").strip()
            if not name:
                continue
            kind = (attrs.get("type") or "text").lower()
            value = (attrs.get("value") or "on").strip()
            if kind == "hidden":
                hidden[name] = value
                continue
            if kind != "checkbox":
                continue
            label = _checkbox_label(block, tag)
            if not label:
                label = name
            checked = bool(re.search(r"\bchecked\b", tag, flags=re.I))
            disable_mode = _energy_disable_when_checked(block) or _energy_disable_when_checked(html)
            port_live = (not checked) if disable_mode else checked
            toggles.append(_PortToggle(label=label, name=name, enabled=port_live, value=value))
        if toggles:
            submit = _parse_submit_button(block)
            return toggles, action, hidden, submit
    toggles = _parse_energy_checkboxes_loose(html)
    if toggles:
        return toggles, "", {}, _parse_submit_button(html or "")
    return [], "", {}, None


def _parse_energy_checkboxes_loose(html: str) -> list[_PortToggle]:
    toggles: list[_PortToggle] = []
    for tag in re.findall(r"<input\b[^>]*type\s*=\s*[\"']?checkbox[\"']?[^>]*>", html or "", flags=re.I):
        attrs = dict(re.findall(r'(\w+)\s*=\s*"([^"]*)"', tag, flags=re.I))
        name = (attrs.get("name") or attrs.get("id") or "").strip()
        if not name:
            continue
        label = _checkbox_label(html, tag)
        if not label and not re.search(r"eth|wifi", name, flags=re.I):
            continue
        checked = bool(re.search(r"\bchecked\b", tag, flags=re.I))
        value = (attrs.get("value") or "on").strip()
        disable_mode = _energy_disable_when_checked(html)
        port_live = (not checked) if disable_mode else checked
        toggles.append(_PortToggle(label=label or name, name=name, enabled=port_live, value=value))
    return toggles


def _checkbox_label(html: str, tag: str) -> str:
    match = re.search(r'\bid\s*=\s*"([^"]+)"', tag, flags=re.I)
    if match:
        ident = match.group(1)
        for pattern in (
            rf'<label[^>]*\sfor\s*=\s*"{re.escape(ident)}"[^>]*>(.*?)</label>',
            rf"<label[^>]*>(.*?)</label>\s*{re.escape(tag)}",
            rf"{re.escape(tag)}\s*<label[^>]*>(.*?)</label>",
        ):
            hit = re.search(pattern, html or "", flags=re.I | re.S)
            if hit:
                return _cell_text(hit.group(1)).strip()[:24]
    neighbor = re.search(re.escape(tag) + r"[^<]{0,80}", html or "", flags=re.I)
    if neighbor and re.search(r"eth|wifi", neighbor.group(0), flags=re.I):
        text = re.sub(r"<[^>]+>", " ", neighbor.group(0))
        token = re.search(r"(?:ETH\d|WiFi\s*[\d.]+\s*G)", text, flags=re.I)
        if token:
            return token.group(0).upper().replace(" ", " ")
    for row in re.findall(r"<tr\b[\s\S]*?</tr>", html or "", flags=re.I):
        if tag in row:
            text = _cell_text(row)
            token = re.search(r"(?:ETH\d|WiFi\s*[\d.]+\s*G)", text, flags=re.I)
            if token:
                return token.group(0).upper().replace(" ", " ")
    return ""


def _parse_submit_button(block: str) -> tuple[str, str] | None:
    for tag in re.findall(r"<input\b[^>]*>", block or "", flags=re.I):
        if not re.search(r'\btype\s*=\s*"(?:submit|button)"', tag, flags=re.I):
            continue
        attrs = dict(re.findall(r'(\w+)\s*=\s*"([^"]*)"', tag, flags=re.I))
        name = (attrs.get("name") or "apply").strip()
        value = (attrs.get("value") or "Apply").strip()
        if re.search(r"aplicar|guardar|apply|save", value, flags=re.I) or re.search(
            r"aplicar|guardar|apply|save", tag, flags=re.I
        ):
            return name, value
    match = re.search(
        r"<button\b[^>]*>([^<]*(?:aplicar|guardar|apply|save)[^<]*)</button>",
        block or "",
        flags=re.I,
    )
    if match:
        return "apply", _cell_text(match.group(1)).strip() or "Apply"
    return ("apply", "Apply")


def _find_port_toggle(toggles: list[_PortToggle], label: str) -> _PortToggle | None:
    needle = re.sub(r"\s+", "", label.lower())
    for item in toggles:
        folded = re.sub(r"\s+", "", item.label.lower())
        if folded == needle or folded.endswith(needle) or needle in folded:
            return item
    return None


def _energy_apply_body(
    toggles: list[_PortToggle],
    *,
    force_enabled: set[str],
    hidden: dict[str, str],
    submit: tuple[str, str] | None,
    html: str = "",
) -> bytes:
    fields: dict[str, str] = dict(hidden)
    disable_mode = _energy_disable_when_checked(html)
    for item in toggles:
        label = re.sub(r"\s+", "", item.label.upper())
        must_enable = any(re.sub(r"\s+", "", token.upper()) in label for token in force_enabled)
        if disable_mode:
            if must_enable:
                continue
            if item.enabled:
                continue
            fields[item.name] = item.value or "on"
            continue
        keep_enabled = item.enabled or must_enable
        if keep_enabled:
            fields[item.name] = item.value or "on"
    if submit:
        fields[submit[0]] = submit[1]
    if len(fields) <= len(hidden):
        return b""
    return urlencode(fields).encode("ascii")


def _eth1_marked_enabled(html: str) -> bool:
    toggles, _, _, _ = _parse_energy_form(html or "")
    eth1 = _find_port_toggle(toggles, _THROUGHPUT_MODEM_PORT)
    return bool(eth1 and eth1.enabled)


def _connection_paths_from_menu(
    host: str,
    username: str,
    password: str,
    source: str,
    device: str,
) -> list[str]:
    hints = ("connect", "connection", "cable", "channel", "modem", "rf")
    paths: list[str] = []
    for page in ("/main.html", "/index.html", "/RgSwInfo.asp", "/cmswinfo.html"):
        status, body = _get(host, page, username, password, source, device, timeout=10, referer=f"http://{host}/")
        if status != 200 or not body or _is_waiting_stub(body):
            continue
        for token in re.findall(r'(?:href|src)\s*=\s*"([^"]+)"', body, flags=re.I):
            folded = token.lower()
            if not any(hint in folded for hint in hints):
                continue
            path = token.split("?", 1)[0].strip()
            if not path.startswith("/"):
                continue
            paths.append(path)
    return paths


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
    cookie: str = "",
    extra_headers: dict[str, str] | None = None,
) -> tuple[int, str]:
    token = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
    status, headers, body = _exchange(
        host,
        path,
        token,
        source,
        device,
        timeout=timeout,
        referer=referer,
        cookie=cookie,
        extra_headers=extra_headers,
        method="GET",
    )
    location = headers.get("location", "")
    target = location.split("?", 1)[0]
    redirect_kwargs = {
        "timeout": timeout,
        "referer": referer,
        "cookie": cookie,
        "extra_headers": extra_headers,
    }
    if status in {301, 302, 303} and target in {"/", "/index.html"}:
        status, _headers, body = _exchange(host, path, token, source, device, **redirect_kwargs)
    elif status in {301, 302, 303} and target.startswith("/") and target != path:
        status, _headers, body = _exchange(host, target, token, source, device, **redirect_kwargs)
    return status, body


def _post(
    host: str,
    path: str,
    username: str,
    password: str,
    source: str,
    device: str,
    data: bytes,
    timeout: float = 12,
    referer: str = "",
    cookie: str = "",
    extra_headers: dict[str, str] | None = None,
) -> tuple[int, str]:
    token = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    merged = {**(extra_headers or {}), **headers}
    status, _headers, body = _exchange(
        host,
        path,
        token,
        source,
        device,
        timeout=timeout,
        referer=referer,
        cookie=cookie,
        extra_headers=merged,
        method="POST",
        body=data,
    )
    return status, body


def _is_channel_page(html: str) -> bool:
    """The connection table. A redirect to the status home is not this page."""
    if _is_waiting_stub(html):
        return False
    text = (html or "").lower()
    if "dbmv" in text or "dpmv" in text:
        return True
    if "potencia" in text and ("snr" in text or "mer" in text):
        return True
    if "downstream" in text and "upstream" in text and ("canal" in text or "channel" in text):
        return True
    return False


def _exchange(
    host: str,
    path: str,
    token: str,
    source: str,
    device: str,
    timeout: float = 6,
    referer: str = "",
    cookie: str = "",
    extra_headers: dict[str, str] | None = None,
    method: str = "GET",
    body: bytes = b"",
) -> tuple[int, dict[str, str], str]:
    if not path.startswith("/"):
        path = "/" + path
    verb = (method or "GET").upper()
    data = b""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        if re.fullmatch(r"[A-Za-z0-9_.:-]{1,15}", device or ""):
            sock.setsockopt(socket.SOL_SOCKET, 25, device.encode("ascii") + b"\0")
        if source:
            sock.bind((source, 0))
        sock.connect((host, 80))
        header = f"{verb} {path} HTTP/1.0\r\nHost: {host}\r\nConnection: close\r\n"
        if token:
            header += f"Authorization: Basic {token}\r\n"
        if referer:
            header += f"Referer: {referer}\r\n"
        if cookie:
            header += f"Cookie: {cookie}\r\n"
        for name, value in (extra_headers or {}).items():
            if name and value:
                header += f"{name}: {value}\r\n"
        if verb == "POST" and body:
            header += f"Content-Length: {len(body)}\r\n"
        sock.sendall((header + "\r\n").encode("ascii"))
        if verb == "POST" and body:
            sock.sendall(body)
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
