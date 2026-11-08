"""Persist certification runs to Supabase (ONT legacy + lab_certification_runs).

Each cloud row uses **validation_number** ``LAB-002-000N`` (station from
config + local ``TC-000N``). The connection
file stays on this PC (mode 0600). Wi-Fi keys go only to the database rows, not
the printed certificate face.
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
    from tc_net_cert.config.tool_registry import tool_fields_for_run

    record = {**record, **tool_fields_for_run()}
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
    run_id = str(record.get("run_id") or "").strip()
    if not run_id:
        return "No se guardó en la base de datos: falta el número de validación (run_id)."
    from tc_net_cert.storage.runs import validation_number as build_validation_number

    validation_number = str(record.get("validation_number") or "").strip()
    if not validation_number:
        validation_number = build_validation_number(str(record.get("station") or ""), run_id)
    verdict = str(record.get("verdict") or record.get("global_verdict") or "")
    ont_statement = """
        insert into public.ont_runs (
            run_id, station, operator, verdict, model, device_type, serial, mac,
            software_version, hardware_version, tx_optical_power, rx_optical_power,
            working_voltage, bias_current, working_temperature, wlan_status, wlan_channel,
            ssid_24, ssid_50, wifi_password_24, wifi_password_50, throughput,
            tool_number, tool_version
        ) values (
            %(run_id)s, %(station)s, %(operator)s, %(verdict)s, %(model)s, %(device_type)s,
            %(serial)s, %(mac)s, %(software_version)s, %(hardware_version)s,
            %(tx_optical_power)s, %(rx_optical_power)s, %(working_voltage)s, %(bias_current)s,
            %(working_temperature)s, %(wlan_status)s, %(wlan_channel)s, %(ssid_24)s, %(ssid_50)s,
            %(wifi_password_24)s, %(wifi_password_50)s, %(throughput)s::jsonb,
            %(tool_number)s, %(tool_version)s
        )
        on conflict (run_id) where (run_id ~ '^LAB-[0-9]{3}-[0-9]{4}$') do update set
            station = excluded.station,
            operator = excluded.operator,
            verdict = excluded.verdict,
            tool_number = excluded.tool_number,
            tool_version = excluded.tool_version,
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
    ont_payload = {key: str(record.get(key) or "") for key in (
        "run_id", "station", "operator", "model", "device_type", "serial", "mac",
        "software_version", "hardware_version", "tx_optical_power", "rx_optical_power",
        "working_voltage", "bias_current", "working_temperature", "wlan_status", "wlan_channel",
        "ssid_24", "ssid_50", "wifi_password_24", "wifi_password_50",
        "tool_number", "tool_version",
    )}
    ont_payload["run_id"] = validation_number
    ont_payload["verdict"] = verdict
    ont_payload["throughput"] = json.dumps(record.get("throughput") or [])
    ont_payload.setdefault("tool_number", str(record.get("tool_number") or ""))
    ont_payload.setdefault("tool_version", str(record.get("tool_version") or ""))

    lab_statement = """
        insert into public.lab_certification_runs (
            validation_number, run_id, station, operator, country_code, bench_scope,
            profile, speed_profile, dut_profile, global_verdict, brand, model, device_type,
            serial, mac, software_version, hardware_version,
            started_at, finished_at, throughput, report_json,
            tool_number, tool_version
        ) values (
            %(validation_number)s, %(run_id)s, %(station)s, %(operator)s, %(country_code)s,
            %(bench_scope)s, %(profile)s, %(speed_profile)s, %(dut_profile)s, %(global_verdict)s,
            %(brand)s, %(model)s, %(device_type)s, %(serial)s, %(mac)s,
            %(software_version)s, %(hardware_version)s,
            %(started_at)s, %(finished_at)s,
            %(throughput)s::jsonb, %(report_json)s::jsonb,
            %(tool_number)s, %(tool_version)s
        )
        on conflict (validation_number) do update set
            run_id = excluded.run_id,
            station = excluded.station,
            operator = excluded.operator,
            country_code = excluded.country_code,
            bench_scope = excluded.bench_scope,
            profile = excluded.profile,
            speed_profile = excluded.speed_profile,
            dut_profile = excluded.dut_profile,
            global_verdict = excluded.global_verdict,
            brand = excluded.brand,
            model = excluded.model,
            device_type = excluded.device_type,
            serial = excluded.serial,
            mac = excluded.mac,
            software_version = excluded.software_version,
            hardware_version = excluded.hardware_version,
            started_at = excluded.started_at,
            finished_at = excluded.finished_at,
            throughput = excluded.throughput,
            report_json = excluded.report_json,
            tool_number = excluded.tool_number,
            tool_version = excluded.tool_version,
            recorded_at = now()
    """
    report_json = record.get("report_json")
    lab_payload = {
        "validation_number": validation_number,
        "run_id": run_id,
        "station": str(record.get("station") or ""),
        "operator": str(record.get("operator") or ""),
        "country_code": str(record.get("country_code") or "GT"),
        "bench_scope": str(record.get("bench_scope") or ""),
        "profile": str(record.get("profile") or ""),
        "speed_profile": str(record.get("speed_profile") or ""),
        "dut_profile": str(record.get("dut_profile") or ""),
        "global_verdict": verdict,
        "brand": str(record.get("brand") or ""),
        "model": str(record.get("model") or ""),
        "device_type": str(record.get("device_type") or ""),
        "serial": str(record.get("serial") or ""),
        "mac": str(record.get("mac") or ""),
        "software_version": str(record.get("software_version") or ""),
        "hardware_version": str(record.get("hardware_version") or ""),
        "started_at": _ts_or_none(record.get("started_at")),
        "finished_at": _ts_or_none(record.get("finished_at")),
        "throughput": json.dumps(record.get("throughput") or []),
        "report_json": json.dumps(report_json) if report_json is not None else None,
        "tool_number": str(record.get("tool_number") or ""),
        "tool_version": str(record.get("tool_version") or ""),
    }

    last_error = "No se guardó en la base de datos."

    def _db_hint(exc: BaseException) -> str:
        code = getattr(exc, "sqlstate", None) or getattr(exc, "pgcode", None) or ""
        text = str(exc).strip().replace("\n", " ")
        if len(text) > 160:
            text = text[:157] + "..."
        prefix = f" ({code})" if code else ""
        return f"{last_error}{prefix} {text}".strip()

    attempts = 3
    for attempt in range(attempts):
        if attempt:
            time.sleep(2.0)
        for target in settings:
            try:
                with psycopg.connect(**target, connect_timeout=8) as connection:
                    with connection.cursor() as cursor:
                        cursor.execute(ont_statement, ont_payload)
                    connection.commit()
                    try:
                        with connection.cursor() as cursor:
                            cursor.execute(lab_statement, lab_payload)
                        connection.commit()
                    except Exception as exc:
                        if not _missing_relation(exc):
                            try:
                                connection.rollback()
                            except Exception:
                                pass
                return ""
            except Exception as exc:
                last_error = _db_hint(exc)
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
            found: set[str] = set()
            with psycopg.connect(**target, connect_timeout=4) as connection:
                with connection.cursor() as cursor:
                    try:
                        cursor.execute(
                            """
                            select validation_number from public.lab_certification_runs
                            where validation_number is not null and validation_number <> ''
                            """
                        )
                        found.update(str(row[0]) for row in cursor.fetchall() if row and row[0])
                    except Exception:
                        # Postgres aborts the transaction on error; rollback so ont_runs can run.
                        try:
                            connection.rollback()
                        except Exception:
                            pass
                    cursor.execute(
                        "select run_id from public.ont_runs where run_id is not null and run_id <> ''"
                    )
                    found.update(str(row[0]) for row in cursor.fetchall() if row and row[0])
            return found
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
    from tc_net_cert.storage.runs import validation_number as build_validation_number

    run_id = str(report.get("run_id") or "")
    site = str(report.get("station") or station)
    validation_id = str(report.get("validation_number") or "").strip() or build_validation_number(site, run_id)
    return {
        "validation_number": validation_id,
        "run_id": run_id,
        "station": str(report.get("station") or station),
        "operator": str(report.get("operator") or operator),
        "verdict": str(report.get("global_verdict") or ""),
        "global_verdict": str(report.get("global_verdict") or ""),
        "country_code": str(report.get("country_code") or "GT"),
        "bench_scope": str(report.get("bench_scope") or ""),
        "profile": str(report.get("profile") or ""),
        "speed_profile": str(report.get("speed_profile") or ""),
        "dut_profile": str(report.get("dut_profile") or ""),
        "brand": str(dut.get("brand") or ""),
        "model": str(dut.get("model") or ""),
        "device_type": str(dut.get("device_type") or dut.get("type") or ""),
        "serial": str(dut.get("serial") or ""),
        "mac": str(dut.get("mac") or ""),
        "software_version": str(dut.get("firmware") or ""),
        "hardware_version": str(dut.get("hardware") or ""),
        "started_at": report.get("started_at_utc") or "",
        "finished_at": report.get("finished_at_utc") or "",
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
        "report_json": report,
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
    from tc_net_cert.storage.runs import run_synced_in_cloud

    for folder in reversed(list_runs(runs_root)):
        report_path = folder / "report.json"
        if not report_path.is_file():
            continue
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if run_synced_in_cloud(folder.name, report, stored, station=station):
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


def _ts_or_none(value: object) -> str | None:
    text = str(value or "").strip()
    return text if text else None


def _missing_relation(exc: BaseException) -> bool:
    message = str(exc).lower()
    if "lab_certification_runs" in message and ("does not exist" in message or "no existe" in message):
        return True
    code = getattr(exc, "sqlstate", None) or getattr(exc, "pgcode", None)
    return code in ("42P01", "3F000")


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
