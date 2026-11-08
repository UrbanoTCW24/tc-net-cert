"""Read the serial and MAC from a Huawei ONT web page.

The HG8145X6-10 path is the one on its own pages: one token from
GetRandCount.asp, one POST to login.cgi, then index.asp and Device
Information. The password is the same base64 the page uses. It is never
printed. A failed login does not try again.
"""

from __future__ import annotations

import http.client
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from base64 import b64encode
from html import unescape

JOB_ENV = "TC_NET_CERT_JOB"
USER_ENV = "TC_NET_CERT_DUT_USER"
PASSWORD_ENV = "TC_NET_CERT_DUT_PASSWORD"
HOST_ENV = "TC_NET_CERT_DUT_HOST"
SERIAL_ENV = "TC_NET_CERT_DUT_SERIAL"
MAC_ENV = "TC_NET_CERT_DUT_MAC"
BRAND_ENV = "TC_NET_CERT_DUT_BRAND"
MODEL_ENV = "TC_NET_CERT_DUT_MODEL"
PORTALS_ENV = "TC_NET_CERT_PORTALS"
JOB_RE = re.compile(r"^JOB-[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*$")
_USER_RE = re.compile(r"^[A-Za-z0-9._@-]{1,32}$")
_IPV4 = re.compile(r"^(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)$")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9+/=_-]{1,128}$")
_MAC_RE = re.compile(r"\b(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}\b")
_BARE_MAC_RE = re.compile(r"\b[0-9A-Fa-f]{12}\b")
_HWTC_RE = re.compile(r"\bHWTC[A-Z0-9]{4,16}\b", re.IGNORECASE)
_HEX_SN_RE = re.compile(r"\b48575443[0-9A-Fa-f]{8}\b", re.IGNORECASE)
_LABELS = {"SERIALNUMBER", "HARDWAREVERSION", "SOFTWAREVERSION", "MODELNAME", "VENDORID"}
_BROWSER = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
_PAGES = (
    "/index.asp",
    "/html/ssmp/deviceinfo/deviceinfo.asp",
    "/html/ssmp/deviceinfo/devinfo.asp",
    "/html/amp/common/devinfo.asp",
    "/html/bbsp/common/devinfo.asp",
)
_ASP_RE = re.compile(r"""['"](/[^'"]+?\.asp(?:\?[^'"]*)?)['"]""", re.IGNORECASE)
_ASSIGN_RE = re.compile(r"\b(ProductName|CfgMode)\s*=\s*'([^']*)'")
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$")
_SHA256_MODES = {"DVODACOM2WIFI"}


def normalize_job(value: str) -> str:
    text = value.strip()
    if text.lower().startswith("job-"):
        text = "JOB-" + text[4:]
    return text


def valid_job(value: str) -> bool:
    return bool(JOB_RE.fullmatch(value)) and len(value) <= 32


def valid_user(value: str) -> bool:
    return bool(_USER_RE.fullmatch(value))


def valid_password(value: str) -> bool:
    return 1 <= len(value) <= 127 and "\n" not in value and "\r" not in value and "\x00" not in value


def allowed_host(host: str) -> bool:
    if not _IPV4.fullmatch(host):
        return False
    first = int(host.split(".", 1)[0])
    return first not in {0, 127} and first < 224


def huawei_password(password: str) -> str:
    raw = bytes(ord(char) & 0xFF for char in password)
    return b64encode(raw).decode("ascii")


def _constructor_values(text: str, name: str) -> list[str]:
    """Quoted arguments of one `new Name(...)` call. A parenthesis inside quotes stays in the value."""
    match = re.search(rf"new\s+{re.escape(name)}\s*\(", text, re.IGNORECASE)
    if not match:
        return []
    values: list[str] = []
    quote = ""
    current: list[str] = []
    for char in text[match.end():]:
        if quote:
            if char == quote:
                values.append("".join(current))
                current = []
                quote = ""
            else:
                current.append(char)
            continue
        if char in {'"', "'"}:
            quote = char
        elif char == ")":
            break
    return values


def parse_basic_info(html: str) -> dict[str, str]:
    """Fields from System Information → Basic Information (stDeviceInfo)."""
    text = _js_text(html or "")
    values = _constructor_values(text, "stDeviceInfo")
    if not values:
        serial, mac = parse_device_page(html or "")
        return {"serial": serial, "mac": mac, "model": "", "software": "", "hardware": ""}

    def field(index: int) -> str:
        return values[index].strip() if len(values) > index else ""

    mac = next((item for item in values if _MAC_RE.fullmatch(item)), "")
    return {
        "serial": field(1),
        "hardware": field(2),
        "software": field(3),
        "model": field(4),
        "mac": ":".join(piece.lower() for piece in mac.split(":")) if mac else "",
        "device_type": field(4),
        "vendor_id": field(5)[:16],
        "release_time": field(6).replace("_", " ").strip()[:32],
        "description": " ".join(field(8).split())[:120],
    }


_OPTIC_KEYS = ("ont_state", "tx_power", "rx_power", "voltage", "bias", "temperature")
_ONT_STATE = re.compile(r"^O[1-7]$", re.IGNORECASE)
_WLAN_KEYS = (
    "wlan_status",
    "wlan_channel",
    "ssid_24",
    "ssid_50",
    "wifi_password_24",
    "wifi_password_50",
)
_INVENTORY_PAGES = (
    "/html/amp/opticinfo/opticinfo.asp",
    "/html/bbsp/common/ontstate.asp",
    "/html/bbsp/common/wan_list_cache_wan.asp",
    "/asp/getMenuArray.asp",
    "/html/amp/wlaninfo/wlaninfo.asp",
    "/html/amp/wlanbasic/WlanBasic.asp",
    "/html/amp/wlanbasic/simplewificfg.asp",
    "/html/amp/wlanbasic/wlanpsk.asp",
)


def parse_optic(html: str) -> dict[str, str]:
    """TX/RX power, voltage, bias and temperature from Optical Information."""
    text = _js_text(html or "")
    found = {key: "" for key in _OPTIC_KEYS}
    match = re.search(r"new\s+stOpticInfo\s*\((.*?)\)", text, re.DOTALL | re.IGNORECASE)
    if match:
        values = _quoted(match.group(1))
        values = [item for item in values if "InternetGateway" not in item]
        # Link status (O1–O7), TX, RX, voltage, temperature, bias
        if len(values) >= 6:
            found["ont_state"] = _ont_state(values[0])
            found["tx_power"] = _optic_value(values[1], "dBm")
            found["rx_power"] = _optic_value(values[2], "dBm")
            found["voltage"] = _optic_value(values[3], "mV")
            found["temperature"] = _optic_value(values[4], "C")
            found["bias"] = _optic_value(values[5], "mA")
            if not found["ont_state"] and values[0].strip().lower() == "ok":
                found["optic_link"] = "OK"
        if len(values) >= 13:
            found["optic_vendor"] = " ".join(values[8].split())[:24]
            found["optic_module"] = " ".join(values[9].split())[:32]
            found["tx_wavelength"] = _optic_value(values[11], "nm")
            found["rx_wavelength"] = _optic_value(values[12], "nm")
        if len(values) >= 15:
            distance = values[13].strip()
            if re.fullmatch(r"\d+", distance) and distance != "0":
                found["optic_distance"] = f"{distance} km"
            if values[14].strip() == "0":
                found["los_status"] = "LOS Off"
            elif values[14].strip() == "1":
                found["los_status"] = "LOS On"
    if not found["ont_state"]:
        visible = unescape(re.sub(r"<[^>]+>", " ", text))
        match = re.search(r"\b(O[1-7])\b", visible, re.IGNORECASE)
        if match:
            found["ont_state"] = match.group(1).upper()
    return found


