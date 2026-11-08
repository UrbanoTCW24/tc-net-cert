"""Register each lab PC online (Supabase) and assign station_code LAB-NNN."""

from __future__ import annotations

import hashlib
import json
import os
import socket
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import getnode

from tc_net_cert.config.loader import default_config_path, load_config, save_config
from tc_net_cert.config.remote_sync import resolve_country_code
from tc_net_cert.version import __version__

IDENTITY_PATH = Path("/var/lib/tc-net-cert/station_identity.json")
_STATION_PLACEHOLDERS = frozenset({"", "LAB-XX", "LAB-UNKNOWN"})
_BOOT_LOCK = threading.Lock()
_BOOT_STARTED = False

_REGISTER_SQL = """
    select public.register_lab_station(
        %(fingerprint)s, %(hostname)s, %(country)s, %(app_version)s, %(layout)s, %(requested)s
    )
"""

_FALLBACK_LOCK_ID = 89001702


def station_placeholder(station: str) -> bool:
    text = (station or "").strip().upper()
    return text in _STATION_PLACEHOLDERS


def install_fingerprint() -> str:
    for candidate in (Path("/etc/machine-id"), Path("/var/lib/dbus/machine-id")):
        try:
            if candidate.is_file():
                token = candidate.read_text(encoding="utf-8").strip()
                if token and token.lower() != "uninitialized":
                    return hashlib.sha256(f"machine-id:{token}".encode()).hexdigest()
        except OSError:
            continue
    host = socket.gethostname()
    blob = f"fallback:{host}:{getnode()}"
    return hashlib.sha256(blob.encode()).hexdigest()


