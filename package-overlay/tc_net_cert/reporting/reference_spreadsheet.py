"""Export / import reference profiles as Excel-friendly CSV (UTF-8 BOM, semicolon)."""

from __future__ import annotations

import csv
import io
import re
from typing import Any

from tc_net_cert.reporting.reference import profile_catalog, save_reference

ONT_RULE_SPECS: list[dict[str, Any]] = [
    {"section": "LECTURA DEL PORTAL", "title": "Marca / modelo", "path": "identity.brandModel", "rule": "exists", "required_key": "brand"},
    {"section": "LECTURA DEL PORTAL", "title": "Tecnología", "path": "identity.technology", "rule": "equal", "required_key": "gpon", "text_key": "", "default_text": "GPON"},
    {"section": "LECTURA DEL PORTAL", "title": "Número de serie", "path": "identity.serial", "rule": "exists", "required_key": "serial"},
    {"section": "LECTURA DEL PORTAL", "title": "Dirección MAC", "path": "identity.mac", "rule": "exists", "required_key": "mac"},
    {"section": "LECTURA DEL PORTAL", "title": "IP de gestión", "path": "identity.mgmtIp", "rule": "exists", "required_key": "mgmt_ip"},
    {"section": "LECTURA DEL PORTAL", "title": "SSID 2.4 GHz", "path": "wifi.ssid24", "rule": "exists", "required_key": "ssid_24"},
    {"section": "LECTURA DEL PORTAL", "title": "Clave 2.4 GHz", "path": "wifi.psk24", "rule": "exists", "required_key": "psk_24"},
    {"section": "LECTURA DEL PORTAL", "title": "SSID 5 GHz", "path": "wifi.ssid50", "rule": "exists", "required_key": "ssid_50"},
    {"section": "LECTURA DEL PORTAL", "title": "Clave 5 GHz", "path": "wifi.psk50", "rule": "exists", "required_key": "psk_50"},
    {"section": "LECTURA DEL PORTAL", "title": "Versión de hardware", "path": "system.hardware", "rule": "exists", "required_key": "hardware"},
    {"section": "LECTURA DEL PORTAL", "title": "Versión de software", "path": "system.software", "rule": "exists", "required_key": "software"},
    {"section": "ÓPTICA GPON", "title": "RX Optical Power", "path": "optics.rxPowerDbm", "rule": "range", "required_key": "rx", "min_key": "rx_low", "max_key": "rx_high", "unit": "dBm"},
    {"section": "ÓPTICA GPON", "title": "TX Optical Power", "path": "optics.txPowerDbm", "rule": "range", "required_key": "tx", "min_key": "tx_low", "max_key": "tx_high", "unit": "dBm"},
    {"section": "REGLAS DEL MODELO", "title": "Working Voltage", "path": "optics.voltageMv", "rule": "range", "required_key": "voltage", "min_key": "voltage_low", "max_key": "voltage_high", "unit": "mV"},
    {"section": "REGLAS DEL MODELO", "title": "Bias Current", "path": "optics.biasMa", "rule": "range", "required_key": "bias", "min_key": "bias_low", "max_key": "bias_high", "unit": "mA"},
    {
        "section": "REGLAS DEL MODELO",
        "title": "Estado ONT",
        "path": "optics.ontState",
        "rule": "one",
        "required_key": "ont_state",
        "text_key": "ont_state",
        "default_text": "O3|O4|O5",
    },
    {"section": "SISTEMA", "title": "Working Temperature", "path": "system.tempC", "rule": "range", "required_key": "temperature", "max_key": "temperature_high", "unit": "°C"},
    {"section": "QOS / TRÁFICO", "title": "Jitter", "path": "qos.jitterMs", "rule": "range", "required_key": "jitter", "max_key": "jitter_ms", "unit": "ms"},
    {"section": "QOS / TRÁFICO", "title": "Packet Loss", "path": "qos.lossPercent", "rule": "range", "required_key": "loss", "max_key": "loss_percent", "unit": "%"},
    {"section": "QOS / TRÁFICO", "title": "Throughput LAN", "path": "qos.throughputMbps", "rule": "range", "required_key": "throughput", "min_key": "throughput_mbps", "unit": "Mbps"},
]