def _ont_state(value: str) -> str:
    text = value.strip().upper()
    return text if _ONT_STATE.fullmatch(text) else ""


def parse_ont_status(html: str) -> dict[str, str]:
    """ONU id, O1–O7 and uptime from the device page. O5 is the operation state."""
    text = _js_text(html or "")
    found = {"ont_id": "", "ont_state": "", "pon_mode": "", "uptime": ""}
    match = re.search(r"new\s+ONTInfo\s*\((.*?)\)", text, re.DOTALL | re.IGNORECASE)
    if match:
        values = [item for item in _quoted(match.group(1)) if "InternetGateway" not in item]
        if len(values) >= 2:
            found["ont_id"] = values[0].strip()[:16]
            found["ont_state"] = _ont_state(values[1])
    mode = re.search(r"\bontPonMode\s*=\s*'([^']+)'", text, re.IGNORECASE)
    if mode:
        found["pon_mode"] = mode.group(1).strip().upper()[:16]
    uptime = re.search(r"\bdev_uptime\s*=\s*'(\d+)'", text)
    if uptime:
        found["uptime"] = _clock(uptime.group(1))
    return found


def parse_provision(body: str) -> dict[str, str]:
    """Service Provisioning Status. The portal returns device, OLT, EMS and ACS codes."""
    parts = [item.strip() for item in _js_text(body or "").replace("\r", "").split(",")]
    if len(parts) < 5 or "getdevicestatus" not in parts[0].lower():
        return {"registration": "", "olt_service": "", "ems_status": "", "acs_status": ""}
    device, olt, ems, acs = parts[1], parts[2].lower(), parts[3], parts[4]
    found = {"registration": "", "olt_service": "", "ems_status": "", "acs_status": ""}
    if device and device != "0":
        found["registration"] = "Sin registro en la OLT"
    elif olt == "0x0":
        found["registration"] = "Registrada en la OLT"
        found["olt_service"] = "Servicio de la OLT configurado"
    elif olt == "0xffffffff":
        found["registration"] = "Registrada en la OLT"
        found["olt_service"] = "La OLT aún aplica la configuración"
    elif olt:
        found["registration"] = "Registrada en la OLT"
        found["olt_service"] = "Falló la configuración de la OLT"
    ems_text = {
        "0": "EMS configurado",
        "1": "EMS aplicando configuración",
        "2": "Sin configuración XML",
    }
    acs_text = {
        "0": "Registrada en el ACS",
        "1": "En espera del ACS",
        "2": "Registrándose en el ACS",
    }
    if device in {"", "0"}:
        found["ems_status"] = ems_text.get(ems, "Falló el EMS" if ems else "")
        found["acs_status"] = acs_text.get(acs, "Falló el registro en el ACS" if acs else "")
    return found


def parse_wan(html: str) -> list[dict[str, str]]:
    """Connected WAN services. The IPoE password field is never kept."""
    text = _js_text(html or "")
    rows: list[dict[str, str]] = []
    for match in re.finditer(r"new\s+WanIP\s*\((.*?)\)", text, re.DOTALL | re.IGNORECASE):
        values = _quoted(match.group(1))
        if len(values) < 27:
            continue
        name = values[8].strip()
        if not name or "sensitive" in name.lower():
            continue
        vlan = values[21].strip()
        if not re.fullmatch(r"\d{1,4}", vlan) or int(vlan) > 4094:
            vlan = ""
        status = values[5].strip()
        if status.lower() == "connected":
            status = "Conectado"
        elif status.lower() == "disconnected":
            status = "Desconectado"
        rows.append(
            {
                "name": name[:24],
                "service": values[26].strip().replace("_", " ")[:32],
                "status": status[:24],
                "mode": values[14].strip()[:16],
                "vlan": vlan,
                "address": values[15].strip()[:40],
                "gateway": values[17].strip()[:40],
                "nat": "NAT" if values[18].strip() == "1" else ("Sin NAT" if values[18].strip() == "0" else ""),
                "uptime": _clock(values[43]) if len(values) > 43 else "",
            }
        )
        if len(rows) >= 8:
            break
    return rows


def _clock(value: str) -> str:
    text = (value or "").strip()
    if not text.isdigit():
        return ""
    total = int(text)
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)
    clock = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{days}d {clock}" if days else clock


def _optic_value(value: str, unit: str) -> str:
    text = value.strip()
    if text in {"", "--", "nok"}:
        return text
    if re.fullmatch(r"[-+]?\d+(?:\.\d+)?", text):
        return f"{text} {unit}"
    return text


_WIFI_CHANNELS = {*(range(1, 15)), 36, 40, 44, 48, 52, 56, 60, 64, 100, 104, 108, 112, 116, 120, 124, 128, 132, 136, 140, 144, 149, 153, 157, 161, 165}
_NOT_SSID = {
    "11a", "11b", "11g", "11n", "11ac", "11ax", "11be", "11bgn", "11bg", "11gn", "11na", "11ng",
    "none", "basic", "wpa", "wpa2", "wpa3", "11i", "wpaand11i", "wpanda11i",
    "aes", "tkip", "wep", "aesencryption", "tkipencryption", "tkipandaesencryption",
    "open", "shared", "psk", "sae", "auto", "enabled", "disabled", "mixed",
    "ht20", "ht40", "ht80", "vht80", "wpaauthentication", "pskauthentication",
}


def parse_wlan(html: str) -> dict[str, str]:
    """WLAN status, channel, both SSIDs, and the 2.4 / 5 GHz keys from Wi-Fi configuration."""
    text = _js_text(html or "")
    found = {key: "" for key in _WLAN_KEYS}
    radios: dict[str, dict[str, str]] = {"24": {}, "50": {}}
    for match in re.finditer(
        r"new\s+(stWlanWifi|stWlanInfo|stWlan|stPreSharedKey|stWlanKey)\s*\((.*?)\)",
        text,
        re.DOTALL | re.IGNORECASE,
    ):
        name = match.group(1).lower()
        values = _quoted(match.group(2))
        if len(values) < 2:
            continue
        band = _radio_band(values[0]) or _mode_band(values[4].lower() if len(values) > 4 else "")
        if name in {"stpresharedkey", "stwlankey"}:
            if band:
                _assign_key(radios[band], values)
            continue
        if not band:
            continue
        radio = radios[band]
        if len(values) > 2 and values[2] in {"0", "1"}:
            radio["enable"] = values[2]
        ssid = _ssid_from(values)
        if ssid and "ssid" not in radio:
            radio["ssid"] = ssid
        channel = _known_channel(values)
        if channel and "channel" not in radio:
            radio["channel"] = channel
    _html_wifi(html or "", radios)
    parts_status = []
    parts_channel = []
    for band, title in (("24", "2.4"), ("50", "5")):
        radio = radios[band]
        if radio.get("ssid"):
            found[f"ssid_{band}"] = radio["ssid"]
        if radio.get("password"):
            found[f"wifi_password_{band}"] = radio["password"]
        if radio.get("enable"):
            parts_status.append(f"{title} {'Enabled' if radio['enable'] == '1' else 'Disabled'}")
        if radio.get("channel"):
            parts_channel.append(f"{title}: {radio['channel']}")
    found["wlan_status"] = " · ".join(parts_status)
    found["wlan_channel"] = " · ".join(parts_channel)
    return found


