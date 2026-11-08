"""Read a Hitron EMTA portal (CGNV5).

The login is one form post, not HTTP Basic. GoAhead joins two Set-Cookie
headers with a bare newline, so the usual header split drops the session.
The Wi-Fi key is returned to the local panel only. The certificate does not
print it. This reader does not claim DOCSIS provisioning, CMTS registration,
or TFTP.
"""

from __future__ import annotations

import json
import re
import socket
from urllib.parse import urlencode

from tc_net_cert.dut.huawei import _result, allowed_host, valid_password, valid_user

PROFILE = "CGNV5"
_IFACE = re.compile(r"^[A-Za-z0-9_.:-]{1,15}$")
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")


def login_page(host: str, source: str = "", device: str = "") -> str:
    """The login document, when the gateway redirects there. No password is sent."""
    if not allowed_host(host):
        return ""
    _status, headers, body = _exchange(host, "/", source, device)
    if "hitron" in body.lower():
        return body
    location = headers.get("location", "")
    if "login.html" not in location.lower() and "login.html" not in body.lower():
        return ""
    _status, _headers, page = _exchange(host, "/login.html", source, device)
    return page if "hitron" in page.lower() else ""


def read_hitron(
    host: str,
    username: str,
    password: str,
    source: str = "",
    device: str = "",
) -> dict[str, object]:
    if not allowed_host(host) or not valid_user(username) or not valid_password(password):
        return _result(False, error="Revisa el usuario y la contraseña del EMTA.", model=PROFILE)
    _status, headers, home = _exchange(host, "/", source, device)
    page = home if "hitron" in home.lower() else ""
    if not page and ("login.html" in headers.get("location", "").lower() or "login.html" in home.lower()):
        _status, _headers, page = _exchange(host, "/login.html", source, device, cookies=headers.get("cookie", ""))
    if "hitron" not in page.lower():
        return _result(False, error="El portal no coincide con un equipo Hitron. No se intentó el ingreso.")
    pre = _cookie_value(headers.get("cookie", ""), "preSession")
    if not pre:
        _status, again, _body = _exchange(host, "/", source, device)
        headers["cookie"] = _merge_cookie(headers.get("cookie", ""), again.get("cookie", ""))
        pre = _cookie_value(headers.get("cookie", ""), "preSession")
    if not pre:
        return _result(False, error="El portal Hitron no entregó la ficha de acceso. No se intentó el ingreso.")
    posted, session = _login(host, username.strip().lower(), password, headers.get("cookie", ""), pre, source, device)
    lowered = posted.lower()
    if "wrong credential" in lowered:
        return _result(False, error="El EMTA no aceptó el usuario o la contraseña.", model=PROFILE, family="emta")
    if "success" not in lowered:
        return _result(
            False,
            error="El portal del EMTA no abrió la sesión. Revisa usuario y contraseña del CGNV5.",
            model=PROFILE,
            family="emta",
        )
    system = _json(host, "/data/getSysInfo.asp", session, source, device)
    model_page = _json(host, "/data/system_model.asp", session, source, device)
    wan = _json(host, "/data/getCmDocsisWan.asp", session, source, device)
    downstream = _json(host, "/data/dsinfo.asp", session, source, device)
    upstream = _json(host, "/data/usinfo.asp", session, source, device)
    radios = _json(host, "/data/wireless_basic.asp", session, source, device)
    ssids = _json(host, "/data/wireless_ssid.asp", session, source, device)
    info = _first(system)
    if not str(info.get("serialNumber") or "") and not str(info.get("rfMac") or ""):
        return _result(False, error="El EMTA abrió la sesión, pero la página no mostró el SN ni la MAC.", model=PROFILE)
    model_name = str(_first(model_page).get("modelName") or info.get("htxProject") or "")
    from tc_net_cert.dut.broadcom import commercial_model

    ssid_24, ssid_50, key_24, key_50 = _radios(ssids if isinstance(ssids, list) else [])
    channels = summarize_channels(
        downstream if isinstance(downstream, list) else [],
        upstream if isinstance(upstream, list) else [],
    )
    access = str(_first(wan).get("NetworkAccess") or "").strip().rstrip(".")
    result = _result(
        True,
        serial=str(info.get("serialNumber") or ""),
        mac=str(info.get("rfMac") or ""),
        model=commercial_model("HISTRON", model_name),
        software=str(info.get("swVersion") or ""),
        hardware=str(info.get("hwVersion") or ""),
        device_type="EMTA",
        wlan_status=_radio_state(radios if isinstance(radios, list) else []),
        wlan_channel=_radio_channels(radios if isinstance(radios, list) else []),
        ssid_24=ssid_24,
        ssid_50=ssid_50,
        wifi_password_24=key_24,
        wifi_password_50=key_50,
    )
    result["brand"] = "HISTRON"
    result["family"] = "emta"
    result["profile"] = PROFILE
    result["docsis_status"] = access
    result.update(channels)
    return result


def summarize_channels(downstream: list, upstream: list) -> dict[str, str]:
    ds_rows = [row for row in downstream if isinstance(row, dict)]
    us_rows = [row for row in upstream if isinstance(row, dict)]
    ds_locked = [row for row in ds_rows if _locked_down(row)]
    us_locked = [row for row in us_rows if _locked_up(row)]
    ds_used = ds_locked or ds_rows
    us_used = us_locked or us_rows
    return {
        "ds_channels": _count(ds_locked, ds_rows),
        "ds_power": _mean(ds_used, "signalStrength", "dBmV"),
        "us_channels": _count(us_locked, us_rows),
        "us_power": _mean(us_used, "signalStrength", "dBmV"),
        "mer": _mean(ds_used, "snr", "dB"),
    }