EMTA_RULE_SPECS: list[dict[str, Any]] = [
    {"section": "LECTURA DEL PORTAL", "title": "Dirección MAC", "path": "identity.mac", "rule": "exists", "required_key": "mac"},
    {"section": "LECTURA DEL PORTAL", "title": "Modelo", "path": "identity.model", "rule": "exists", "required_key": "model_name"},
    {"section": "LECTURA DEL PORTAL", "title": "Número de serie", "path": "identity.serial", "rule": "exists", "required_key": "serial"},
    {"section": "LECTURA DEL PORTAL", "title": "Versión de hardware", "path": "system.hardware", "rule": "exists", "required_key": "hardware"},
    {"section": "LECTURA DEL PORTAL", "title": "Versión de software", "path": "system.software", "rule": "exists", "required_key": "software"},
    {"section": "LECTURA DEL PORTAL", "title": "Estado DOCSIS", "path": "docsis.status", "rule": "one", "required_key": "docsis", "default_text": "operational|online"},
    {"section": "LECTURA DEL PORTAL", "title": "SSID 2.4 GHz", "path": "wifi.ssid24", "rule": "exists", "required_key": "ssid_24"},
    {"section": "LECTURA DEL PORTAL", "title": "Clave 2.4 GHz", "path": "wifi.psk24", "rule": "exists", "required_key": "psk_24"},
    {"section": "LECTURA DEL PORTAL", "title": "SSID 5 GHz", "path": "wifi.ssid50", "rule": "exists", "required_key": "ssid_50"},
    {"section": "LECTURA DEL PORTAL", "title": "Clave 5 GHz", "path": "wifi.psk50", "rule": "exists", "required_key": "psk_50"},
    {"section": "RF DOWNSTREAM", "title": "Downstream power", "path": "rf.dsPowerDbmv", "rule": "range", "required_key": "ds", "min_key": "ds_low", "max_key": "ds_high", "unit": "dBmV"},
    {"section": "RF DOWNSTREAM", "title": "Canales downstream", "path": "rf.dsChannels", "rule": "range", "required_key": "ds_channels", "min_key": "ds_channels_min", "unit": "canales"},
    {"section": "RF DOWNSTREAM", "title": "SNR / MER", "path": "rf.merDb", "rule": "range", "required_key": "mer", "min_key": "mer_low", "max_key": "mer_high", "unit": "dB"},
    {"section": "RF DOWNSTREAM", "title": "Incorregibles", "path": "rf.uncorrectables", "rule": "range", "required_key": "uncorrectables", "max_key": "uncorrectables_max"},
    {"section": "RF UPSTREAM", "title": "Upstream power", "path": "rf.usPowerDbmv", "rule": "range", "required_key": "us", "min_key": "us_low", "max_key": "us_high", "unit": "dBmV"},
    {"section": "SISTEMA", "title": "Working Temperature", "path": "system.tempC", "rule": "range", "required_key": "temperature", "max_key": "temperature_high", "unit": "°C"},
    {"section": "QOS / TRÁFICO", "title": "Jitter", "path": "qos.jitterMs", "rule": "range", "required_key": "jitter", "max_key": "jitter_ms", "unit": "ms"},
    {"section": "QOS / TRÁFICO", "title": "Packet Loss", "path": "qos.lossPercent", "rule": "range", "required_key": "loss", "max_key": "loss_percent", "unit": "%"},
    {"section": "QOS / TRÁFICO", "title": "Throughput LAN", "path": "qos.throughputMbps", "rule": "range", "required_key": "throughput", "min_key": "throughput_mbps", "unit": "Mbps"},
]

_HEADERS = [
    "seccion",
    "parametro",
    "path",
    "tipo_regla",
    "valor_min",
    "valor_max",
    "valor_texto",
    "obligatorio",
    "unidad",
    "clave_regla",
]


def _specs(technology: str) -> list[dict[str, Any]]:
    return EMTA_RULE_SPECS if technology == "EMTA" else ONT_RULE_SPECS


def _cell(value: object) -> str:
    if value is None:
        return ""
    return str(value)