def _known_channel(values: list[str]) -> str:
    for index in (5, 13):
        if len(values) > index and _wifi_channel(values[index]):
            return values[index]
    return next((item for item in values[1:] if _wifi_channel(item)), "")


def _wifi_channel(value: str) -> bool:
    return bool(re.fullmatch(r"\d{1,3}", value)) and int(value) in _WIFI_CHANNELS


def _ssid_from(values: list[str]) -> str:
    """Prefer the SSID slot. A 2-letter country code such as GT is not a network name."""
    for index in (5, 3, 2):
        if len(values) > index and _looks_ssid(values[index]):
            return values[index]
    return _first_ssid(values)


def _first_ssid(values: list[str]) -> str:
    return next((item for item in values[1:] if _looks_ssid(item)), "")


def _assign_key(radio: dict[str, str], values: list[str]) -> None:
    if radio.get("password"):
        return
    secret = next((item for item in values[1:] if _looks_psk(item)), "")
    if secret:
        radio["password"] = secret


def _html_wifi(html: str, radios: dict[str, dict[str, str]]) -> None:
    ssids: list[str] = []
    keys: list[str] = []
    for tag in re.findall(r"(?is)<input\b[^>]*>", html):
        ident = (_attr(tag, "id") + " " + _attr(tag, "name")).lower()
        value = _attr(tag, "value")
        if not value:
            continue
        if any(word in ident for word in ("ssid", "wlanssid", "wifiname")) and _looks_ssid(value) and value not in ssids:
            ssids.append(value)
        elif any(word in ident for word in ("psk", "preshared", "wpakey", "wlanpwd", "txt_password", "wifi")) and _looks_psk(value):
            if value not in keys:
                keys.append(value)
    for band, ssid in zip(("24", "50"), ssids):
        radios[band].setdefault("ssid", ssid)
    for band, secret in zip(("24", "50"), keys):
        radios[band].setdefault("password", secret)


def _attr(tag: str, name: str) -> str:
    match = re.search(rf'(?is)\b{name}\s*=\s*"([^"]*)"', tag) or re.search(rf"(?is)\b{name}\s*=\s*'([^']*)'", tag)
    return unescape(match.group(1)).strip() if match else ""


def _quoted(blob: str) -> list[str]:
    return [left or right for left, right in re.findall(r"'([^']*)'|\"([^\"]*)\"", blob)]


def _mode_band(mode: str) -> str:
    if mode in {"11ac", "11a", "11na", "11ax"}:
        return "50"
    if mode in {"11b", "11g", "11bg", "11bgn", "11gn", "11n"}:
        return "24"
    return ""


def _radio_band(blob: str) -> str:
    low = blob.lower()
    if re.search(r"wlanconfiguration\.5\b|5\s*ghz|5g\b|5\.0", low):
        return "50"
    if re.search(r"wlanconfiguration\.1\b|2\.4|2\s*ghz", low):
        return "24"
    return ""


def _looks_ssid(value: str) -> bool:
    if not value or len(value) > 32 or len(value) < 3 or "InternetGateway" in value:
        return False
    if re.fullmatch(r"\d{1,3}", value) or value in {"0", "1"}:
        return False
    if re.fullmatch(r"[A-Za-z]{2}", value):
        return False
    if _MAC_RE.fullmatch(value) or re.fullmatch(r"(?:2\.4|5(?:\.0)?)\s*ghz", value.lower()):
        return False
    if re.fullmatch(r"ath\d+", value, re.IGNORECASE):
        return False
    token = re.sub(r"[^a-z0-9]", "", value.lower())
    if token in _NOT_SSID or re.fullmatch(r"11[abgnx0-9]+", value.lower()):
        return False
    return any(char.isalpha() for char in value)


def _looks_psk(value: str) -> bool:
    if not value or not 8 <= len(value) <= 63 or "InternetGateway" in value:
        return False
    if _MAC_RE.fullmatch(value) or value.lower() in _NOT_SSID:
        return False
    if re.fullmatch(r"\d{1,4}", value) or re.fullmatch(r"ath\d+", value, re.IGNORECASE):
        return False
    return any(char.isalnum() for char in value)


def parse_device_page(html: str) -> tuple[str, str]:
    text = _js_text(html)
    visible = unescape(re.sub(r"<[^>]+>", " ", text))
    visible = re.sub(r"\s+", " ", visible)
    quoted = re.findall(r'"([^"]*)"', text)
    serials: list[str] = []
    macs: list[str] = []
    device_serial, device_mac = _device_record(text)
    if device_serial:
        serials.append(device_serial)
    if device_mac:
        macs.append(device_mac)
    for source in (visible, " ".join(quoted), text):
        serials.extend(_HWTC_RE.findall(source))
        serials.extend(_HEX_SN_RE.findall(source))
        macs.extend(_MAC_RE.findall(source))
    sn_line = re.search(
        r"\bSN\b\s*[:=]\s*([0-9A-Fa-f]{8,32})(?:\s*\(([A-Za-z0-9]{8,32})\))?",
        visible,
        re.IGNORECASE,
    )
    if sn_line and sn_line.group(2):
        serials.insert(0, sn_line.group(2))
    elif sn_line:
        serials.insert(0, sn_line.group(1))
    labeled_serial = re.search(
        r"(?:Serial\s*Number|SerialNumber|Equipment\s*ID)\s*[:=]\s*([A-Za-z0-9]{8,32})",
        visible,
        re.IGNORECASE,
    )
    if labeled_serial and labeled_serial.group(1).upper() not in _LABELS:
        serials.insert(0, labeled_serial.group(1))
    labeled_mac = re.search(
        r"(?:MAC\s*Address|MacAddress)\s*[:=]\s*((?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2})",
        visible,
        re.IGNORECASE,
    )
    if labeled_mac:
        macs.insert(0, labeled_mac.group(1))
    if not macs:
        for token in quoted:
            if _BARE_MAC_RE.fullmatch(token) and not token.lower().startswith("48575443"):
                macs.append(":".join(token[index : index + 2] for index in range(0, 12, 2)))
    serial = _choose_serial(serials)
    mac = ""
    if macs:
        mac = ":".join(piece.lower() for piece in macs[0].split(":"))
    return serial, mac


def _js_text(html: str) -> str:
    return re.sub(r"\\x([0-9A-Fa-f]{2})", lambda match: chr(int(match.group(1), 16)), html)


def _device_record(text: str) -> tuple[str, str]:
    match = re.search(r"new\s+stDeviceInfo\((.*?)\)", text, re.DOTALL)
    if not match:
        return "", ""
    values = re.findall(r'"([^"]*)"', match.group(1))
    serial = values[1] if len(values) > 1 else ""
    mac = next((item for item in values if _MAC_RE.fullmatch(item)), "")
    return serial, mac


