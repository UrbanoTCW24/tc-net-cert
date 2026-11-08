"""Store one ONT certification in the lab Supabase project.

The connection file stays on this PC (mode 0600). Wi-Fi keys are written
only to that database, not to the certificate face.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

SUPABASE_ENV = "TC_NET_CERT_SUPABASE"


def config_path() -> Path:
    override = os.environ.get(SUPABASE_ENV, "").strip()
    if override:
        return Path(override)
    return Path.home() / ".config" / "tc-net-cert" / "supabase.env"


def save_run(config_path: Path, record: dict) -> str:
    """Insert or update the run. Returns an empty string, or a short error without secrets."""
    try:
        settings = _settings(config_path)
    except OSError:
        return "No se guardó en la base de datos: falta la conexión de esta PC."
    if not settings:
        return "No se guardó en la base de datos: falta la conexión de esta PC."
    try:
        import psycopg
    except ImportError:
        return "No se guardó en la base de datos: falta el cliente de base de datos."
    statement = """
        insert into public.ont_runs (
            run_id, station, operator, verdict, model, device_type, serial, mac,
            software_version, hardware_version, tx_optical_power, rx_optical_power,
            working_voltage, bias_current, working_temperature, wlan_status, wlan_channel,
            ssid_24, ssid_50, wifi_password_24, wifi_password_50, throughput
        ) values (
            %(run_id)s, %(station)s, %(operator)s, %(verdict)s, %(model)s, %(device_type)s,
            %(serial)s, %(mac)s, %(software_version)s, %(hardware_version)s,
            %(tx_optical_power)s, %(rx_optical_power)s, %(working_voltage)s, %(bias_current)s,
            %(working_temperature)s, %(wlan_status)s, %(wlan_channel)s, %(ssid_24)s, %(ssid_50)s,
            %(wifi_password_24)s, %(wifi_password_50)s, %(throughput)s::jsonb
        )
        on conflict (run_id) do update set
            station = excluded.station,
            operator = excluded.operator,
            verdict = excluded.verdict,
            model = excluded.model,
            device_type = excluded.device_type,
            serial = excluded.serial,
            mac = excluded.mac,
            software_version = excluded.software_version,
            hardware_version = excluded.hardware_version,
            tx_optical_power = excluded.tx_optical_power,
            rx_optical_power = excluded.rx_optical_power,
            working_voltage = excluded.working_voltage,
            bias_current = excluded.bias_current,
            working_temperature = excluded.working_temperature,
            wlan_status = excluded.wlan_status,
            wlan_channel = excluded.wlan_channel,
            ssid_24 = excluded.ssid_24,
            ssid_50 = excluded.ssid_50,
            wifi_password_24 = excluded.wifi_password_24,
            wifi_password_50 = excluded.wifi_password_50,
            throughput = excluded.throughput,
            recorded_at = now()
    """
    payload = {key: str(record.get(key) or "") for key in (
        "run_id", "station", "operator", "verdict", "model", "device_type", "serial", "mac",
        "software_version", "hardware_version", "tx_optical_power", "rx_optical_power",
        "working_voltage", "bias_current", "working_temperature", "wlan_status", "wlan_channel",
        "ssid_24", "ssid_50", "wifi_password_24", "wifi_password_50",
    )}
    if not payload["run_id"]:
        payload["run_id"] = None
    payload["throughput"] = json.dumps(record.get("throughput") or [])
    last_error = "No se guardó en la base de datos."
    attempts = 3
    for attempt in range(attempts):
        if attempt:
            time.sleep(2.0)
        for target in settings:
            try:
                with psycopg.connect(**target, connect_timeout=8) as connection:
                    with connection.cursor() as cursor:
                        cursor.execute(statement, payload)
                    connection.commit()
                return ""
            except Exception:
                last_error = "No se guardó en la base de datos."
    return (
        last_error
        + " Comprueba Wi‑Fi e Internet; si la prueba ya terminó, usa «Enviar no enviadas» en el historial."
    )


_stored_cache: tuple[float, set[str] | None] | None = None


def forget_listed_run_ids() -> None:
    global _stored_cache
    _stored_cache = None


def listed_run_ids(config_path: Path) -> set[str] | None:
    """Run ids already stored in Supabase. None means this PC could not ask."""
    global _stored_cache
    now = time.monotonic()
    if _stored_cache is not None and now - _stored_cache[0] < (30 if _stored_cache[1] is not None else 8):
        return _stored_cache[1]
    found = _query_run_ids(config_path)
    _stored_cache = (now, found)
    return found


def _query_run_ids(config_path: Path) -> set[str] | None:
    try:
        settings = _settings(config_path)
    except OSError:
        return None
    if not settings:
        return None
    try:
        import psycopg
    except ImportError:
        return None
    for target in settings:
        try:
            with psycopg.connect(**target, connect_timeout=4) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "select run_id from public.ont_runs where run_id is not null and run_id <> ''"
                    )
                    return {str(row[0]) for row in cursor.fetchall() if row and row[0]}
        except Exception:
            continue
    return None


def record_from_report(report: dict, *, station: str = "", operator: str = "") -> dict:
    """Build the Supabase row from a finished report.json (no Wi-Fi keys on resync)."""
    dut = report.get("dut") if isinstance(report.get("dut"), dict) else {}
    flows = []
    for flow in report.get("flows") or []:
        if not isinstance(flow, dict):
            continue
        kind = str(flow.get("kind") or "")
        if flow.get("mbps_avg") is None and kind != "wifi":
            continue
        flows.append(
            {
                "source": str(flow.get("source") or ""),
                "destination": str(flow.get("destination") or ""),
                "kind": kind,
                "mbps": flow.get("mbps_avg"),
                "verdict": str(flow.get("verdict") or ""),
            }
        )
    return {
        "run_id": str(report.get("run_id") or ""),
        "station": str(report.get("station") or station),
        "operator": str(report.get("operator") or operator),
        "verdict": str(report.get("global_verdict") or ""),
        "model": str(dut.get("model") or ""),
        "device_type": str(dut.get("device_type") or dut.get("type") or ""),
        "serial": str(dut.get("serial") or ""),
        "mac": str(dut.get("mac") or ""),
        "software_version": str(dut.get("firmware") or ""),
        "hardware_version": str(dut.get("hardware") or ""),
        "tx_optical_power": str(dut.get("optical_tx") or ""),
        "rx_optical_power": str(dut.get("optical_rx") or ""),
        "working_voltage": str(dut.get("optical_voltage") or ""),
        "bias_current": str(dut.get("optical_bias") or ""),
        "working_temperature": str(dut.get("optical_temperature") or ""),
        "wlan_status": str(dut.get("wlan_status") or ""),
        "wlan_channel": str(dut.get("wlan_channel") or ""),
        "ssid_24": str(dut.get("ssid_24") or ""),
        "ssid_50": str(dut.get("ssid_50") or ""),
        "wifi_password_24": str(dut.get("wifi_password_24") or ""),
        "wifi_password_50": str(dut.get("wifi_password_50") or ""),
        "throughput": flows,
    }


def sync_unsent_runs(
    runs_root: Path,
    *,
    env_file: Path | None = None,
    station: str = "",
    operator: str = "",
) -> dict:
    """Upload local runs missing from Supabase. Returns ok, sent, failed, error."""
    from tc_net_cert.storage.runs import list_runs

    path = env_file or config_path()
    settings_err = _settings_error(path)
    if settings_err:
        return {"ok": False, "sent": [], "failed": 0, "error": settings_err}
    stored = listed_run_ids(path)
    if stored is None:
        return {
            "ok": False,
            "sent": [],
            "failed": 0,
            "error": "No se pudo consultar la base de datos (revisa red Wi‑Fi, host pooler y contraseña en supabase.env).",
        }
    sent: list[str] = []
    failed = 0
    message = ""
    for folder in reversed(list_runs(runs_root)):
        if folder.name in stored:
            continue
        report_path = folder / "report.json"
        if not report_path.is_file():
            continue
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        error = save_run(path, record_from_report(report, station=station, operator=operator))
        if error:
            failed += 1
            message = error
            continue
        sent.append(folder.name)
        stored.add(folder.name)
    forget_listed_run_ids()
    return {"ok": failed == 0, "sent": sent, "failed": failed, "error": message}


def _settings_error(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return f"No se encontró {path}. Copia privado/supabase.env (no el .example)."
    fields: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip() or line.strip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        fields[key.strip()] = value.strip()
    if not fields.get("password", ""):
        return "Falta password= en supabase.env. Usa privado/supabase.env de la USB, no supabase.env.example."
    if not any(fields.get(f"{prefix}_host") and fields.get(f"{prefix}_user") for prefix in ("pooler", "direct")):
        return "supabase.env incompleto: faltan pooler_host/user o direct_host/user."
    return ""


def _settings(path: Path) -> list[dict[str, object]]:
    text = path.read_text(encoding="utf-8")
    fields: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip() or line.strip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        fields[key.strip()] = value.strip()
    password = fields.get("password", "")
    dbname = fields.get("dbname", "postgres")
    if not password:
        return []
    targets = []
    for prefix in ("pooler", "direct"):
        host = fields.get(f"{prefix}_host", "")
        user = fields.get(f"{prefix}_user", "")
        port = fields.get(f"{prefix}_port", "5432")
        if host and user:
            targets.append(
                {
                    "host": host,
                    "port": int(port),
                    "dbname": dbname,
                    "user": user,
                    "password": password,
                    "sslmode": "require",
                }
            )
    return targets