def _row_for_spec(spec: dict[str, Any], profile: dict) -> list[str]:
    key = str(spec["required_key"])
    rules = profile.get("rules") if isinstance(profile.get("rules"), dict) else {}
    required = profile.get("required") if isinstance(profile.get("required"), dict) else {}
    rule = str(rules.get(key) or spec.get("rule") or "exists")
    min_key = spec.get("min_key")
    max_key = spec.get("max_key")
    text_key = spec.get("text_key")
    min_val = profile.get(min_key) if min_key else ""
    max_val = profile.get(max_key) if max_key else ""
    if text_key:
        if text_key == "ont_state":
            allowed = profile.get("allowed_gpon_states")
            if isinstance(allowed, list) and allowed:
                text_val = "|".join(str(item) for item in allowed)
            else:
                text_val = profile.get(text_key) or spec.get("default_text") or ""
        else:
            text_val = profile.get(text_key) or spec.get("default_text") or ""
    elif rule in {"equal", "one", "pattern"}:
        text_val = spec.get("default_text") or ""
    else:
        text_val = ""
    oblig = "SI" if bool(required.get(key, False)) else "NO"
    return [
        _cell(spec.get("section")),
        _cell(spec.get("title")),
        _cell(spec.get("path")),
        rule,
        _cell(min_val),
        _cell(max_val),
        _cell(text_val),
        oblig,
        _cell(spec.get("unit") or ""),
        key,
    ]


def export_reference_csv(name: str) -> tuple[bytes, str]:
    catalog = {item["name"]: item for item in profile_catalog()}
    profile = catalog.get(name)
    if profile is None:
        raise ValueError(f"No existe el perfil de referencia '{name}'.")
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";")
    writer.writerow(["perfil", name])
    writer.writerow(["tecnologia", profile.get("technology") or "ONT"])
    writer.writerow(["modelo", profile.get("model") or name])
    writer.writerow(_HEADERS)
    for spec in _specs(str(profile.get("technology") or "ONT")):
        writer.writerow(_row_for_spec(spec, profile))
    payload = ("\ufeff" + buffer.getvalue()).encode("utf-8")
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_") or "referencia"
    return payload, f"{safe}-referencia.csv"


def _parse_float(text: str) -> float | None:
    token = text.strip().replace(",", ".")
    if not token:
        return None
    try:
        return float(token)
    except ValueError:
        return None


def import_reference_csv(content: str, *, expected_name: str = "") -> dict:
    reader = csv.reader(io.StringIO(content.lstrip("\ufeff")), delimiter=";")
    meta_name = ""
    meta_tech = "ONT"
    rows: list[list[str]] = []
    header_seen = False
    for row in reader:
        if not row:
            continue
        if len(row) >= 2 and row[0].lower() in {"perfil", "profile"}:
            meta_name = row[1].strip()
            continue
        if len(row) >= 2 and row[0].lower() in {"tecnologia", "technology"}:
            meta_tech = row[1].strip().upper()
            continue
        if row[0].lower() == "seccion" and "parametro" in [c.lower() for c in row[1:3]]:
            header_seen = True
            continue
        if header_seen:
            rows.append(row)
    name = (expected_name or meta_name).strip()
    if not name:
        raise ValueError("El archivo no indica el nombre del perfil (fila perfil;...).")
    if expected_name and meta_name and meta_name != expected_name:
        raise ValueError(f"El archivo es del perfil {meta_name}, no de {expected_name}.")
    specs = {str(s["required_key"]): s for s in _specs(meta_tech)}
    values: dict[str, Any] = {"technology": meta_tech, "rules": {}, "required": {}}
    for row in rows:
        if len(row) < 10:
            continue
        key = row[9].strip()
        spec = specs.get(key)
        if spec is None:
            continue
        rule = row[3].strip() or str(spec.get("rule") or "exists")
        values["rules"][key] = rule
        values["required"][key] = row[7].strip().upper() in {"SI", "S", "YES", "1", "TRUE"}
        min_key = spec.get("min_key")
        max_key = spec.get("max_key")
        text_key = spec.get("text_key")
        if min_key:
            values[min_key] = _parse_float(row[4])
        if max_key:
            values[max_key] = _parse_float(row[5])
        if text_key:
            values[text_key] = row[6].strip()
        elif rule in {"equal", "one", "pattern"} and row[6].strip():
            if spec.get("text_key") == "ont_state" or key == "ont_state":
                token = row[6].strip()
                values["ont_state"] = token
                if rule == "one":
                    values["allowed_gpon_states"] = [
                        part.strip() for part in token.replace(",", "|").split("|") if part.strip()
                    ]
    if meta_tech == "EMTA":
        values["ont_state"] = ""
        if values.get("mer_low") is not None:
            values["mer_min"] = values["mer_low"]
    return save_reference(name, values)