def _choose_serial(serials: list[str]) -> str:
    hex_sn = ""
    hwtc = ""
    other = ""
    for item in serials:
        text = item.upper()
        if text in _LABELS:
            continue
        if re.fullmatch(r"48575443[0-9A-F]{8}", text):
            if not hex_sn:
                hex_sn = text
            continue
        if text.startswith("HWTC") and not hwtc:
            hwtc = text
            continue
        if not other:
            other = text
    return hex_sn or hwtc or other


def _token(body: str) -> str:
    text = body.lstrip("\ufeff").strip()
    if not text:
        return ""
    return text.splitlines()[0].strip().lstrip("\ufeff")


def _candidate_pages(html: str) -> list[str]:
    found: list[str] = []
    for match in _ASP_RE.findall(html or ""):
        path = urllib.parse.urlsplit(match).path
        if not path.startswith("/") or path in {"/", "/login.asp"}:
            continue
        if path not in found:
            found.append(path)
    found.sort(key=lambda path: (0 if "device" in path.lower() else 1, path))
    return found[:8]


def _closed_session(body: str) -> bool:
    """The login page and the Waiting page. A Wi-Fi form also has a password box and must be kept."""
    lowered = (body or "").lower()
    if "<title>waiting" in lowered and "wlan" not in lowered and "ssid" not in lowered:
        return True
    return "txt_password" in lowered and (
        "getrandcount" in lowered or "safelogin" in lowered or "login.cgi" in lowered
    )


def _read_device(transport, host: str, first_page: str) -> dict[str, str]:
    collected = "" if _closed_session(first_page) else first_page
    queue = [*_PAGES, *_candidate_pages(first_page)]
    seen: set[str] = set()
    index = 0
    while index < len(queue) and index < 12:
        path = queue[index]
        index += 1
        if path in seen:
            continue
        seen.add(path)
        body, _cookie = transport.request("GET", path, headers={"Referer": f"http://{host}/index.asp"})
        if not body or _closed_session(body):
            continue
        collected += "\n" + body
        for extra in _candidate_pages(body):
            if extra not in seen and extra not in queue:
                queue.append(extra)
        if "stdeviceinfo" in body.lower():
            info = parse_basic_info(body)
            _fill(info, parse_ont_status(body))
            if info.get("serial"):
                return info
    info = parse_basic_info(collected)
    if info.get("serial"):
        return info
    serial, mac = parse_device_page(collected)
    return {
        "serial": serial,
        "mac": mac,
        "model": "",
        "software": "",
        "hardware": "",
        "device_type": "",
    }


def _read_inventory(transport, host: str, first_page: str) -> dict[str, str]:
    """Basic Information, then Optical Information and Wi-Fi configuration on the same session."""
    info = _read_device(transport, host, first_page)
    queue = list(_INVENTORY_PAGES)
    seen: set[str] = set()
    index = 0
    while index < len(queue) and index < 18:
        path = queue[index]
        index += 1
        if path in seen:
            continue
        seen.add(path)
        body, _cookie = transport.request("GET", path, headers={"Referer": f"http://{host}/index.asp"})
        if not body or _closed_session(body):
            continue
        if path.endswith("getMenuArray.asp"):
            for extra in _menu_paths(body):
                if extra not in seen and extra not in queue:
                    queue.append(extra)
            continue
        lowered = path.lower()
        if "optic" in lowered:
            _fill(info, parse_optic(body))
        if path.endswith("ontstate.asp"):
            _fill(info, parse_ont_status(body))
        if path.endswith("wan_list_cache_wan.asp"):
            rows = parse_wan(body)
            if rows:
                info["wan_rows"] = rows
        if "wlan" in lowered or "wifi" in lowered or "ssid" in lowered or "psk" in lowered:
            _fill(info, parse_wlan(body))
            for extra in _candidate_pages(body):
                extra_low = extra.lower()
                if extra in seen or extra in queue:
                    continue
                if any(word in extra_low for word in ("wlan", "wifi", "ssid", "psk")):
                    queue.append(extra)
    state, _cookie = transport.request(
        "POST",
        "/html/ssmp/common/GetDeviceState.asp",
        data=b"1",
        headers={"Referer": f"http://{host}/html/ssmp/bss/bssinfo.asp"},
    )
    _fill(info, parse_provision(state or ""))
    info.setdefault("device_type", info.get("model") or "")
    return info


def _menu_paths(html: str) -> list[str]:
    found: list[str] = []
    for match in _ASP_RE.findall(html or ""):
        path = urllib.parse.urlsplit(match).path
        if path and not path.startswith("/"):
            path = "/" + path
        lowered = path.lower()
        if "optic" not in lowered and "wlan" not in lowered and "wifi" not in lowered:
            continue
        if path not in found:
            found.append(path)
    return found[:6]


def _fill(target: dict[str, str], incoming: dict[str, str]) -> None:
    for key, value in incoming.items():
        if value and not target.get(key):
            target[key] = value


def _normalize_cg_model(token: str) -> str:
    from tc_net_cert.dut.portals import canonical_profile_model

    raw = (token or "").strip()
    if not raw:
        return ""
    if not raw.upper().startswith("CG-"):
        match = re.search(r"CG-?\d{4}", raw, flags=re.I)
        if match:
            raw = match.group(0)
    text = raw.upper()
    if text.startswith("CG") and not text.startswith("CG-"):
        text = "CG-" + text[2:]
    return canonical_profile_model(text)


def fingerprint_portal(status: int, challenge: str, home: str = "") -> dict[str, str]:
    """HTTP signature of the login page: family, portal_kind, model, brand."""
    from tc_net_cert.dut.broadcom import commercial_model

    mark = fingerprint_page(home or "")
    lowered = (home or "").lower()
    blob = f"{home or ''}\n{challenge or ''}"
    if "hitron" in lowered:
        model_token = ""
        hitron = re.search(r"CGNV\d+\w*", blob, flags=re.I)
        if hitron:
            model_token = hitron.group(0)
        model = commercial_model("HISTRON", model_token) or "CGNV5"
        return {
            **mark,
            "family": "emta",
            "portal_kind": "emta-hitron",
            "model": model,
            "brand": "HISTRON",
        }
    if mark.get("family") == "huawei-ont" and "kaon" not in lowered:
        return {
            **mark,
            "family": "ont",
            "portal_kind": "huawei-ont",
            "model": str(mark.get("model") or ""),
            "brand": "HUAWEI",
        }
    if _emta_portal(status, challenge, home or ""):
        model = ""
        if re.search(r"CG[- ]?3000", blob, flags=re.I):
            model = "CG-3000"
        elif re.search(r"CG[- ]?2200", blob, flags=re.I):
            model = "CG-2200"
        else:
            match = re.search(r"CG-?\d{4}", blob, flags=re.I)
            if match:
                model = _normalize_cg_model(match.group(0))
        brand = "KAON" if "kaon" in lowered else ""
        if not model and brand == "KAON":
            model = "CG-2200"
        return {
            **mark,
            "family": "emta",
            "portal_kind": "emta-broadcom",
            "model": model,
            "brand": brand,
        }
    return {**mark, "family": "", "portal_kind": "", "model": mark.get("model") or "", "brand": ""}