def read_identity_cache() -> dict:
    if not IDENTITY_PATH.is_file():
        return {}
    try:
        loaded = json.loads(IDENTITY_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def write_identity_cache(payload: dict) -> None:
    IDENTITY_PATH.parent.mkdir(parents=True, exist_ok=True)
    IDENTITY_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        IDENTITY_PATH.chmod(0o644)
    except OSError:
        pass


def bench_layout_label(port_count: int) -> str:
    if port_count >= 8:
        return "8lan"
    return "4lan"


def _connect(env_path: Path):
    from tc_net_cert.storage.supabase_runs import _settings, _settings_error

    if _settings_error(env_path):
        return None, _settings_error(env_path)
    settings = _settings(env_path)
    if not settings:
        return None, "Sin credenciales Supabase."
    try:
        import psycopg
    except ImportError:
        return None, "Falta cliente psycopg."
    last = "No se pudo conectar a Supabase."
    for target in settings:
        try:
            connection = psycopg.connect(**target, connect_timeout=10)
            return connection, ""
        except Exception as exc:
            last = str(exc)[:120]
    return None, last


def _register_rpc(
    env_path: Path,
    *,
    fingerprint: str,
    hostname: str,
    country: str,
    app_version: str,
    layout: str,
    requested: str,
) -> tuple[str, str]:
    connection, error = _connect(env_path)
    if connection is None:
        return "", error
    payload = {
        "fingerprint": fingerprint,
        "hostname": hostname[:120],
        "country": country,
        "app_version": app_version,
        "layout": layout,
        "requested": requested or None,
    }
    try:
        with connection:
            with connection.cursor() as cursor:
                try:
                    cursor.execute(_REGISTER_SQL, payload)
                    row = cursor.fetchone()
                    if row and row[0]:
                        return str(row[0]), ""
                except Exception as exc:
                    sqlstate = str(getattr(exc, "sqlstate", "") or "")
                    missing = sqlstate == "42883" or "register_lab_station" in str(exc).lower()
                    missing_table = sqlstate == "42P01" or "lab_stations" in str(exc).lower()
                    if missing or missing_table:
                        return _register_fallback(cursor, connection, payload)
                    return "", str(exc)[:160]
        return "", "Registro vacío."
    except Exception as exc:
        return "", str(exc)[:160]
    finally:
        try:
            connection.close()
        except Exception:
            pass


def _register_fallback(cursor, connection, payload: dict) -> tuple[str, str]:
    fingerprint = str(payload["fingerprint"])
    cursor.execute(
        "select station_code from public.lab_stations where install_fingerprint = %(fp)s",
        {"fp": fingerprint},
    )
    row = cursor.fetchone()
    if row and row[0]:
        code = str(row[0])
        cursor.execute(
            """
            update public.lab_stations
            set last_seen_at = now(),
                hostname = coalesce(nullif(%(host)s, ''), hostname),
                app_version = coalesce(nullif(%(ver)s, ''), app_version)
            where station_code = %(code)s
            """,
            {
                "host": payload.get("hostname") or "",
                "ver": payload.get("app_version") or "",
                "code": code,
            },
        )
        connection.commit()
        return code, ""

    requested = str(payload.get("requested") or "").strip().upper()
    if requested and requested not in _STATION_PLACEHOLDERS:
        cursor.execute(
            "select station_code from public.lab_stations where station_code = %(code)s",
            {"code": requested},
        )
        if cursor.fetchone() is None:
            cursor.execute(
                """
                insert into public.lab_stations (
                    station_code, install_fingerprint, hostname, country_code, app_version, bench_layout
                ) values (%(code)s, %(fp)s, %(host)s, %(country)s, %(ver)s, %(layout)s)
                """,
                {
                    "code": requested,
                    "fp": fingerprint,
                    "host": payload.get("hostname") or "",
                    "country": payload.get("country") or "GT",
                    "ver": payload.get("app_version") or "",
                    "layout": payload.get("layout") or "",
                },
            )
            connection.commit()
            return requested, ""

    cursor.execute("select pg_advisory_xact_lock(%(id)s)", {"id": _FALLBACK_LOCK_ID})
    cursor.execute(
        """
        select coalesce(max((regexp_match(station_code, '^LAB-(\\d+)$'))[1]::int), 0) + 1
        from public.lab_stations
        """
    )
    row = cursor.fetchone()
    next_n = int(row[0] if row and row[0] else 1)
    code = f"LAB-{next_n:03d}"
    cursor.execute(
        """
        insert into public.lab_stations (
            station_code, install_fingerprint, hostname, country_code, app_version, bench_layout
        ) values (%(code)s, %(fp)s, %(host)s, %(country)s, %(ver)s, %(layout)s)
        """,
        {
            "code": code,
            "fp": fingerprint,
            "host": payload.get("hostname") or "",
            "country": payload.get("country") or "GT",
            "ver": payload.get("app_version") or "",
            "layout": payload.get("layout") or "",
        },
    )
    connection.commit()
    return code, ""


def provision_station(
    *,
    config_path: Path | None = None,
    env_path: Path | None = None,
    force_config_write: bool = False,
) -> dict:
    """Authenticate against Supabase and ensure this PC has station_code LAB-NNN."""
    from tc_net_cert.storage.supabase_runs import config_path as default_supabase_path

    path = config_path or default_config_path()
    env_file = env_path or default_supabase_path()
    if not env_file.is_file():
        return {"ok": False, "error": "supabase.env no configurado.", "station_code": ""}

    try:
        config, cfg_path = load_config(path)
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:120], "station_code": ""}

    fingerprint = install_fingerprint()
    hostname = socket.gethostname()
    country = resolve_country_code(config.station)
    layout = bench_layout_label(len(config.ports))
    requested = "" if station_placeholder(config.station) else config.station.strip().upper()

    code, error = _register_rpc(
        env_file,
        fingerprint=fingerprint,
        hostname=hostname,
        country=country,
        app_version=__version__,
        layout=layout,
        requested=requested,
    )
    if not code:
        cached = read_identity_cache()
        if cached.get("station_code") and not station_placeholder(str(cached.get("station_code"))):
            code = str(cached["station_code"])
            error = error or "Sin red; se usa caché local."
        else:
            return {"ok": False, "error": error or "No se pudo registrar la estación.", "station_code": ""}

    config_updated = False
    if station_placeholder(config.station) or force_config_write:
        if config.station != code:
            config.station = code
            try:
                save_config(config, cfg_path)
                config_updated = True
            except OSError as exc:
                return {
                    "ok": True,
                    "station_code": code,
                    "config_updated": False,
                    "error": f"ID asignado {code} pero no se pudo escribir config: {exc}"[:120],
                }

    identity = {
        "station_code": code,
        "install_fingerprint": fingerprint,
        "hostname": hostname,
        "country_code": country,
        "registered_at": read_identity_cache().get("registered_at")
        or datetime.now(timezone.utc).isoformat(),
        "last_registered_at": datetime.now(timezone.utc).isoformat(),
        "app_version": __version__,
        "source": "supabase",
    }
    write_identity_cache(identity)

    return {
        "ok": True,
        "station_code": code,
        "config_updated": config_updated,
        "fingerprint": fingerprint[:12] + "…",
        "error": error,
    }


def station_registry_status() -> dict:
    identity = read_identity_cache()
    code = str(identity.get("station_code") or "")
    return {
        "station_code": code,
        "registered_at": identity.get("last_registered_at") or identity.get("registered_at") or "",
        "hostname": identity.get("hostname") or "",
        "fingerprint": str(identity.get("install_fingerprint") or "")[:12],
    }


def cloud_bootstrap_background(config_path: Path, env_path: Path) -> None:
    """On panel boot: register station, sync profiles, then check OTA release."""
    global _BOOT_STARTED

    def _run() -> None:
        time.sleep(0.5)
        try:
            provision_station(config_path=config_path, env_path=env_path)
        except Exception:
            pass
        try:
            config, _ = load_config(config_path)
            from tc_net_cert.config.remote_sync import (
                get_or_register_tool_identity,
                resolve_country_code,
                sync_profiles,
            )
            from tc_net_cert.config.release_sync import sync_release_offer
            from tc_net_cert.version import __version__

            get_or_register_tool_identity(
                station_id=config.station,
                country_code=resolve_country_code(config.station),
                installed_version=__version__,
                mac_host=config.management_mac,
                env_path=env_path,
            )
            sync_profiles(station=config.station, env_path=env_path)
            sync_release_offer(env_path=env_path)
        except Exception:
            pass

    with _BOOT_LOCK:
        if _BOOT_STARTED:
            return
        _BOOT_STARTED = True
    threading.Thread(target=_run, name="tc-cloud-bootstrap", daemon=True).start()
