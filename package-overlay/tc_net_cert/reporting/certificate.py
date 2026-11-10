"""TECHCORP Lab certificate face. The stored JSON is unchanged."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from html import escape

from tc_net_cert.core.models import CertificationReport, FlowResult, Verdict
from tc_net_cert.dut.broadcom import commercial_model, uncorrectable_total
from tc_net_cert.reporting.reference import expected_window, load_reference, ont_state_expectation, ont_state_passes, optical_chart

_WEEKDAYS = ("Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo")
_MONTHS = (
    "Enero",
    "Febrero",
    "Marzo",
    "Abril",
    "Mayo",
    "Junio",
    "Julio",
    "Agosto",
    "Septiembre",
    "Octubre",
    "Noviembre",
    "Diciembre",
)
_MINIMUM = {"gigabit": 900.0, "1G": 900.0, "100M": 90.0, "2.5G": 2250.0}
_MEASURED = {"simultaneous", "throughput", "throughput-single", "bidirectional"}


def certificate_sheet(report: CertificationReport, reference: dict | None = None) -> dict:
    moment = _parse(report.started_at_local or report.started_at_utc)
    links = _links(report)
    numbers = [item["mbps"] for item in links if item["mbps"] is not None]
    passed = sum(1 for item in links if item["state"] == "PASS")
    total = len(links)
    average = sum(numbers) / len(numbers) if numbers else None
    minimum = _MINIMUM.get(report.profile, _MINIMUM.get(report.speed_profile, 900.0))
    approved = report.global_verdict == Verdict.PASS
    speed = _link_speed(report)
    gateway = _gateway(report)
    model = commercial_model(report.dut.brand, report.dut.model) or report.dut.model or "—"
    kind = {"ont": "ONT (GPON)", "emta": "EMTA", "switch": "Switch"}.get(report.dut.type, report.dut.type or "—")
    summary = _summary(report.global_verdict.value, passed, total, average, minimum)
    ont = report.dut.type == "ont"
    ref = reference or load_reference(
        model="" if model == "—" else model,
        kind=report.dut.type,
        station=getattr(report, "station", "") or "",
    )
    cpe_rows = _ont_rows(report, ref) if ont else []
    emta_rows = _emta_rows(report, ref) if report.dut.type == "emta" else []
    rx_value = _first_number(report.dut.optical_rx)
    tx_value = _first_number(report.dut.optical_tx)
    return {
        "order": report.run_id,
        "serial": report.dut.serial or "—",
        "mac": (report.dut.mac or "—").upper(),
        "long_date": _long_date(moment),
        "short_stamp": _short_stamp(moment),
        "approved": approved,
        "headline": "APROBADO" if approved else "REPROBADO",
        "stamp": "OK!" if approved else "NO!",
        "summary": summary,
        "link_count": total,
        "minimum": _mbps(minimum),
        "identity": [
            ("Equipo", "Serial (SN)", report.dut.serial or "—", "Identify", "OK" if report.dut.serial else "—"),
            ("Equipo", "MAC", (report.dut.mac or "—").upper(), "Identify", "OK" if report.dut.mac else "—"),
            *(
                [
                    ("Equipo B", "Serial (SN)", report.bench_duts["B"].serial or "—", "Identify", "OK" if report.bench_duts["B"].serial else "—"),
                    ("Equipo B", "MAC", (report.bench_duts["B"].mac or "—").upper(), "Identify", "OK" if report.bench_duts["B"].mac else "—"),
                ]
                if report.bench_scope == "A+B"
                and report.bench_duts.get("B")
                and (report.bench_duts["B"].serial or report.bench_duts["B"].mac)
                else []
            ),
            ("Equipo", "Modelo", model, "Basic Information", "INFO"),
            ("Equipo", "Device Type", report.dut.device_type or model, "Basic Information", "INFO"),
            ("Equipo", "Hardware Version", report.dut.hardware or "—", "Basic Information", "INFO"),
            ("Equipo", "Software Version", report.dut.firmware or "—", "Basic Information", "INFO"),
            ("Óptica", "TX Optical Power", report.dut.optical_tx or "—", "Optical Information", "INFO"),
            ("Óptica", "RX Optical Power", report.dut.optical_rx or "—", "Optical Information", "INFO"),
            ("Óptica", "Working Voltage", report.dut.optical_voltage or "—", "Optical Information", "INFO"),
            ("Óptica", "Bias Current", report.dut.optical_bias or "—", "Optical Information", "INFO"),
            ("Óptica", "Working Temperature", report.dut.optical_temperature or "—", "Optical Information", "INFO"),
            ("Wi-Fi", "WLAN Status", report.dut.wlan_status or "—", "WLAN", "INFO"),
            ("Wi-Fi", "WLAN Channel", report.dut.wlan_channel or "—", "WLAN", "INFO"),
            ("Wi-Fi", "SSID 2.4", report.dut.ssid_24 or "—", "WLAN", "INFO"),
            ("Wi-Fi", "SSID 5", report.dut.ssid_50 or "—", "WLAN", "INFO"),
            ("Equipo", "Tipo producto", kind, "—", "INFO"),
            ("Equipo", "IP CPE", gateway, "—", "INFO"),
            ("Enlace", "Link speed", speed, "PHY negociado", "INFO"),
            ("Enlace", "Camino", "Ethernet", "—", "INFO"),
        ],
        "failures": _failures(report),
        "links": links,
        "wifi": _wifi_rows(report),
        "global_measured": summary,
        "global_reference": f"{passed}/{total} PASS" if total else "—",
        "global_state": report.global_verdict.value,
        "sum_mbps": _mbps(sum(numbers)) if numbers else "N/A",
        "avg_mbps": _mbps(average) if average is not None else "N/A",
        "max_mbps": _mbps(max(numbers)) if numbers else "N/A",
        "min_mbps": _mbps(min(numbers)) if numbers else "N/A",
        "avg_state": "PASS" if average is not None and average >= minimum else ("NOT RUN" if average is None else "FAIL"),
        "min_state": "PASS" if numbers and min(numbers) >= minimum else ("NOT RUN" if not numbers else "FAIL"),
        "duration": _duration(links),
        "criterion": f"Avg ≥ {minimum:.0f} Mbps · enlace ≥ {minimum:.0f} Mbps",
        "year": moment.year if moment else 2026,
        "emta": report.dut.type == "emta",
        "ont": ont,
        "rows": emta_rows,
        "physical_rows": _emta_physical(report, ref) if report.dut.type == "emta" else [],
        "rf_chart": _rf_chart(report) if report.dut.type == "emta" else "",
        "ds_rows": _rf_table(report.dut.ds_rows) if report.dut.type == "emta" else [],
        "us_rows": _rf_table(report.dut.us_rows) if report.dut.type == "emta" else [],
        "ds_correctable_total": _column_total(report.dut.ds_rows, "correctable") if report.dut.type == "emta" else "",
        "ds_uncorrectable_total": _column_total(report.dut.ds_rows, "uncorrectable") if report.dut.type == "emta" else "",
        "cpe_rows": cpe_rows,
        "tally": _tally(cpe_rows),
        "emta_tally": _yes_tally(emta_rows) if report.dut.type == "emta" else "",
        "percent": _percent(cpe_rows),
        "record_id": _record_id(report),
        "security_hash": _security_hash(report),
        "headline_cpe": "CERTIFICADO" if approved else "NO CERTIFICADO",
        "reference_name": str(ref.get("name") or "ont-gpon"),
        "throughput_rows": _throughput_rows(report, ref) if ont else [],
        "wan_rows": [row.model_dump() for row in report.dut.wan_rows] if ont else [],
        "optical_chart": optical_chart(
            rx_value,
            tx_value,
            ref,
            model=report.dut.model or "",
            ont_state=report.dut.ont_state or "",
            link=_ethernet_link(report),
            detail={
                "ont_id": report.dut.ont_id,
                "pon_mode": report.dut.pon_mode,
                "optic_link": report.dut.optic_link,
                "los_status": report.dut.los_status,
                "optic_distance": report.dut.optic_distance,
                "registration": report.dut.registration,
                "olt_service": report.dut.olt_service,
                "ems_status": report.dut.ems_status,
                "acs_status": report.dut.acs_status,
                "uptime": report.dut.uptime,
            },
        )
        if ont
        else "",
    }


def _note_row(add, section: str, name: str, value: str) -> None:
    """Portal detail that does not change the score. An empty read is omitted."""
    text = " ".join(str(value or "").split())
    if not text or text in {"--", "nok"}:
        return
    add(section, name, text[:120], "—", "—", scored=False)


def _ont_rows(report: CertificationReport, reference: dict) -> list[dict]:
    """CPE sheet from the TECHCORP ONT report. Missing portal fields stay SIN DATO.

    Wi-Fi keys are not printed. Jitter, packet loss and the O5 state are not
    invented. Throughput is the LAN iperf3 result against the reference profile.
    """
    minimum = float(reference["throughput_mbps"])
    rows: list[dict] = []

    def add(section: str, name: str, read: str, expected: str, state: str, scored: bool = True) -> None:
        rows.append(
            {
                "section": section,
                "name": name,
                "read": read or "SIN DATO",
                "expected": expected,
                "state": state,
                "scored": scored and expected != "—",
            }
        )

    def present(section: str, name: str, value: str, score_key: str) -> None:
        text = (value or "").strip()
        add(
            section,
            name,
            text or "SIN DATO",
            "Presente",
            "CUMPLE" if text else "SIN DATO",
            scored=_counts_score(reference, score_key),
        )

    def ranged(section: str, name: str, value: str, low: float | None, high: float | None, expected: str, score_key: str) -> None:
        scored = _counts_score(reference, score_key)
        rules = reference.get("rules") if isinstance(reference.get("rules"), dict) else {}
        if rules.get(score_key) == "exists":
            text = (value or "").strip()
            add(section, name, text or "SIN DATO", "Presente", "CUMPLE" if text else "SIN DATO", scored=scored)
            return
        number = _first_number(value)
        if number is None or (low is None and high is None):
            add(section, name, "SIN DATO" if number is None else value.strip(), expected, "SIN DATO", scored=scored)
            return
        ok = (low is None or number >= low) and (high is None or number <= high)
        add(section, name, value.strip(), expected, "CUMPLE" if ok else "NO CUMPLE", scored=scored)

    brand = (report.dut.brand or "").strip()
    model = (report.dut.model or "").strip()
    identity = " ".join(part for part in (brand, model) if part) or "SIN DATO"
    add("Identificación", "Marca / modelo", identity, "Presente", "CUMPLE" if identity != "SIN DATO" else "SIN DATO", scored=_counts_score(reference, "brand", False))
    add("Identificación", "Tecnología", "GPON", 'igual a "GPON"', "CUMPLE", scored=_counts_score(reference, "gpon", False))
    present("Identificación", "Número de serie", report.dut.serial, "serial")
    present("Identificación", "Dirección MAC", (report.dut.mac or "").upper(), "mac")
    dut_b = report.bench_duts.get("B") if report.bench_duts else None
    if report.bench_scope == "A+B" and dut_b and (dut_b.serial or dut_b.mac):
        add(
            "Identificación",
            "Banco B — SN",
            dut_b.serial,
            "Presente",
            "CUMPLE" if dut_b.serial else "SIN DATO",
            scored=_counts_score(reference, "serial", False),
        )
        add(
            "Identificación",
            "Banco B — MAC",
            (dut_b.mac or "").upper(),
            "Presente",
            "CUMPLE" if dut_b.mac else "SIN DATO",
            scored=_counts_score(reference, "mac", False),
        )
        b_model = " ".join(part for part in ((dut_b.brand or "").strip(), (dut_b.model or "").strip()) if part)
        if b_model:
            add("Identificación", "Banco B — modelo", b_model, "Presente", "CUMPLE", scored=False)
    add("Identificación", "IP de gestión", _gateway(report), "Presente", "CUMPLE" if _gateway(report) not in {"", "—"} else "SIN DATO", scored=_counts_score(reference, "mgmt_ip", False))
    present("Wi-Fi", "Wi-Fi 2.4GHz (SSID)", report.dut.ssid_24, "ssid_24")
    add("Wi-Fi", "Contraseña Wi-Fi", (report.dut.wifi_password_24 or "").strip() or "SIN DATO", "Presente", "CUMPLE" if (report.dut.wifi_password_24 or "").strip() else "SIN DATO", scored=False)
    present("Wi-Fi", "Wi-Fi 5GHz (SSID)", report.dut.ssid_50, "ssid_50")
    add("Wi-Fi", "Clave 5 GHz", (report.dut.wifi_password_50 or "").strip() or "SIN DATO", "Presente", "CUMPLE" if (report.dut.wifi_password_50 or "").strip() else "SIN DATO", scored=False)
    measured = [item for item in _links(report) if item.get("mbps") is not None]
    if not measured:
        add("QoS", "Throughput", "SIN DATO", f"≥ {minimum:.0f} Mbps", "SIN DATO", scored=_counts_score(reference, "throughput"))
    else:
        numbers = [float(item["mbps"]) for item in measured]
        lowest = min(numbers)
        average = sum(numbers) / len(numbers)
        state = "CUMPLE" if lowest >= minimum else "NO CUMPLE"
        add("QoS", "Throughput", f"{average:.2f} Mbps", f"≥ {minimum:.0f} Mbps", state, scored=_counts_score(reference, "throughput"))
    jitter, loss = _qos_numbers(report)
    limit_jitter = float(reference["jitter_ms"])
    limit_loss = float(reference["loss_percent"])
    if jitter is None:
        add("QoS", "Jitter", "SIN DATO", f"≤ {_trim(limit_jitter)} ms", "SIN DATO", scored=_counts_score(reference, "jitter"))
    else:
        add(
            "QoS",
            "Jitter",
            f"{jitter:.2f} ms",
            f"≤ {_trim(limit_jitter)} ms",
            "CUMPLE" if jitter <= limit_jitter else "NO CUMPLE",
            scored=_counts_score(reference, "jitter"),
        )
    if loss is None:
        add("QoS", "Packet Loss", "SIN DATO", f"≤ {_trim(limit_loss)} %", "SIN DATO", scored=_counts_score(reference, "loss"))
    else:
        add(
            "QoS",
            "Packet Loss",
            f"{loss:.2f} %",
            f"≤ {_trim(limit_loss)} %",
            "CUMPLE" if loss <= limit_loss else "NO CUMPLE",
            scored=_counts_score(reference, "loss"),
        )
    ranged(
        "Óptica GPON",
        "RX Optical Power",
        report.dut.optical_rx,
        _bound(reference, "rx_low"),
        _bound(reference, "rx_high"),
        expected_window(reference.get("rx_low"), reference.get("rx_high"), "dBm"),
        "rx",
    )
    ranged(
        "Óptica GPON",
        "TX Optical Power",
        report.dut.optical_tx,
        _bound(reference, "tx_low"),
        _bound(reference, "tx_high"),
        expected_window(reference.get("tx_low"), reference.get("tx_high"), "dBm"),
        "tx",
    )
    expected_state = ont_state_expectation(reference)
    raw_state = (report.dut.ont_state or "").strip().upper()
    read_state = raw_state.split()[0] if raw_state else ""
    phrase = {
        "O1": "Estado inicial",
        "O2": "Espera",
        "O3": "Número de serie",
        "O4": "Ranging",
        "O5": "Operación (aprovisionado)",
        "O6": "POPUP",
        "O7": "Parada de emergencia",
    }.get(read_state, "")
    shown_state = f"{read_state} · {phrase}" if phrase else read_state
    if not read_state:
        add("Óptica GPON", "Estado ONT", "SIN DATO", expected_state, "SIN DATO", scored=_counts_score(reference, "ont_state"))
    else:
        add(
            "Óptica GPON",
            "Estado ONT",
            shown_state,
            expected_state,
            "CUMPLE" if ont_state_passes(reference, raw_state) else "NO CUMPLE",
            scored=_counts_score(reference, "ont_state"),
        )
    _note_row(add, "Estado GPON", "Registro en la OLT", report.dut.registration)
    _note_row(add, "Estado GPON", "Servicio de la OLT", report.dut.olt_service)
    _note_row(add, "Estado GPON", "EMS", report.dut.ems_status)
    _note_row(add, "Estado GPON", "ACS", report.dut.acs_status)
    _note_row(add, "Óptica GPON", "Enlace óptico", report.dut.optic_link)
    _note_row(add, "Óptica GPON", "Módulo óptico", " ".join(part for part in (report.dut.optic_vendor, report.dut.optic_module) if part))
    _note_row(add, "Identificación", "Descripción", report.dut.description)
    _note_row(add, "Identificación", "Vendor", report.dut.vendor_id)
    _note_row(add, "Sistema", "Fecha de software", report.dut.release_time)
    present("Sistema", "Versión de hardware", report.dut.hardware, "hardware")
    present("Sistema", "Versión de software", report.dut.firmware, "software")
    ranged(
        "Reglas del modelo",
        "Working Voltage",
        report.dut.optical_voltage,
        _bound(reference, "voltage_low"),
        _bound(reference, "voltage_high"),
        expected_window(reference.get("voltage_low"), reference.get("voltage_high"), "mV"),
        "voltage",
    )
    ranged(
        "Reglas del modelo",
        "Bias Current",
        report.dut.optical_bias,
        _bound(reference, "bias_low"),
        _bound(reference, "bias_high"),
        expected_window(reference.get("bias_low"), reference.get("bias_high"), "mA"),
        "bias",
    )
    ranged(
        "SISTEMA",
        "Working Temperature",
        report.dut.optical_temperature,
        _bound(reference, "temperature_low"),
        _bound(reference, "temperature_high"),
        expected_window(reference.get("temperature_low"), reference.get("temperature_high"), "°C"),
        "temperature",
    )
    add("Trazabilidad", "ID registro", _record_id(report), "—", "—", scored=False)
    from tc_net_cert.config.tool_registry import tool_trace_label

    add("Trazabilidad", "Herramienta certificadora", tool_trace_label(), "—", "—", scored=False)
    add("Trazabilidad", "Nodo plataforma", "TECHCORP LAB", "—", "—", scored=False)
    add("Trazabilidad", "Hash seguridad", _security_hash(report)[:32] + "…", "—", "—", scored=False)
    return rows


def _wifi_measurements(report: CertificationReport) -> list[dict]:
    """5 GHz first, then 2.4. A missing window stays empty, never 0 Mbps."""
    found = {
        "5": {"mbps": None, "note": "", "target": "", "ssid": ""},
        "2.4": {"mbps": None, "note": "", "target": "", "ssid": ""},
    }
    for flow in report.flows:
        if flow.kind != "wifi":
            continue
        source = (flow.source or "").upper()
        if "2.4" in source:
            band = "2.4"
        elif source.endswith("5") or "5G" in source:
            band = "5"
        else:
            continue
        slot = found[band]
        if (flow.destination or "").startswith("LAN"):
            slot["target"] = flow.destination
        ssid = (flow.assessment or "").strip()
        if ssid and ssid not in {"NONE", "PASS", "FAIL", "SKIPPED", "NOT_RUN", "N/A"}:
            slot["ssid"] = ssid
        if flow.mbps_avg is not None and flow.mbps_avg > 0:
            slot["mbps"] = float(flow.mbps_avg)
        elif flow.reasons and slot["mbps"] is None:
            slot["note"] = flow.reasons[0]
    rows = []
    for band in ("5", "2.4"):
        slot = found[band]
        label = "5.0" if band == "5" else "2.4"
        name = f"Wi-Fi {label} GHz"
        if slot["ssid"]:
            name += f" {slot['ssid']}"
        if slot["target"]:
            name += f" → {slot['target']}"
        rows.append({"band": band, "label": label, "name": name, **slot})
    return rows


def _throughput_rows(report: CertificationReport, reference: dict) -> list[dict]:
    """Sheet 2. A missing Wi-Fi run stays 'No se hizo la prueba', never 0 Mbps."""
    minimum = float(reference["throughput_mbps"])
    rows: list[dict] = []
    measured = [item for item in _links(report) if item.get("mbps") is not None]
    if not measured:
        rows.append(
            {
                "name": "LAN",
                "read": "No se hizo la prueba",
                "reference": f"≥ {minimum:.0f} Mbps",
                "state": "SIN DATO",
            }
        )
    else:
        for item in measured:
            mbps = float(item["mbps"])
            reason = str(item.get("reason") or "")
            rows.append(
                {
                    "name": item["parameter"],
                    "read": item["measured"],
                    "reference": reason or f"≥ {minimum:.0f} Mbps",
                    "state": "NO CUMPLE" if reason or mbps < minimum else "CUMPLE",
                }
            )
    limits = {"2.4": float(reference.get("wifi_24_mbps") or 0), "5": float(reference.get("wifi_50_mbps") or 0)}
    for item in _wifi_measurements(report):
        limit = limits[item["band"]]
        if item["mbps"] is None:
            rows.append(
                {
                    "name": item["name"],
                    "read": item["note"] or "No se hizo la prueba",
                    "reference": f"≥ {limit:.0f} Mbps" if limit else "informativo",
                    "state": "SIN DATO",
                }
            )
            continue
        if limit:
            state = "CUMPLE" if item["mbps"] >= limit else "NO CUMPLE"
            expected = f"≥ {limit:.0f} Mbps"
        else:
            state = "—"
            expected = "informativo"
        rows.append(
            {
                "name": item["name"],
                "read": f"{item['mbps']:.2f} Mbps",
                "reference": expected,
                "state": state,
            }
        )
    return rows


def _tilt_row(report: CertificationReport) -> dict:
    """Spread between the highest and lowest locked downstream power."""
    powers = []
    for row in report.dut.ds_rows:
        state = (row.state or "").lower()
        if "no sintonizado" in state or "not locked" in state:
            continue
        number = _unit_number(row.power, "dBmV")
        if number is not None:
            powers.append(number)
    expected = "≤ 6 dBmV entre canal máx. y mín."
    if len(powers) < 2:
        return {"name": "Tilt DS", "read": "NO_MEDIDO", "expected": expected, "state": "NO_MEDIDO"}
    tilt = max(powers) - min(powers)
    return {
        "name": "Tilt DS",
        "read": f"{tilt:.1f} dBmV",
        "expected": expected,
        "state": "PASS" if tilt <= 6 else "FAIL",
    }


def _uncorrectable_delta(report: CertificationReport) -> tuple[str, str]:
    """Difference between the pre-traffic counter and the portal read at the end."""
    text = (report.dut.ds_uncorrectable_delta or "").strip()
    if not text.isdigit():
        return "NO_MEDIDO", "N/A"
    number = int(text)
    return str(number), "Sí" if number <= 1000 else "No"


def _dhcp_rows(rows: list[dict], report: CertificationReport) -> None:
    """LAN leases come from the test. Telephony and WAN DHCP come from the portal."""

    def add(name: str, read: str, expected: str, state: str) -> None:
        rows.append({"section": "DHCP", "name": name, "read": read or "NO_MEDIDO", "expected": expected, "state": state})

    ports = list(report.ports)
    if not ports:
        add("Concesión en los LAN", "NO_MEDIDO", "el equipo entrega IP en cada LAN", "N/A")
    for port in ports:
        facts = port.dhcp
        if facts is None or facts.verdict == Verdict.NOT_RUN:
            add(port.logical_name, "NO_MEDIDO", "concesión del equipo", "N/A")
        elif facts.success and facts.ip:
            add(port.logical_name, facts.ip, "concesión del equipo", "Sí")
        else:
            add(port.logical_name, facts.message or "sin concesión", "concesión del equipo", "No")
    telephony = (report.dut.mta_dhcp or "").strip()
    telephony_ok = telephony.lower() in {"completed", "operational", "operacional", "ok"}
    add(
        "DHCP de telefonía",
        telephony or "NO_MEDIDO",
        "Completed",
        "Sí" if telephony_ok else ("N/A" if not telephony else "No"),
    )
    wan = (report.dut.wan_dhcp or "").strip()
    add("DHCP WAN", wan or "NO_MEDIDO", "el módem obtiene IP del CMTS", "Sí" if wan else "N/A")


def _qos_detail(report: CertificationReport) -> tuple[float | None, str, float | None, str]:
    """Worst jitter and worst loss, each with the LAN pair that produced it."""
    jitter = None
    jitter_pair = ""
    loss = None
    loss_pair = ""
    for flow in report.flows:
        if flow.kind != "qos" or flow.ping is None:
            continue
        pair = f"{flow.source} → {flow.destination}"
        if flow.ping.rtt_mdev_ms is not None and (jitter is None or flow.ping.rtt_mdev_ms > jitter):
            jitter = float(flow.ping.rtt_mdev_ms)
            jitter_pair = pair
        if flow.ping.loss_percent is not None and flow.ping.transmitted and (loss is None or flow.ping.loss_percent > loss):
            loss = float(flow.ping.loss_percent)
            loss_pair = pair
    return jitter, jitter_pair, loss, loss_pair


def _qos_numbers(report: CertificationReport) -> tuple[float | None, float | None]:
    """Worst ping mdev and worst loss among the QoS pings. Missing pings stay empty."""
    jitters: list[float] = []
    losses: list[float] = []
    for flow in report.flows:
        if flow.kind != "qos" or flow.ping is None:
            continue
        if flow.ping.rtt_mdev_ms is not None:
            jitters.append(float(flow.ping.rtt_mdev_ms))
        if flow.ping.loss_percent is not None and flow.ping.transmitted:
            losses.append(float(flow.ping.loss_percent))
    return (max(jitters) if jitters else None, max(losses) if losses else None)


def _trim(value: float) -> str:
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _yes_tally(rows: list[dict]) -> str:
    yes = sum(1 for row in rows if row.get("state") == "Sí")
    no = sum(1 for row in rows if row.get("state") == "No")
    missing = sum(1 for row in rows if row.get("state") == "N/A")
    return f"{yes} Sí · {no} No · {missing} N/A · total {len(rows)}"


def _tally(rows: list[dict]) -> str:
    scored = [row for row in rows if row.get("scored")]
    ok = sum(1 for row in scored if row["state"] == "CUMPLE")
    missing = sum(1 for row in scored if row["state"] == "SIN DATO")
    bad = sum(1 for row in scored if row["state"] in {"NO CUMPLE", "OBSERVACIÓN"})
    return f"{ok}/{len(scored)} cumplen · {bad} fallan · {missing} sin dato"


def _percent(rows: list[dict]) -> str:
    scored = [row for row in rows if row.get("scored")]
    if not scored:
        return "0%"
    ok = sum(1 for row in scored if row["state"] == "CUMPLE")
    return f"{round(100 * ok / len(scored))}%"


def _record_id(report: CertificationReport) -> str:
    return _security_hash(report)[:8]


def _security_hash(report: CertificationReport) -> str:
    material = "|".join(
        (
            report.run_id,
            report.dut.serial,
            report.dut.mac,
            report.global_verdict.value,
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest().upper()


def _bound(reference: dict, key: str) -> float | None:
    value = reference.get(key)
    if value is None or value == "":
        return None
    return float(value)


def _counts_score(reference: dict, key: str, default: bool = True) -> bool:
    flags = reference.get("required")
    if isinstance(flags, dict) and key in flags:
        return bool(flags[key])
    return default


def _first_number(value: str) -> float | None:
    match = re.search(r"[-+]?\d+(?:\.\d+)?", value or "")
    if not match:
        return None
    return float(match.group(0))


def _unit_number(value: str, unit: str) -> float | None:
    text = (value or "").strip().lower()
    mark = unit.lower()
    if not text or mark not in text:
        return None
    if mark == "db" and "dbm" in text:
        return None
    return _first_number(value)


def _rf_window(rows: list[dict], name: str, value: str, low: object, high: object, unit: str) -> None:
    low_n = None if low in (None, "") else float(low)
    high_n = None if high in (None, "") else float(high)
    expected = expected_window(low_n, high_n, unit)
    number = _unit_number(value, unit)
    if number is None or (low_n is None and high_n is None):
        rows.append({"section": "RF / CABLEMODEM", "name": name, "read": "NO_MEDIDO" if number is None else value, "expected": expected, "state": "N/A"})
        return
    inside = (low_n is None or number >= low_n) and (high_n is None or number <= high_n)
    rows.append(
        {
            "section": "RF / CABLEMODEM",
            "name": name,
            "read": value,
            "expected": expected,
            "state": "Sí" if inside else "No",
        }
    )


def _rf_floor(rows: list[dict], name: str, value: str, minimum: object, unit: str) -> None:
    floor = float(minimum)
    expected = f"≥ {_trim(floor)} {unit}"
    number = _unit_number(value, unit)
    if number is None:
        rows.append({"section": "RF / CABLEMODEM", "name": name, "read": "NO_MEDIDO", "expected": expected, "state": "N/A"})
        return
    rows.append(
        {
            "section": "RF / CABLEMODEM",
            "name": name,
            "read": value,
            "expected": expected,
            "state": "Sí" if number >= floor else "No",
        }
    )


def _rf_table(rows: list) -> list[dict[str, str]]:
    """One printable line per portal channel. Frequency in MHz when the page gives hertz."""
    shown = []
    for row in rows:
        frequency = _hertz(row.frequency)
        identity = " · ".join(part for part in (row.channel_id, frequency, row.symbol_rate) if part)
        shown.append(
            {
                "channel": row.channel or "—",
                "state": row.state or "—",
                "modulation": row.modulation or "—",
                "identity": identity or "—",
                "power": row.power or "—",
                "snr": row.snr or "—",
                "correctable": row.correctable or "—",
                "uncorrectable": row.uncorrectable or "—",
            }
        )
    return shown


def _hertz(text: str) -> str:
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*hz\s*", text or "", flags=re.I)
    if not match:
        return (text or "").strip()
    hz = float(match.group(1))
    if hz < 1_000_000:
        return (text or "").strip()
    mhz = f"{hz / 1_000_000:.3f}".rstrip("0").rstrip(".")
    return f"{mhz} MHz"


def _column_total(rows: list, name: str) -> str:
    numbers = []
    for row in rows:
        text = str(getattr(row, name, "") or "")
        if text.isdigit():
            numbers.append(int(text))
    if not numbers:
        return ""
    return str(sum(numbers))


def _rf_reading(report: CertificationReport, band: str) -> tuple[str, str, str]:
    """One-line DS or US reading. An empty portal stays NO_MEDIDO."""
    if band == "ds":
        expected = "DS ≥ 16 · RX −10 … 10 dBmV · MER 28 … 35 dB"
        power = _unit_number(report.dut.ds_power, "dBmV")
        mer = _unit_number(report.dut.mer, "dB")
        count = _first_number(report.dut.ds_channels)
        parts = [part for part in (report.dut.ds_channels, report.dut.ds_power, report.dut.mer) if (part or "").strip()]
        if not parts:
            return "NO_MEDIDO", expected, "N/A"
        failed = (power is not None and not (-10 <= power <= 10)) or (mer is not None and mer < 28) or (count is not None and count <= 4)
        return " · ".join(parts), expected, "No" if failed else "Sí"
    expected = "TX 30 … 61 dBmV · US ≥ 4"
    power = _unit_number(report.dut.us_power, "dBmV")
    parts = [part for part in (report.dut.us_channels, report.dut.us_power) if (part or "").strip()]
    if not parts:
        return "NO_MEDIDO", expected, "N/A"
    failed = power is not None and (power < 30 or power > 61)
    return " · ".join(parts), expected, "No" if failed else "Sí"


def _emta_physical(report: CertificationReport, reference: dict | None = None) -> list[dict]:
    """Sheet 2 breakdown. Missing portal numbers stay NO_MEDIDO."""
    limits = reference or {}
    ds_count = _first_number(report.dut.ds_channels)
    us_count = _first_number(report.dut.us_channels)
    ds_power = _unit_number(report.dut.ds_power, "dBmV")
    us_power = _unit_number(report.dut.us_power, "dBmV")
    mer = _unit_number(report.dut.mer, "dB")
    _jitter, loss = _qos_numbers(report)
    loss_limit = float(limits.get("loss_percent", 1) or 1)

    def row(name: str, read: str, expected: str, state: str) -> dict:
        return {"name": name, "read": read or "NO_MEDIDO", "expected": expected, "state": state}

    if ds_count is None:
        tuner = row("Tuner DS", "NO_MEDIDO", "≥ 16 canales", "NO_MEDIDO")
    elif ds_count >= 16:
        tuner = row("Tuner DS", report.dut.ds_channels, "≥ 16 canales", "PASS")
    elif ds_count <= 4:
        tuner = row("Tuner DS", report.dut.ds_channels, "≥ 16 canales · ≤ 4 = FAIL", "FAIL")
    else:
        tuner = row("Tuner DS", report.dut.ds_channels, "≥ 16 canales · 5–15 = observación", "OBSERVACION")
    if us_power is None:
        pa = row("US Tx Power (PA)", "NO_MEDIDO", "30 … 61 dBmV · < 30 = FAIL", "NO_MEDIDO")
    elif us_power < 30 or us_power > 61:
        pa = row("US Tx Power (PA)", report.dut.us_power, "30 … 61 dBmV · < 30 = FAIL", "FAIL")
    else:
        pa = row("US Tx Power (PA)", report.dut.us_power, "30 … 61 dBmV · < 30 = FAIL", "PASS")
    if us_count is None:
        bond = row("Amarre US", "NO_MEDIDO", "≥ 4 PASS · 1 canal = observación", "NO_MEDIDO")
    elif us_count >= 4:
        bond = row("Amarre US", report.dut.us_channels, "≥ 4 PASS · 1 canal = observación", "PASS")
    else:
        bond = row("Amarre US", report.dut.us_channels, "≥ 4 PASS · 1 canal = observación", "OBSERVACION")
    if ds_power is None:
        rx = row("DS Rx Power", "NO_MEDIDO", "−10 … 10 dBmV", "NO_MEDIDO")
    elif -10 <= ds_power <= 10:
        rx = row("DS Rx Power", report.dut.ds_power, "−10 … 10 dBmV", "PASS")
    else:
        rx = row("DS Rx Power", report.dut.ds_power, "−10 … 10 dBmV", "OBSERVACION")
    delta_text, delta_state = _uncorrectable_delta(report)
    if delta_state == "N/A":
        fec = row("FEC / Uncorrectables", "NO_MEDIDO", "≤ 1000 en la ventana · ausente = NO_MEDIDO", "NO_MEDIDO")
    elif delta_state == "Sí":
        fec = row("FEC / Uncorrectables", delta_text, "≤ 1000 en la ventana", "PASS")
    else:
        fec = row("FEC / Uncorrectables", delta_text, "≤ 1000 en la ventana", "FAIL")
    if mer is None:
        mer_row = row("MER / SNR", "NO_MEDIDO", "≥ 28 dB · por encima de 35 también cumple", "NO_MEDIDO")
    elif mer < 28:
        mer_row = row("MER / SNR", report.dut.mer, "≥ 28 dB · por encima de 35 también cumple", "FAIL")
    else:
        mer_row = row("MER / SNR", report.dut.mer, "≥ 28 dB · por encima de 35 también cumple", "PASS")
    if loss is None:
        loss_row = row("Packet Loss", "NO_MEDIDO", f"≤ {_trim(loss_limit)} %", "NO_MEDIDO")
    elif loss <= loss_limit:
        loss_row = row("Packet Loss", f"{loss:.2f} %", f"≤ {_trim(loss_limit)} %", "PASS")
    else:
        loss_row = row("Packet Loss", f"{loss:.2f} %", f"≤ {_trim(loss_limit)} %", "FAIL")
    return [
        fec,
        tuner,
        pa,
        bond,
        _tilt_row(report),
        rx,
        mer_row,
        loss_row,
    ]


def _rf_chart(report: CertificationReport) -> str:
    """Channel bars on a level meter. An empty portal does not draw a sample trace."""
    ds_points, ds_seen = _meter_points(report.dut.ds_rows)
    us_points, us_seen = _meter_points(report.dut.us_rows)
    ds_signal = _signal_name(report.dut.ds_rows)
    ds = _unit_number(report.dut.ds_power, "dBmV")
    us = _unit_number(report.dut.us_power, "dBmV")
    ds_text = (report.dut.ds_power or "").strip() or "NO_MEDIDO"
    us_text = (report.dut.us_power or "").strip() or "NO_MEDIDO"
    model = commercial_model(report.dut.brand, report.dut.model) or (report.dut.model or "").strip() or "esta unidad"
    if not ds_points and not us_points and ds is None and us is None:
        plot = '<p class="optic-note">Sin lectura de espectro en esta unidad.</p>'
        note = f"Medido en {escape(model)}: sin canales de RF."
    else:
        plot = _rf_meter(ds_points, us_points, ds, us, ds_seen, us_seen, ds_signal)
        ofdm_note = (
            " El downstream OFDM es el bloque ancho que reporta el portal, no portadoras QAM256."
            if "OFDM" in ds_signal
            else ""
        )
        note = (
            f"Medido en {escape(model)}. La escala sigue las medidas, no toda la ventana. "
            "En subida, el número de arriba es la potencia y el de abajo es la frecuencia en MHz."
            f"{ofdm_note}"
        )
    ds_span = _span_text(ds_points)
    tilt = _tilt_text(ds_points)
    return (
        '<section class="optic">'
        "<h3>ESPECTRO DE SEÑAL DE RF</h3>"
        '<div class="optic-top"><p class="legend">'
        + ('<span class="swatch meter-ofdm"></span>Bloque OFDM ' if "OFDM" in ds_signal else "")
        + '<span class="swatch meter-ds"></span>DS dentro de ventana '
        '<span class="swatch meter-us"></span>US dentro de ventana '
        '<span class="swatch meter-bad"></span>fuera</p>'
        '<div class="chips">'
        f'<div class="chip"><small>DS {escape(ds_signal or "promedio")}</small><b>{escape(ds_text)}</b></div>'
        f'<div class="chip"><small>DS mín · máx</small><b>{escape(ds_span)}</b></div>'
        f'<div class="chip"><small>Tilt DS</small><b>{escape(tilt)}</b></div>'
        f'<div class="chip"><small>US</small><b>{escape(us_text)}</b></div>'
        f'<div class="chip"><small>US promedio</small><b>{escape(_average_text(us_points, us_text))}</b></div>'
        "</div></div>"
        f"{plot}"
        f'<p class="optic-note">{note}</p>'
        "</section>"
    )


def _signal_name(rows: list) -> str:
    """Modulation printed on the locked carriers, such as QAM256."""
    names: list[str] = []
    for row in rows or []:
        state = str(getattr(row, "state", "") or "").lower()
        if "no sintonizado" in state or "not locked" in state:
            continue
        name = str(getattr(row, "modulation", "") or "").strip()
        folded = name.lower()
        if not name or folded in {"desconocido", "unknown", "other", "unspecified"}:
            continue
        if name not in names:
            names.append(name)
    return " · ".join(names[:2])


def _meter_points(rows: list) -> tuple[list[tuple[float, float]], int]:
    """Locked channels as MHz and dBmV, plus how many rows the portal listed."""
    found: list[tuple[float, float]] = []
    seen = 0
    for row in rows or []:
        seen += 1
        state = str(getattr(row, "state", "") or "").lower()
        if "no sintonizado" in state or "not locked" in state:
            continue
        mhz = _mhz_value(str(getattr(row, "frequency", "") or ""))
        power = _unit_number(str(getattr(row, "power", "") or ""), "dBmV")
        if mhz is None or power is None:
            continue
        width = 0.0
        if "ofdm" in str(getattr(row, "modulation", "") or "").lower():
            width = _mhz_value(str(getattr(row, "bandwidth", "") or "")) or 0.0
        found.append((mhz, power, width))
    found.sort()
    return found, seen


def _average_text(points: list[tuple[float, float]], fallback: str) -> str:
    if not points:
        return fallback or "—"
    mean = sum(power for _mhz, power, *_rest in points) / len(points)
    return f"{mean:.1f} dBmV"


def _mhz_value(text: str) -> float | None:
    match = re.search(r"(-?\d+(?:\.\d+)?)\s*(khz|mhz|hz)\b", text or "", flags=re.I)
    if not match:
        return None
    value = float(match.group(1))
    unit = match.group(2).lower()
    if unit == "hz":
        value /= 1_000_000
    elif unit == "khz":
        value /= 1_000
    if value <= 0:
        return None
    return value


def _span_text(points: list[tuple[float, float]]) -> str:
    if not points:
        return "—"
    levels = [power for _mhz, power, *_rest in points]
    return f"{min(levels):.1f} · {max(levels):.1f}"


def _tilt_text(points: list[tuple[float, float]]) -> str:
    if len(points) < 2:
        return "—"
    levels = [power for _mhz, power, *_rest in points]
    return f"{max(levels) - min(levels):.1f} dB"


def _rf_meter(
    ds_points: list[tuple[float, float]],
    us_points: list[tuple[float, float]],
    ds_avg: float | None,
    us_avg: float | None,
    ds_seen: int,
    us_seen: int,
    ds_signal: str,
) -> str:
    """Two stacked level meters, the way a cable meter shows DS and US."""
    parts = [
        '<svg class="rf-meter" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 720 348" width="100%" role="img" aria-label="Medidor de espectro RF">',
        '<rect width="720" height="348" rx="12" fill="#0f172a"/>',
    ]
    _meter_pane(
        parts,
        "DOWNSTREAM",
        ds_points,
        ds_avg,
        y0=28,
        window=(-10.0, 10.0),
        fallback=(-20.0, 20.0),
        good="#22d3ee",
        seen=ds_seen,
        label_each=len(ds_points) <= 8,
        signal=ds_signal,
    )
    _meter_pane(
        parts,
        "UPSTREAM",
        us_points,
        us_avg,
        y0=188,
        window=(30.0, 61.0),
        fallback=(20.0, 70.0),
        good="#4ade80",
        seen=us_seen,
        label_each=True,
    )
    parts.append("</svg>")
    return "".join(parts)


def _meter_pane(
    parts: list[str],
    title: str,
    points: list[tuple[float, float]],
    average: float | None,
    *,
    y0: float,
    window: tuple[float, float],
    fallback: tuple[float, float],
    good: str,
    seen: int,
    label_each: bool,
    signal: str = "",
) -> None:
    left, right, top, height = 64.0, 700.0, y0 + 18.0, 108.0
    bottom = top + height
    levels = [power for _mhz, power, *_rest in points]
    if levels:
        span = max(levels) - min(levels)
        visible = max(1.2, span * 2.4)
        middle = (max(levels) + min(levels)) / 2
        y_low = middle - visible / 2
        y_high = middle + visible / 2
        if min(levels) < window[0] or max(levels) > window[1]:
            y_low = min(y_low, window[0], min(levels)) - 0.4
            y_high = max(y_high, window[1], max(levels)) + 0.4
    elif average is not None:
        y_low, y_high = average - 1.0, average + 1.0
    else:
        y_low, y_high = fallback
    if points:
        freqs = [mhz for mhz, _power, *_rest in points]
        freqs.extend(mhz + rest[0] for mhz, _power, *rest in points if rest and rest[0] >= 20)
        span = max(freqs) - min(freqs)
        pad = max(6.0, span * 0.08)
        x_low, x_high = min(freqs) - pad, max(freqs) + pad
    else:
        x_low, x_high = 0.0, 1.0

    def y_px(value: float) -> float:
        ratio = (y_high - value) / (y_high - y_low)
        return top + max(0.0, min(1.0, ratio)) * height

    def x_px(mhz: float) -> float:
        return left + (mhz - x_low) / (x_high - x_low) * (right - left)

    counted = f" · {len(points)} de {seen}" if seen > len(points) else (f" · {len(points)}" if points else "")
    if signal:
        counted = f"{counted} · {signal}"
    parts.append(f'<text x="16" y="{y0 + 12:.0f}" font-size="11" font-family="Arial" font-weight="700" fill="#e2e8f0">{title}{counted}</text>')
    parts.append(
        f'<text x="704" y="{y0 + 12:.0f}" font-size="10" font-family="Arial" fill="#94a3b8" text-anchor="end">'
        f"ventana {window[0]:.0f} … {window[1]:.0f} dBmV</text>"
    )
    parts.append(f'<rect x="{left}" y="{top}" width="{right - left}" height="{height}" fill="#111827"/>')
    covers = y_low >= window[0] and y_high <= window[1]
    if not covers:
        band_top = y_px(max(window[1], y_high))
        band_bottom = y_px(min(window[0], y_low))
        parts.append(
            f'<rect x="{left}" y="{band_top:.1f}" width="{right - left}" height="{max(0.0, band_bottom - band_top):.1f}" fill="#14532d" opacity="0.55"/>'
        )
        for limit in window:
            if y_low <= limit <= y_high:
                y = y_px(limit)
                parts.append(f'<line x1="{left}" y1="{y:.1f}" x2="{right}" y2="{y:.1f}" stroke="#4ade80" stroke-dasharray="3 3" stroke-width="1"/>')
    fine = (y_high - y_low) < 8
    for index in range(5):
        level = y_low + (y_high - y_low) * index / 4
        y = y_px(level)
        shown = f"{level:.1f}" if fine else f"{level:.0f}"
        parts.append(f'<line x1="{left}" y1="{y:.1f}" x2="{right}" y2="{y:.1f}" stroke="#1e293b"/>')
        parts.append(
            f'<text x="58" y="{y + 3:.1f}" font-size="9" font-family="Arial" fill="#94a3b8" text-anchor="end">{shown}</text>'
        )
    if points:
        gaps = [points[i + 1][0] - points[i][0] for i in range(len(points) - 1)]
        gaps = [item for item in gaps if item > 0]
        step = min(gaps) if gaps else (x_high - x_low)
        bar_w = max(2.4, min(12.0, (step / (x_high - x_low)) * (right - left) * 0.62))
        peaks: list[str] = []
        for mhz, power, *rest in points:
            width = rest[0] if rest else 0.0
            color = good if window[0] <= power <= window[1] else "#fb7185"
            y = y_px(power)
            if width >= 20:
                x1 = x_px(mhz)
                x2 = x_px(mhz + width)
                block = "#c4b5fd" if window[0] <= power <= window[1] else "#fb7185"
                parts.append(
                    f'<rect x="{x1:.1f}" y="{y:.1f}" width="{max(6.0, x2 - x1):.1f}" height="{max(1.0, bottom - y):.1f}" fill="{block}" opacity="0.45" stroke="{block}"/>'
                )
                mid = (x1 + x2) / 2
                parts.append(
                    f'<text x="{mid:.1f}" y="{(y + bottom) / 2:.1f}" font-size="11" font-family="Arial" font-weight="700" fill="#f8fafc" text-anchor="middle">OFDM {power:.1f} dBmV</text>'
                )
                parts.append(
                    f'<text x="{x1:.1f}" y="{bottom + 16:.1f}" font-size="10" font-family="Arial" font-weight="700" fill="#e2e8f0" text-anchor="middle">{_mhz_label(mhz)}</text>'
                )
                parts.append(
                    f'<text x="{x2:.1f}" y="{bottom + 16:.1f}" font-size="10" font-family="Arial" font-weight="700" fill="#e2e8f0" text-anchor="middle">{_mhz_label(mhz + width)}</text>'
                )
                continue
            x = x_px(mhz)
            peaks.append(f"{x:.1f},{y:.1f}")
            parts.append(
                f'<rect x="{x - bar_w / 2:.1f}" y="{y:.1f}" width="{bar_w:.1f}" height="{max(1.0, bottom - y):.1f}" fill="{color}" opacity="0.9"/>'
            )
            if label_each:
                label_y = max(top + 11, y - 5)
                parts.append(
                    f'<text x="{x:.1f}" y="{label_y:.1f}" font-size="10" font-family="Arial" font-weight="700" fill="#f8fafc" text-anchor="middle">{power:.1f}</text>'
                )
        if len(peaks) >= 2:
            parts.append(
                f'<polyline points="{" ".join(peaks)}" fill="none" stroke="#f8fafc" stroke-width="1.4"/>'
            )
        if label_each:
            for mhz, _power, *rest in points:
                if rest and rest[0] >= 20:
                    continue
                x = x_px(mhz)
                parts.append(f'<line x1="{x:.1f}" y1="{bottom}" x2="{x:.1f}" y2="{bottom + 5}" stroke="#e2e8f0"/>')
                parts.append(
                    f'<text x="{x:.1f}" y="{bottom + 16:.1f}" font-size="10" font-family="Arial" font-weight="700" fill="#e2e8f0" text-anchor="middle">{_mhz_label(mhz)}</text>'
                )
        else:
            for mhz in _frequency_ticks(x_low, x_high):
                x = x_px(mhz)
                if x < left + 8 or x > right - 8:
                    continue
                parts.append(f'<line x1="{x:.1f}" y1="{bottom}" x2="{x:.1f}" y2="{bottom + 4}" stroke="#64748b"/>')
                parts.append(
                    f'<text x="{x:.1f}" y="{bottom + 14:.1f}" font-size="9" font-family="Arial" fill="#94a3b8" text-anchor="middle">{mhz:.0f}</text>'
                )
    elif average is not None:
        color = good if window[0] <= average <= window[1] else "#fb7185"
        x = (left + right) / 2
        y = y_px(average)
        parts.append(f'<rect x="{x - 6:.1f}" y="{y:.1f}" width="12" height="{max(1.0, bottom - y):.1f}" fill="{color}"/>')
        parts.append(
            f'<text x="{x:.1f}" y="{bottom + 14:.1f}" font-size="9" font-family="Arial" fill="#94a3b8" text-anchor="middle">promedio</text>'
        )
    else:
        parts.append(
            f'<text x="{(left + right) / 2:.0f}" y="{(top + bottom) / 2:.0f}" font-size="11" font-family="Arial" fill="#64748b" text-anchor="middle">Sin canales</text>'
        )
    if not (points and label_each):
        parts.append(f'<text x="{right}" y="{bottom + 14:.1f}" font-size="9" font-family="Arial" fill="#64748b" text-anchor="end">MHz</text>')


def _mhz_label(mhz: float) -> str:
    if abs(mhz - round(mhz)) < 0.05:
        return f"{mhz:.0f}"
    return f"{mhz:.1f}"


def _frequency_ticks(low: float, high: float) -> list[float]:
    span = max(1.0, high - low)
    step = 100.0
    for candidate in (1, 2, 5, 10, 20, 25, 50, 100):
        if span / candidate <= 6:
            step = float(candidate)
            break
    start = (int(low // step) + 1) * step
    ticks = []
    value = start
    while value < high and len(ticks) < 8:
        ticks.append(value)
        value += step
    return ticks


def _shown_brand(brand: str) -> str:
    """The lab prints HISTRON. The portal logo says Hitron Technologies."""
    text = (brand or "").strip()
    if text.upper() in {"HITRON", "HISTRIN"}:
        return "HISTRON"
    return text


def _emta_rows(report: CertificationReport, reference: dict | None = None) -> list[dict]:
    """Same sections as the TECHCORP EMTA sheet, plus the measured LAN throughput.

    RF values stay NO_MEDIDO until the portal itself shows them. The sample
    certificate's numbers are not copied onto this unit.
    """
    rows: list[dict] = []

    def add(section: str, name: str, read: str, expected: str, state: str) -> None:
        rows.append({"section": section, "name": name, "read": read or "NO_MEDIDO", "expected": expected, "state": state})

    def present(section: str, name: str, value: str) -> None:
        text = (value or "").strip()
        add(section, name, text or "NO_MEDIDO", "Presente", "Sí" if text else "N/A")

    present("IDENTIFICACIÓN", "Dirección MAC", (report.dut.mac or "").upper())
    present("IDENTIFICACIÓN", "Marca", _shown_brand(report.dut.brand))
    model = commercial_model(report.dut.brand, report.dut.model) or (report.dut.model or "").strip()
    kind = (report.dut.device_type or "").strip()
    shown_model = f"{model} / {kind}" if model and kind and kind != model else (model or kind)
    present("IDENTIFICACIÓN", "Modelo / Device Type", shown_model)
    present("IDENTIFICACIÓN", "Número de serie", report.dut.serial)
    present("HARDWARE / SOFTWARE", "Versión de hardware", report.dut.hardware)
    present("HARDWARE / SOFTWARE", "Versión de software", report.dut.firmware)
    if (report.dut.uptime or "").strip():
        present("HARDWARE / SOFTWARE", "Tiempo de funcionamiento", report.dut.uptime)
    if (report.dut.cm_ip or "").strip():
        present("RF / CABLEMODEM", "IP del cable modem", report.dut.cm_ip)
    limits = reference or {}
    docsis = (report.dut.docsis_status or "").strip()
    folded = docsis.lower()
    docsis_ok = "operational" in folded or "online" in folded or "operacional" in folded
    add(
        "RF / CABLEMODEM",
        "Estado DOCSIS",
        docsis or "NO_MEDIDO",
        "uno de: operational, online, operacional",
        "Sí" if docsis_ok else ("N/A" if not docsis else "No"),
    )
    present("RF / CABLEMODEM", "Canales downstream", report.dut.ds_channels)
    _rf_window(
        rows,
        "Downstream power",
        report.dut.ds_power,
        limits.get("ds_low", -10),
        limits.get("ds_high", 10),
        "dBmV",
    )
    accumulated = uncorrectable_total(report.dut.ds_rows)
    if accumulated is None:
        add("RF / CABLEMODEM", "Incorregibles DS acumulados", "NO_MEDIDO", "lectura del portal", "N/A")
    else:
        add("RF / CABLEMODEM", "Incorregibles DS acumulados", str(accumulated), "acumulado del portal, no es la ventana", "Sí")
    delta_text, delta_state = _uncorrectable_delta(report)
    add("RF / CABLEMODEM", "DS Uncorrectables_Delta", delta_text, "≤ 1000 en la ventana", delta_state)
    add("RF / CABLEMODEM", "Lectura general DS", *_rf_reading(report, "ds"))
    add("RF / CABLEMODEM", "Lectura general US", *_rf_reading(report, "us"))
    present("RF / CABLEMODEM", "Canales upstream", report.dut.us_channels)
    _rf_window(
        rows,
        "Upstream power",
        report.dut.us_power,
        limits.get("us_low", 30),
        limits.get("us_high", 61),
        "dBmV",
    )
    mer_low = _bound(limits, "mer_low")
    if mer_low is None:
        mer_low = _bound(limits, "mer_min")
    if mer_low is None:
        mer_low = 28.0
    mer_high = _bound(limits, "mer_high")
    if mer_high is None:
        mer_high = 35.0
    _rf_floor(rows, "SNR / MER", report.dut.mer, mer_low, "dB")
    if rows and rows[-1]["name"] == "SNR / MER":
        rows[-1]["expected"] = expected_window(mer_low, mer_high, "dB")
    grade = "APROBADO" if report.global_verdict == Verdict.PASS else report.global_verdict.value
    add(
        "SISTEMA",
        "Clasificación lab",
        grade,
        "APROBADO cuando el veredicto global es PASS",
        "Sí" if report.global_verdict == Verdict.PASS else "No",
    )
    temp_high = float(limits.get("temperature_high", 75) or 75)
    temperature = (report.dut.optical_temperature or "").strip()
    degrees = _first_number(temperature)
    add(
        "SISTEMA",
        "Temperatura del equipo",
        temperature or "NO_MEDIDO",
        expected_window(None, temp_high, "°C") + " · el portal debe mostrarla",
        "N/A" if degrees is None else ("Sí" if degrees <= temp_high else "No"),
    )
    _dhcp_rows(rows, report)
    present("WI-FI", "2.4G Wi-Fi Name", report.dut.ssid_24)
    present("WI-FI", "2.4G Wi-Fi Password", report.dut.wifi_password_24)
    present("WI-FI", "5.0G Wi-Fi Name", report.dut.ssid_50)
    present("WI-FI", "5.0G Wi-Fi Password", report.dut.wifi_password_50)
    add("QOS / TRÁFICO", "Capacidad objetivo", _link_speed(report), "1000 Mbps · ethernet", "Sí" if _link_speed(report).startswith("1000") else "N/A")
    jitter_limit = float(limits.get("jitter_ms", 30))
    loss_limit = float(limits.get("loss_percent", 1))
    jitter, jitter_pair, loss, loss_pair = _qos_detail(report)
    if jitter is None:
        add("QOS / TRÁFICO", "Jitter", "NO_MEDIDO", f"≤ {_trim(jitter_limit)} ms · ping entre LAN", "N/A")
    else:
        shown = f"{jitter:.2f} ms"
        if jitter_pair:
            shown = f"{shown} · {jitter_pair}"
        add(
            "QOS / TRÁFICO",
            "Jitter",
            shown,
            f"≤ {_trim(jitter_limit)} ms",
            "Sí" if jitter <= jitter_limit else "No",
        )
    if loss is None:
        add("QOS / TRÁFICO", "Packet Loss", "NO_MEDIDO", f"≤ {_trim(loss_limit)} % · ping entre LAN", "N/A")
    else:
        shown = f"{loss:.2f} %"
        if loss_pair:
            shown = f"{shown} · {loss_pair}"
        add(
            "QOS / TRÁFICO",
            "Packet Loss",
            shown,
            f"≤ {_trim(loss_limit)} %",
            "Sí" if loss <= loss_limit else "No",
        )
    links = _links(report)
    measured = [item for item in links if item.get("mbps") is not None]
    if not measured:
        reason = next((item for item in report.global_reasons if "perdió la conexión" in item.lower()), "")
        add(
            "QOS / TRÁFICO",
            "Throughput",
            "NO_MEDIDO",
            reason or "iperf3 en los LAN",
            "No" if reason else "N/A",
        )
    else:
        for item in measured:
            state = "Sí" if item["state"] == "PASS" else "No"
            add("QOS / TRÁFICO", f"Throughput {item['parameter']}", item["measured"], item["reference"], state)
        numbers = [item["mbps"] for item in measured]
        minimum = float((reference or {}).get("throughput_mbps") or _MINIMUM.get(report.profile, _MINIMUM.get(report.speed_profile, 900.0)))
        average = sum(numbers) / len(numbers)
        add(
            "QOS / TRÁFICO",
            "Throughput promedio",
            _mbps(average),
            f"≥ {minimum:.0f} Mbps",
            "Sí" if average >= minimum else "No",
        )
        for item in links:
            if item.get("mbps") is not None or item["parameter"] == "Sin medición iperf3":
                continue
            add("QOS / TRÁFICO", f"Throughput {item['parameter']}", "N/A", item["reference"], "No")
    for item in _wifi_measurements(report):
        add(
            "QOS / TRÁFICO",
            f"Throughput {item['name']}",
            f"{item['mbps']:.2f} Mbps" if item["mbps"] is not None else (item["note"] or "No se hizo la prueba"),
            "informativo · 5 GHz primero, después 2.4",
            "Sí" if item["mbps"] is not None else "N/A",
        )
    add("TRAZABILIDAD", "Hash de seguridad", _security_hash(report)[:32], "Integridad", "Sí")
    add("TRAZABILIDAD", "ID de registro", _record_id(report), "ULID lab", "Sí")
    return rows


def _links(report: CertificationReport) -> list[dict]:
    minimum = _MINIMUM.get(report.profile, _MINIMUM.get(report.speed_profile, 900.0))
    chosen = [flow for flow in report.flows if flow.kind == "simultaneous" and _counts(flow)]
    if not chosen:
        chosen = [flow for flow in report.flows if flow.kind in _MEASURED and _counts(flow)]
    rows = [_link_row(flow, minimum) for flow in chosen]
    if rows:
        return rows
    return [
        {
            "section": "Matriz P2P",
            "parameter": "Sin medición iperf3",
            "measured": "N/A",
            "reference": "—",
            "state": "NOT RUN",
            "mbps": None,
            "duration": None,
        }
    ]


def _wifi_rows(report: CertificationReport) -> list[dict]:
    rows = []
    for flow in report.flows:
        if flow.kind != "wifi":
            continue
        rows.append(
            {
                "parameter": f"{flow.source} · {flow.assessment}" if flow.assessment else flow.source,
                "measured": f"{flow.mbps_avg:.2f} Mbps" if flow.mbps_avg is not None else "N/A",
                "reference": (
                    f"{flow.requested_duration_s:.0f} s · informativo"
                    if flow.mbps_avg is not None
                    else (flow.reasons[0] if flow.reasons else "No hubo medición")
                ),
                "state": "INFO" if flow.mbps_avg is not None else "—",
            }
        )
    return rows


def _counts(flow: FlowResult) -> bool:
    return flow.verdict not in {Verdict.SKIPPED, Verdict.NOT_RUN} or flow.mbps_avg is not None


def _link_row(flow: FlowResult, minimum: float) -> dict:
    crossed = flow.kind == "simultaneous" and flow.source in {"LAN3", "LAN4"}
    if flow.kind != "simultaneous":
        phase = "Medición"
    else:
        phase = "Fase 2" if crossed else "Fase 1"
    label = f"{_side(flow.source)} → {_side(flow.destination)}"
    if crossed:
        label += " [Cruzado]"
    state = flow.verdict.value if flow.mbps_avg is not None else "NOT RUN"
    reason = _failure_reason(flow)
    reference = reason or f"{phase} · ≥ {minimum:.0f} Mbps"
    if flow.mbps_avg is None and not reason and flow.reasons:
        reference = flow.reasons[0]
    return {
        "section": "Matriz P2P",
        "parameter": label,
        "measured": f"{flow.mbps_avg:.2f} Mbps" if flow.mbps_avg is not None else "N/A",
        "reference": reference,
        "state": state,
        "mbps": flow.mbps_avg,
        "duration": flow.actual_duration_s or flow.requested_duration_s,
        "reason": reason,
    }


def _failure_reason(flow: FlowResult) -> str:
    """Why a measured flow failed. A rate above the minimum is not that reason."""
    if flow.verdict != Verdict.FAIL:
        return ""
    text = " ".join(flow.reasons)
    asked = float(flow.requested_duration_s or 0)
    seconds = flow.actual_duration_s
    clock = ""
    if seconds is not None and asked > 0 and seconds + 1 < asked:
        clock = f" Duró {seconds:.0f} s de {asked:.0f} s."
    flaps = flow.error_delta.link_flaps if flow.error_delta is not None else 0
    flap = f" El enlace cayó {flaps} veces." if flaps else ""
    pair = f"{flow.source} → {flow.destination}"
    if "UNIT_POWER_OFF_OR_REBOOT" in text or "TEST_ABORTED_BY_CARRIER" in text:
        rate = f" Hasta el corte promedió {flow.mbps_avg:.0f} Mbps." if flow.mbps_avg is not None else ""
        phase = "cruzada " if flow.direction == "reverse" or flow.source in {"LAN3", "LAN4"} else ""
        return (
            f"La prueba {phase}{pair} se detuvo porque el equipo perdió el enlace "
            f"(apagado o reinicio).{clock}{flap}{rate}"
        )
    for reason in flow.reasons:
        line = reason.strip()
        if not line or line == "PASS":
            continue
        if line.startswith(("Errors ", "Delta ")):
            continue
        if ">=" in line and "minimum" in line:
            continue
        return line + clock
    if flaps:
        return f"El enlace de {pair} cayó {flaps} veces.{clock}"
    return ""


def _side(name: str) -> str:
    number = name.replace("LAN", "LAN ")
    if name in {"LAN1", "LAN2", "LAN3", "LAN4"}:
        bench = "Banco A"
    elif name in {"LAN5", "LAN6", "LAN7", "LAN8"}:
        bench = "Banco B"
    else:
        bench = "PC1" if name in {"LAN1", "LAN2"} else "PC2"
    return f"{number} ({bench})"


def _failures(report: CertificationReport) -> list[tuple[str, str, str, str, str]]:
    rows = []
    explained: set[str] = set()
    for flow in report.flows:
        reason = _failure_reason(flow)
        if not reason:
            continue
        rows.append(("Throughput", f"{flow.source} → {flow.destination}", reason, "—", "FAIL"))
        explained.add(flow.source)
        explained.add(flow.destination)
    for port in report.ports:
        reason = port.failure_reason or ""
        if port.verdict != Verdict.FAIL or not reason or port.logical_name in explained:
            continue
        if ">=" in reason and "minimum" in reason:
            continue
        rows.append(("Enlace", port.logical_name, reason, "—", "FAIL"))
    return rows


def _summary(verdict: str, passed: int, total: int, average: float | None, minimum: float) -> str:
    avg = f"{average:.2f} Mbps" if average is not None else "N/A"
    ratio = f"{passed}/{total} enlaces" if total else "sin enlaces"
    return f"{verdict} · {ratio} · avg {avg} · umbral ≥ {minimum:.0f} Mbps"


def _ethernet_link(report: CertificationReport) -> str:
    """Negotiated LAN speed. A missing link stays SIN DATO, never 0 Mbps."""
    found: list[str] = []
    for port in report.ports:
        link = port.link
        if link is None or not link.detected or not link.speed_mbps:
            continue
        duplex = (link.duplex or "").strip()
        text = f"{link.speed_mbps} Mbps" + (f" · {duplex}" if duplex else "")
        if text not in found:
            found.append(text)
    return " · ".join(found) if found else "SIN DATO"


def _link_speed(report: CertificationReport) -> str:
    speeds = {port.link.speed_mbps for port in report.ports if port.link and port.link.speed_mbps}
    if len(speeds) == 1:
        return f"{next(iter(speeds))} Mbps · ethernet"
    if speeds:
        return " · ".join(f"{speed} Mbps" for speed in sorted(speeds))
    return "N/A"


def _gateway(report: CertificationReport) -> str:
    for port in report.ports:
        if port.gateway and port.gateway.target:
            return port.gateway.target
    return "192.168.1.1"


def _duration(links: list[dict]) -> str:
    values = [item["duration"] for item in links if item.get("duration")]
    if not values:
        return "N/A"
    return f"{max(values):.0f} s"


def _mbps(value: float | None) -> str:
    if value is None:
        return "N/A"
    return f"{value:.2f} Mbps"


def _parse(value: str) -> datetime | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _long_date(moment: datetime | None) -> str:
    if moment is None:
        return "—"
    return f"{_WEEKDAYS[moment.weekday()]}, {moment.day} De {_MONTHS[moment.month - 1]} De {moment.year}"


def _short_stamp(moment: datetime | None) -> str:
    if moment is None:
        return "—"
    return f"{moment.day}/{moment.month}/{moment.year % 100:02d}, {moment:%H:%M:%S}"