def apply_portal_fingerprint(found: dict[str, object], fingerprint: dict[str, str]) -> None:
    """Expose probe results to the panel even when login did not run."""
    if fingerprint.get("family") and not found.get("family"):
        found["family"] = fingerprint["family"]
    if fingerprint.get("brand") and not found.get("brand"):
        found["brand"] = fingerprint["brand"]
    if fingerprint.get("model"):
        found["detected_model"] = fingerprint["model"]
        if not found.get("model"):
            found["model"] = fingerprint["model"]
    if fingerprint.get("portal_kind"):
        found["portal_kind"] = fingerprint["portal_kind"]


def fingerprint_page(html: str) -> dict[str, str]:
    """Read the login page markers. ProductName and CfgMode identify the ONT."""
    text = _js_text(html or "")
    found = {"model": "", "cfg_mode": "", "family": "", "login": ""}
    for key, value in _ASSIGN_RE.findall(text):
        if key == "ProductName" and not found["model"]:
            found["model"] = value.strip()
        elif key == "CfgMode" and not found["cfg_mode"]:
            found["cfg_mode"] = value.strip()
    lowered = text.lower()
    huawei = any(
        marker in lowered
        for marker in ("safelogin.js", "getrandcount.asp", "login.cgi", "txt_password", "stdeviceinfo")
    )
    if not huawei and not found["model"]:
        return found
    found["family"] = "huawei-ont"
    if found["cfg_mode"].upper() in _SHA256_MODES:
        found["login"] = "sha256"
    elif "getrandcount.asp" in lowered or found["cfg_mode"]:
        found["login"] = "rand-base64"
    return found


def _clean_profiles(raw: object) -> dict[str, dict[str, str]]:
    if not isinstance(raw, dict):
        return {}
    cleaned: dict[str, dict[str, str]] = {}
    for model, item in raw.items():
        name = str(model)
        if not isinstance(item, dict) or not _MODEL_RE.fullmatch(name):
            continue
        username = str(item.get("username") or "")
        password = str(item.get("password") or "")
        if valid_user(username) and valid_password(password):
            cleaned[name] = {"username": username, "password": password}
    return cleaned


def _credentials(
    model: str,
    profiles: dict[str, dict[str, str]],
    username: str,
    password: str,
    form_model: str = "",
) -> tuple[str, str]:
    saved = profiles.get(model) if model else None
    if saved:
        return saved["username"], saved["password"]
    # A password saved for another model must not be tried on this portal.
    if model and form_model and form_model != model:
        return "", ""
    if len(profiles) == 1 and not model:
        only = next(iter(profiles.values()))
        return only["username"], only["password"]
    if valid_user(username) and valid_password(password):
        return username, password
    return "", ""


def select_profile(
    mark: dict[str, str],
    status: int,
    challenge: str,
    profiles: dict,
    home: str = "",
) -> dict[str, str]:
    """Pick one saved profile from the login page fingerprint. Another model's password is not selected."""
    from tc_net_cert.dut.portals import match_saved_profile

    none = {"ok": "", "family": "", "model": "", "username": "", "password": "", "error": ""}
    if status == 0 and not home:
        return {**none, "error": "El portal no respondió por el puerto de prueba. No se intentó el ingreso."}
    fingerprint = fingerprint_portal(status, challenge, home or "")
    if not fingerprint.get("portal_kind") and mark.get("model"):
        fingerprint["model"] = str(mark.get("model") or fingerprint.get("model") or "")
    return match_saved_profile(fingerprint, profiles or {}, home or "", challenge or "")


def _emta_portal(status: int, challenge: str, home: str) -> bool:
    text = f"{challenge}\n{home}".lower()
    if "broadcom" in text or "kaon" in text:
        return True
    return status == 401 and "basic" in challenge.lower()


def portal_hint(status: int, challenge: str, home: str) -> dict[str, str]:
    """Best-effort type/model before a saved profile exists."""
    fp = fingerprint_portal(status, challenge, home or "")
    return {
        "family": fp.get("family") or "",
        "model": fp.get("model") or "",
        "brand": fp.get("brand") or "",
        "portal_kind": fp.get("portal_kind") or "",
    }


def discover_portal(
    host: str,
    profiles: dict | None = None,
    source: str = "",
    device: str = "",
) -> dict[str, object]:
    """Probe the cabled portal once, then log in with the one matching saved profile."""
    if not allowed_host(host):
        return _result(False, error="La dirección del equipo no es válida.")
    from tc_net_cert.dut.broadcom import probe, read_basic
    from tc_net_cert.dut.hitron import login_page, read_hitron

    status, challenge, home = probe(host, source, device)
    if status == 302 and "hitron" not in (home or "").lower():
        home = login_page(host, source, device) or home
    fingerprint = fingerprint_portal(status, challenge, home or "")
    hint = portal_hint(status, challenge, home or "")
    choice = select_profile(fingerprint_page(home or ""), status, challenge, profiles or {}, home or "")
    if choice["ok"] != "yes":
        error = choice["error"]
        if device and error.startswith("El portal no respondió"):
            error = f"El portal no respondió por {device}. No se intentó el ingreso."
        model = choice["model"] or hint["model"]
        result = _result(False, error=error, model=model)
        apply_portal_fingerprint(result, fingerprint)
        if hint["family"] and not result.get("family"):
            result["family"] = hint["family"]
        if hint["brand"] and not result.get("brand"):
            result["brand"] = hint["brand"]
        if choice.get("username") and choice.get("password"):
            result["profile"] = choice["model"]
        return result
    if fingerprint.get("portal_kind") == "emta-hitron" or "hitron" in (home or "").lower():
        found = read_hitron(host, choice["username"], choice["password"], source=source, device=device)
    elif choice["family"] == "ont" or fingerprint.get("portal_kind") == "huawei-ont":
        client = PortalSession(host, source=source, device=device)
        try:
            found = _collect(
                client,
                choice["username"],
                choice["password"],
                profiles,
                form_model=choice["model"],
                inventory=True,
                home=home or None,
            )
        finally:
            client.close()
    else:
        found = read_basic(
            host,
            choice["username"],
            choice["password"],
            source=source,
            device=device,
        )
    found["gateway"] = host
    found["iface"] = device
    found["family"] = choice["family"]
    found["profile"] = choice["model"]
    apply_portal_fingerprint(found, fingerprint)
    if choice["family"] == "ont" and not found.get("model"):
        found["model"] = choice["model"]
    return found