def _count(locked: list, rows: list) -> str:
    if not rows:
        return ""
    return f"{len(locked)} sintonizados de {len(rows)}"


def _locked_down(row: dict) -> bool:
    frequency = _number(row.get("frequency"))
    return frequency not in {None, 0.0}


def _locked_up(row: dict) -> bool:
    channel = str(row.get("channelId") or "").strip()
    return channel not in {"", "0", "---"} and _locked_down(row)


def _mean(rows: list, key: str, unit: str) -> str:
    numbers = [value for value in (_number(row.get(key)) for row in rows) if value is not None]
    if not numbers:
        return ""
    return f"{sum(numbers) / len(numbers):.1f} {unit}"


def _number(value: object) -> float | None:
    match = _NUMBER.search(str(value or ""))
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def _radios(rows: list) -> tuple[str, str, str, str]:
    chosen = {"2.4G": None, "5G": None}
    for row in rows:
        if not isinstance(row, dict):
            continue
        band = str(row.get("band") or "")
        if band not in chosen or chosen[band] is not None:
            continue
        if str(row.get("enable") or "").upper() != "ON":
            continue
        chosen[band] = row
    first = chosen["2.4G"] or {}
    second = chosen["5G"] or {}
    return (
        str(first.get("ssidName") or ""),
        str(second.get("ssidName") or ""),
        str(first.get("passPhrase") or ""),
        str(second.get("passPhrase") or ""),
    )


def _radio_state(rows: list) -> str:
    states = [str(row.get("wlsOnOff") or "").upper() for row in rows if isinstance(row, dict)]
    if "ON" in states:
        return "ON"
    return states[0] if states else ""


def _radio_channels(rows: list) -> str:
    labels = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        band = str(row.get("band") or "")
        channel = str(row.get("wlsCurrentChannel") or "").strip()
        if band and channel:
            labels.append(f"{band} {channel}")
    return " · ".join(labels)


def _first(payload: object) -> dict:
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        return payload[0]
    if isinstance(payload, dict):
        return payload
    return {}


def _json(host: str, path: str, cookie: str, source: str, device: str) -> object:
    _status, _headers, body = _exchange(host, path, source, device, cookies=cookie)
    try:
        return json.loads(body or "null")
    except json.JSONDecodeError:
        return None


def _login(
    host: str,
    username: str,
    password: str,
    cookie: str,
    pre_session: str,
    source: str,
    device: str,
) -> tuple[str, str]:
    body = urlencode({"usr": username, "pwd": password, "preSession": pre_session}).encode("ascii")
    _status, headers, text = _exchange(
        host,
        "/goform/login",
        source,
        device,
        method="POST",
        body=body,
        cookies=cookie,
    )
    return text, _merge_cookie(cookie, headers.get("cookie", ""))


def _exchange(
    host: str,
    path: str,
    source: str,
    device: str,
    method: str = "GET",
    body: bytes = b"",
    cookies: str = "",
    timeout: float = 12,
) -> tuple[int, dict[str, str], str]:
    if not path.startswith("/"):
        path = "/" + path
    data = b""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        if _IFACE.fullmatch(device or ""):
            sock.setsockopt(socket.SOL_SOCKET, 25, device.encode("ascii") + b"\0")
        if source:
            sock.bind((source, 0))
        sock.connect((host, 80))
        header = f"{method} {path} HTTP/1.0\r\nHost: {host}\r\nConnection: close\r\n"
        if cookies:
            header += f"Cookie: {cookies}\r\n"
        if body:
            header += "Content-Type: application/x-www-form-urlencoded\r\n"
            header += f"Content-Length: {len(body)}\r\n"
        sock.sendall(header.encode("ascii") + b"\r\n" + body)
        while len(data) < 900_000:
            try:
                chunk = sock.recv(65536)
            except socket.timeout:
                break
            if not chunk:
                break
            data += chunk
    except OSError:
        if not data:
            return 0, {}, ""
    finally:
        sock.close()
    return _parse(data)


def _parse(data: bytes) -> tuple[int, dict[str, str], str]:
    text = data.decode("utf-8", "replace").replace("\r", "")
    lines = text.split("\n")
    status = 0
    if lines and len(lines[0].split()) >= 2 and lines[0].split()[1].isdigit():
        status = int(lines[0].split()[1])
    headers: dict[str, str] = {}
    cookies: list[str] = []
    body_at = len(lines)
    for index, line in enumerate(lines[1:], start=1):
        if line == "":
            body_at = index + 1
            break
        if line.lstrip().startswith("<"):
            body_at = index
            break
        if ":" not in line:
            body_at = index
            break
        name, value = line.split(":", 1)
        key = name.strip().lower()
        if key == "set-cookie":
            cookies.append(value.strip().split(";", 1)[0])
        else:
            headers[key] = value.strip()
    if cookies:
        headers["cookie"] = "; ".join(cookies)
    return status, headers, "\n".join(lines[body_at:]).strip()


def _cookie_value(cookie: str, name: str) -> str:
    prefix = name + "="
    for part in cookie.split(";"):
        item = part.strip()
        if item.startswith(prefix):
            return item[len(prefix):]
    return ""


def _merge_cookie(left: str, right: str) -> str:
    found: dict[str, str] = {}
    for part in f"{left}; {right}".split(";"):
        item = part.strip()
        if "=" not in item:
            continue
        name, value = item.split("=", 1)
        found[name] = value
    return "; ".join(f"{name}={value}" for name, value in found.items())