def _result(
    ok: bool,
    serial: str = "",
    mac: str = "",
    error: str = "",
    model: str = "",
    cfg_mode: str = "",
    software: str = "",
    hardware: str = "",
    gateway: str = "",
    iface: str = "",
    device_type: str = "",
    tx_power: str = "",
    rx_power: str = "",
    voltage: str = "",
    bias: str = "",
    temperature: str = "",
    ont_state: str = "",
    wlan_status: str = "",
    wlan_channel: str = "",
    ssid_24: str = "",
    ssid_50: str = "",
    wifi_password_24: str = "",
    wifi_password_50: str = "",
    ont_id: str = "",
    pon_mode: str = "",
    optic_link: str = "",
    los_status: str = "",
    tx_wavelength: str = "",
    rx_wavelength: str = "",
    optic_vendor: str = "",
    optic_module: str = "",
    optic_distance: str = "",
    vendor_id: str = "",
    release_time: str = "",
    description: str = "",
    registration: str = "",
    olt_service: str = "",
    ems_status: str = "",
    acs_status: str = "",
    uptime: str = "",
    wan_rows: list | None = None,
) -> dict[str, object]:
    return {
        "ok": ok,
        "serial": serial,
        "mac": mac,
        "model": model,
        "cfg_mode": cfg_mode,
        "software": software,
        "hardware": hardware,
        "gateway": gateway,
        "iface": iface,
        "device_type": device_type,
        "tx_power": tx_power,
        "rx_power": rx_power,
        "voltage": voltage,
        "bias": bias,
        "temperature": temperature,
        "ont_state": ont_state,
        "wlan_status": wlan_status,
        "wlan_channel": wlan_channel,
        "ssid_24": ssid_24,
        "ssid_50": ssid_50,
        "wifi_password_24": wifi_password_24,
        "wifi_password_50": wifi_password_50,
        "ont_id": ont_id,
        "pon_mode": pon_mode,
        "optic_link": optic_link,
        "los_status": los_status,
        "tx_wavelength": tx_wavelength,
        "rx_wavelength": rx_wavelength,
        "optic_vendor": optic_vendor,
        "optic_module": optic_module,
        "optic_distance": optic_distance,
        "vendor_id": vendor_id,
        "release_time": release_time,
        "description": description,
        "registration": registration,
        "olt_service": olt_service,
        "ems_status": ems_status,
        "acs_status": acs_status,
        "uptime": uptime,
        "wan_rows": list(wan_rows or []),
        "error": error,
    }


def exchange(
    host: str,
    username: str,
    password: str,
    opener,
    profiles: dict | None = None,
    form_model: str = "",
) -> dict[str, object]:
    if not allowed_host(host):
        return _result(False, error="La dirección del ONT no es válida.")
    return _collect(_UrlTransport(opener, host), username, password, profiles, form_model=form_model)


def _infer_gateway(address: str, prefixlen: int | None) -> str:
    """Typical CPE gateway on the same /24 when NM only installed a link route."""
    import ipaddress

    if not address or prefixlen is None:
        return ""
    try:
        iface = ipaddress.ip_interface(f"{address}/{int(prefixlen)}")
    except ValueError:
        return ""
    if iface.network.version != 4 or iface.network.prefixlen < 8:
        return ""
    candidate = str(iface.network.network_address + 1)
    return candidate if allowed_host(candidate) else ""


def _routers_from_default_routes() -> dict[str, str]:
    """Map local source address → default gateway from the routing table."""
    import re
    import subprocess

    found: dict[str, str] = {}
    try:
        proc = subprocess.run(
            ["ip", "-4", "route", "show", "default"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except OSError:
        return found
    pattern = re.compile(r"^default via (\S+).*?\ssrc (\S+)")
    for line in proc.stdout.splitlines():
        match = pattern.match(line.strip())
        if not match:
            continue
        gateway, address = match.group(1), match.group(2)
        if allowed_host(gateway):
            found[address] = gateway
    return found


def _nmcli_routers() -> dict[str, str]:
    """NetworkManager DHCP option 3, keyed by leased IPv4 (no default route required)."""
    import subprocess

    found: dict[str, str] = {}
    try:
        proc = subprocess.run(
            ["nmcli", "-t", "-f", "DEVICE,TYPE", "device"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except OSError:
        return found
    for line in (proc.stdout or "").splitlines():
        parts = line.split(":", 1)
        if len(parts) != 2 or parts[1] != "ethernet":
            continue
        dev = parts[0].strip()
        if not dev:
            continue
        try:
            show = subprocess.run(
                ["nmcli", "-g", "IP4.ADDRESS,IP4.GATEWAY", "device", "show", dev],
                capture_output=True,
                text=True,
                timeout=8,
                check=False,
            )
        except OSError:
            continue
        addresses: list[str] = []
        gateways: list[str] = []
        for row in (show.stdout or "").splitlines():
            text = row.strip()
            if not text:
                continue
            if "/" in text and _IPV4.fullmatch(text.split("/", 1)[0]):
                addresses.append(text.split("/", 1)[0])
            elif _IPV4.fullmatch(text):
                gateways.append(text)
        for index, address in enumerate(addresses):
            gateway = gateways[index] if index < len(gateways) else gateways[0] if len(gateways) == 1 else ""
            if address and gateway and allowed_host(gateway):
                found[address] = gateway
    return found


def _route_gateway(dev: str, address: str, probe: str = "192.168.1.1") -> str:
    """Resolve the CPE gateway on a test NIC when only a link route exists (ipv4.never-default)."""
    import re
    import subprocess

    if not allowed_host(probe) or not _IPV4.fullmatch(address):
        return ""
    try:
        proc = subprocess.run(
            ["ip", "-4", "route", "get", probe, "from", address, "oif", dev],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except OSError:
        return ""
    if proc.returncode != 0:
        return ""
    text = proc.stdout.strip()
    match = re.search(r"\bvia (\S+)", text)
    if match and allowed_host(match.group(1)):
        return match.group(1)
    if probe in text and allowed_host(probe):
        return probe
    return ""


def _dhcp_routers() -> dict[str, str]:
    """Leased address → DHCP router. This is the ONT gateway even when that route is not installed."""
    from pathlib import Path

    root = Path("/run/systemd/netif/leases")
    found: dict[str, str] = {}
    if root.is_dir():
        for path in root.iterdir():
            fields: dict[str, str] = {}
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for line in lines:
                if "=" not in line:
                    continue
                key, value = line.split("=", 1)
                fields[key.strip()] = value.strip()
            address = fields.get("ADDRESS", "")
            router = fields.get("ROUTER", "").split()[0] if fields.get("ROUTER") else ""
            if address and router and allowed_host(router):
                found[address] = router
    for address, router in _routers_from_default_routes().items():
        found.setdefault(address, router)
    for address, router in _nmcli_routers().items():
        found.setdefault(address, router)
    return found


def dut_paths(macs: set[str]) -> list[dict[str, str]]:
    """Every test NIC that has a DHCP gateway. Wi-Fi is never included."""
    import json as _json
    import subprocess
    from pathlib import Path

    wanted = {mac.lower() for mac in macs}
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
        return []
    routers = _dhcp_routers()
    net = Path("/sys/class/net")
    found: list[dict[str, str]] = []
    for record in records:
        iface = str(record.get("ifname") or "")
        sysfs = net / iface
        if not iface or (sysfs / "wireless").exists() or (sysfs / "phy80211").exists():
            continue
        try:
            mac = (sysfs / "address").read_text(encoding="utf-8").strip().lower()
        except OSError:
            continue
        if mac not in wanted:
            continue
        for item in record.get("addr_info") or []:
            address = str(item.get("local") or "")
            prefixlen = item.get("prefixlen")
            if not address.startswith("192.168."):
                continue
            gateway = routers.get(address, "")
            if not allowed_host(gateway):
                gateway = _infer_gateway(address, prefixlen if isinstance(prefixlen, int) else None)
            if not allowed_host(gateway):
                gateway = _route_gateway(iface, address)
            if not allowed_host(gateway) and address.startswith("192.168.1."):
                gateway = "192.168.1.1"
            if allowed_host(gateway):
                found.append({"iface": iface, "source": address, "gateway": gateway})
                break
    return found


def portal_retryable(error: str) -> bool:
    """Another test cable may reach the same portal. A rejected password is not retried."""
    text = error or ""
    if "no aceptó el usuario o la contraseña" in text:
        return False
    return (
        text.startswith("El portal no respondió")
        or text.startswith("El portal del ONT no respondió")
        or "no entregó la página de estado" in text
        or "no abrió la sesión" in text
        or "no entregó la ficha de acceso" in text
    )


def dut_path(macs: set[str]) -> dict[str, str]:
    """Test NIC, its address, and the DHCP gateway of that cable. Wi-Fi is never chosen."""
    found = dut_paths(macs)
    return found[0] if found else {}


def lan_source(macs: set[str]) -> str:
    return dut_path(macs).get("source", "")


def read_portal(
    host: str,
    username: str,
    password: str,
    profiles: dict | None = None,
    source: str = "",
    device: str = "",
    form_model: str = "",
    inventory: bool = False,
) -> dict[str, object]:
    """Log in through one test NIC and read Basic Information on that same TCP connection."""
    if not allowed_host(host):
        return _result(False, error="La dirección del ONT no es válida.")
    from tc_net_cert.dut.broadcom import probe, read_basic
    from tc_net_cert.dut.hitron import login_page, read_hitron

    status, challenge, home = probe(host, source, device)
    if status == 302 and "hitron" not in (home or "").lower():
        home = login_page(host, source, device) or home
    if status == 0 and not home:
        error = "El portal no respondió por el puerto de prueba. No se intentó el ingreso."
        if device:
            error = f"El portal no respondió por {device}. No se intentó el ingreso."
        found = _result(False, error=error)
        found["gateway"] = host
        found["iface"] = device
        return found
    fingerprint = fingerprint_portal(status, challenge, home or "")
    portal_kind = fingerprint.get("portal_kind") or ""
    if portal_kind == "emta-hitron" or "hitron" in (home or "").lower():
        from tc_net_cert.dut.portals import match_saved_profile

        picked = match_saved_profile(fingerprint, profiles or {}, home or "", challenge or "")
        if picked.get("ok") == "yes" and not (valid_user(username) and valid_password(password)):
            username = picked["username"]
            password = picked["password"]
        found = read_hitron(host, username, password, source=source, device=device)
        if found.get("ok"):
            found["family"] = "emta"
    elif portal_kind == "huawei-ont" or (
        fingerprint_page(home or "").get("family") == "huawei-ont" and "kaon" not in (home or "").lower()
    ):
        client = PortalSession(host, source=source, device=device)
        try:
            found = _collect(
                client,
                username,
                password,
                profiles,
                form_model=form_model,
                inventory=inventory,
                home=home or None,
            )
        finally:
            client.close()
    else:
        found = read_basic(host, username, password, source=source, device=device)
        found["family"] = "emta"
    apply_portal_fingerprint(found, fingerprint)
    if portal_kind == "emta-hitron" and found.get("ok"):
        found["family"] = "emta"
    elif portal_kind == "huawei-ont" and found.get("ok"):
        found["family"] = "ont"
    elif portal_kind == "emta-broadcom" and found.get("ok"):
        found["family"] = "emta"
        from tc_net_cert.dut.portals import canonical_profile_model

        model = str(found.get("model") or fingerprint.get("model") or "")
        found["profile"] = canonical_profile_model(model) or model or "CG-2200"
    found["gateway"] = host
    found["iface"] = device
    return found


def _collect(
    transport,
    username: str,
    password: str,
    profiles: dict | None,
    form_model: str = "",
    inventory: bool = False,
    home: str | None = None,
) -> dict[str, object]:
    if home is None:
        home, _cookie = transport.request("GET", "/")
    if not home:
        return _result(False, error="El portal del ONT no respondió.")
    mark = fingerprint_page(home)
    model = mark["model"]
    cfg_mode = mark["cfg_mode"]
    if mark["family"] != "huawei-ont":
        return _result(False, error="El portal no coincide con un equipo conocido.", model=model, cfg_mode=cfg_mode)
    if mark["login"] != "rand-base64":
        label = model or "detectado"
        mode = f" ({cfg_mode})" if cfg_mode else ""
        return _result(
            False,
            error=f"El equipo {label}{mode} usa otra forma de acceso. No se intentó el ingreso.",
            model=model,
            cfg_mode=cfg_mode,
        )
    login_user, login_password = _credentials(
        model, _clean_profiles(profiles), username, password, form_model=form_model
    )
    if not login_user:
        label = model or "detectado"
        return _result(False, error=f"No hay un perfil guardado para {label}.", model=model, cfg_mode=cfg_mode)
    token_body, _cookie = transport.request(
        "POST",
        "/asp/GetRandCount.asp",
        data=b"",
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    token = _token(token_body or "")
    if not _TOKEN_RE.fullmatch(token):
        if token_body and ("<html" in token_body.lower() or "txt_password" in token_body.lower()):
            error = "El ONT mostró la página de acceso en lugar de la ficha."
        else:
            error = "El ONT no entregó la ficha de acceso."
        return _result(False, error=error, model=model, cfg_mode=cfg_mode)
    login = urllib.parse.urlencode(
        {
            "UserName": login_user,
            "PassWord": huawei_password(login_password),
            "Language": "english",
            "x.X_HW_Token": token,
        }
    ).encode("ascii")
    page, set_cookie = transport.request("POST", "/login.cgi", data=login)
    if not _session_open(page, set_cookie):
        if page and ("txt_password" in page.lower() or "getrandcount" in page.lower()):
            error = "El ONT no aceptó el usuario o la contraseña."
        else:
            error = "El ONT no abrió la sesión del portal."
        return _result(False, error=error, model=model, cfg_mode=cfg_mode)
    if inventory:
        info = _read_inventory(transport, transport.host, page or "")
    else:
        info = _read_device(transport, transport.host, page or "")
    serial = info.get("serial") or ""
    page_model = info.get("model") or model
    if serial:
        return _result(
            True,
            serial,
            info.get("mac") or "",
            model=page_model,
            cfg_mode=cfg_mode,
            software=info.get("software") or "",
            hardware=info.get("hardware") or "",
            device_type=info.get("device_type") or page_model,
            tx_power=info.get("tx_power") or "",
            rx_power=info.get("rx_power") or "",
            voltage=info.get("voltage") or "",
            bias=info.get("bias") or "",
            temperature=info.get("temperature") or "",
            ont_state=info.get("ont_state") or "",
            wlan_status=info.get("wlan_status") or "",
            wlan_channel=info.get("wlan_channel") or "",
            ssid_24=info.get("ssid_24") or "",
            ssid_50=info.get("ssid_50") or "",
            wifi_password_24=info.get("wifi_password_24") or "",
            wifi_password_50=info.get("wifi_password_50") or "",
            ont_id=str(info.get("ont_id") or ""),
            pon_mode=str(info.get("pon_mode") or ""),
            optic_link=str(info.get("optic_link") or ""),
            los_status=str(info.get("los_status") or ""),
            tx_wavelength=str(info.get("tx_wavelength") or ""),
            rx_wavelength=str(info.get("rx_wavelength") or ""),
            optic_vendor=str(info.get("optic_vendor") or ""),
            optic_module=str(info.get("optic_module") or ""),
            optic_distance=str(info.get("optic_distance") or ""),
            vendor_id=str(info.get("vendor_id") or ""),
            release_time=str(info.get("release_time") or ""),
            description=str(info.get("description") or ""),
            registration=str(info.get("registration") or ""),
            olt_service=str(info.get("olt_service") or ""),
            ems_status=str(info.get("ems_status") or ""),
            acs_status=str(info.get("acs_status") or ""),
            uptime=str(info.get("uptime") or ""),
            wan_rows=info.get("wan_rows") if isinstance(info.get("wan_rows"), list) else [],
        )
    return _result(
        False,
        error="La sesión entró, pero Basic Information no mostró el SN.",
        model=page_model,
        cfg_mode=cfg_mode,
    )


def _session_open(page: str | None, set_cookie: str) -> bool:
    """The ONT answers a good login with Waiting plus a sid cookie. Waiting alone is not a session."""
    if not page:
        return False
    lowered = page.lower()
    if "txt_password" in lowered or "getrandcount" in lowered:
        return False
    if "sid=" in set_cookie and "id=1" in set_cookie:
        return True
    return "<title>waiting" not in lowered


def _set_cookie(response) -> str:
    headers = getattr(response, "headers", None)
    if headers is None:
        return ""
    getter = getattr(headers, "get", None)
    if getter is None:
        return ""
    values = getter("Set-Cookie")
    if values is None and hasattr(headers, "get_all"):
        values = headers.get_all("Set-Cookie")
    if isinstance(values, list):
        return "\n".join(values)
    return values or ""


def _open(
    opener,
    host: str,
    path: str,
    method: str = "GET",
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[str | None, str]:
    request = urllib.request.Request(
        f"http://{host}{path}",
        data=data,
        method=method,
        headers={"User-Agent": _BROWSER, "Referer": f"http://{host}/", **(headers or {})},
    )
    try:
        with opener.open(request, timeout=8) as response:
            return response.read(200_000).decode("utf-8", "replace"), _set_cookie(response)
    except urllib.error.HTTPError as exc:
        return exc.read(200_000).decode("utf-8", "replace"), _set_cookie(exc)
    except (OSError, urllib.error.URLError):
        return None, ""


def _request(
    opener,
    host: str,
    path: str,
    method: str = "GET",
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> str | None:
    body, _cookie = _open(opener, host, path, method=method, data=data, headers=headers)
    return body


class PortalSession:
    """One HTTP connection to the ONT. GetRandCount and login.cgi must share it."""

    def __init__(self, host: str, port: int = 80, source: str = "", device: str = "") -> None:
        self.host = host
        self.source = source
        self.device = device if re.fullmatch(r"[A-Za-z0-9_.:-]{1,15}", device or "") else ""
        self.cookie = "Cookie=body:Language:english:id=-1"
        self.last_status = 0
        self.last_headers: dict[str, str] = {}
        source_address = (source, 0) if source and not self.device else None
        self.conn = http.client.HTTPConnection(host, port, timeout=12, source_address=source_address)
        if self.device:
            self.conn.connect = self._connect  # type: ignore[method-assign]
        self.conn.connect()

    def _connect(self) -> None:
        """Leave through the test NIC. A host route on Wi-Fi would otherwise steal 192.168.1.1."""
        import socket

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(self.conn.timeout)
        sock.setsockopt(socket.SOL_SOCKET, 25, self.device.encode("ascii") + b"\0")
        if self.source:
            sock.bind((self.source, 0))
        sock.connect((self.host, self.conn.port or 80))
        self.conn.sock = sock

    def close(self) -> None:
        self.conn.close()

    def request(
        self,
        method: str,
        path: str,
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[str | None, str]:
        hdrs = {
            "User-Agent": _BROWSER,
            "Referer": f"http://{self.host}/",
            "Cookie": self.cookie,
            "Connection": "keep-alive",
            **(headers or {}),
        }
        body = b"" if method == "POST" and data is None else data
        if body is not None and "Content-Type" not in hdrs:
            hdrs["Content-Type"] = "application/x-www-form-urlencoded"
        try:
            self.conn.request(method, path, body=body, headers=hdrs)
            response = self.conn.getresponse()
            self.last_status = int(response.status)
            self.last_headers = {key.lower(): value for key, value in response.getheaders()}
            payload = response.read(200_000).decode("utf-8", "replace")
            cookies = [value for key, value in response.getheaders() if key.lower() == "set-cookie"]
            set_cookie = "\n".join(cookies)
        except (OSError, http.client.HTTPException):
            self.last_status = 0
            self.last_headers = {}
            return None, ""
        pair = set_cookie.split(";", 1)[0].strip()
        if pair.lower().startswith("cookie=") and "sid=" in pair:
            self.cookie = pair
        return payload, set_cookie


class _UrlTransport:
    def __init__(self, opener, host: str) -> None:
        self.opener = opener
        self.host = host

    def request(
        self,
        method: str,
        path: str,
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[str | None, str]:
        return _open(self.opener, self.host, path, method=method, data=data, headers=headers)


def _opener(host: str) -> urllib.request.OpenerDirector:
    from http.cookiejar import Cookie, CookieJar

    jar = CookieJar()
    jar.set_cookie(
        Cookie(
            version=0,
            name="Cookie",
            value="body:Language:english:id=-1",
            port=None,
            port_specified=False,
            domain=host,
            domain_specified=True,
            domain_initial_dot=False,
            path="/",
            path_specified=True,
            secure=False,
            expires=None,
            discard=True,
            comment=None,
            comment_url=None,
            rest={},
        )
    )

    class _SameHost(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            parsed = urllib.parse.urlparse(newurl)
            if parsed.scheme != "http" or parsed.hostname != host:
                return None
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPCookieProcessor(jar),
        _SameHost(),
    )


def main() -> None:
    try:
        payload = json.load(sys.stdin)
        host = str(payload.get("host") or "")
        username = str(payload.get("username") or "")
        password = str(payload.get("password") or "")
        if payload.get("counters") is True:
            from tc_net_cert.dut.broadcom import read_counters

            result = read_counters(host, username, password)
        else:
            result = read_portal(
                host,
                username,
                password,
                profiles=payload.get("profiles"),
                inventory=bool(payload.get("inventory")),
            )
            _stash_wifi_keys(result, str(payload.get("secret_path") or ""))
    except Exception:
        result = _result(False, error="No se pudo consultar el ONT.")
    sys.stdout.write(json.dumps(result))


def _stash_wifi_keys(result: dict[str, object], secret_path: str) -> None:
    """Keep the ONT Wi-Fi keys out of the audited stdout. The parent reads this file once."""
    keys = {
        "wifi_password_24": str(result.get("wifi_password_24") or ""),
        "wifi_password_50": str(result.get("wifi_password_50") or ""),
    }
    result["wifi_password_24"] = ""
    result["wifi_password_50"] = ""
    if not secret_path or not any(keys.values()):
        return
    from pathlib import Path

    path = Path(secret_path)
    path.write_text(json.dumps(keys), encoding="utf-8")
    path.chmod(0o600)


if __name__ == "__main__":
    main()
