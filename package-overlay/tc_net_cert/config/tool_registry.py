"""TOOL-NNN registry handshake with Supabase (offline cache)."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from tc_net_cert.version import __version__

TOOL_IDENTITY_PATH = Path("/var/lib/tc-net-cert/tool_identity.json")
_MAC_RE = re.compile(r"^([0-9a-f]{2}(:[0-9a-f]{2}){5})$")

_REGISTER_SQL = """
    select public.register_or_update_tool(
        %(station_id)s, %(mac_host)s, %(installed_version)s, %(country)s
    )
"""


def normalize_mac_host(value: str) -> str:
    text = (value or "").strip().lower().replace("-", ":")
    if _MAC_RE.match(text):
        return text
    return ""


def read_tool_identity_cache() -> dict:
    if not TOOL_IDENTITY_PATH.is_file():
        return {}
    try:
        loaded = json.loads(TOOL_IDENTITY_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def write_tool_identity_cache(payload: dict) -> None:
    TOOL_IDENTITY_PATH.parent.mkdir(parents=True, exist_ok=True)
    TOOL_IDENTITY_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        TOOL_IDENTITY_PATH.chmod(0o644)
    except OSError:
        pass


def tool_fields_for_run() -> dict[str, str]:
    cache = read_tool_identity_cache()
    version = str(cache.get("installed_version") or __version__)
    return {
        "tool_number": str(cache.get("tool_number") or ""),
        "tool_version": version,
    }


def tool_identity_status() -> dict:
    cache = read_tool_identity_cache()
    number = str(cache.get("tool_number") or "")
    version = str(cache.get("installed_version") or __version__)
    label = f"{number} ({version})" if number else ""
    return {
        "tool_number": number,
        "tool_version": version,
        "tool_label": label,
        "station_id": str(cache.get("station_id") or ""),
        "mac_host": str(cache.get("mac_host") or ""),
        "country_code": str(cache.get("country_code") or ""),
        "last_seen": str(cache.get("last_seen") or cache.get("last_registered_at") or ""),
        "source": str(cache.get("source") or ("cache" if number else "none")),
    }


def tool_trace_label() -> str:
    meta = tool_identity_status()
    if meta["tool_number"]:
        return f"{meta['tool_number']} ({meta['tool_version']})"
    return meta["tool_version"] or __version__


def _connect(env_path: Path):
    from tc_net_cert.storage.supabase_runs import _settings, _settings_error

    if _settings_error(env_path):
        return None, _settings_error(env_path)
    settings = _settings(env_path)
    if not settings:
        return None, "Sin credenciales Supabase en este equipo."
    try:
        import psycopg
    except ImportError:
        return None, "Falta cliente psycopg."
    last = "No se pudo conectar a Supabase."
    for target in settings:
        try:
            return psycopg.connect(**target, connect_timeout=10), ""
        except Exception as exc:
            last = str(exc)[:120]
    return None, last


def _register_remote(
    env_path: Path,
    *,
    station_id: str,
    mac_host: str,
    installed_version: str,
    country: str,
) -> tuple[str, str]:
    connection, error = _connect(env_path)
    if connection is None:
        return "", error
    payload = {
        "station_id": station_id[:64],
        "mac_host": mac_host,
        "installed_version": installed_version,
        "country": country,
    }
    try:
        with connection.cursor() as cursor:
            cursor.execute(_REGISTER_SQL, payload)
            row = cursor.fetchone()
        connection.commit()
        if row and row[0]:
            return str(row[0]), ""
    except Exception as exc:
        try:
            connection.rollback()
        except Exception:
            pass
        sqlstate = str(getattr(exc, "sqlstate", "") or "")
        lowered = str(exc).lower()
        use_fallback = (
            sqlstate in ("42883", "42P01")
            or "register_or_update_tool" in lowered
            or "lab_tools" in lowered
            or "current transaction is aborted" in lowered
        )
        if use_fallback:
            try:
                with connection.cursor() as cursor:
                    code, fb_err = _register_fallback(cursor, connection, payload)
                if code:
                    return code, fb_err
            except Exception as fb_exc:
                try:
                    connection.rollback()
                except Exception:
                    pass
                if "42P01" in str(getattr(fb_exc, "sqlstate", "")) or "lab_tools" in str(fb_exc).lower():
                    return "", "Ejecute docs/supabase/lab_tools.sql en el SQL Editor de Supabase."
                return "", str(fb_exc)[:160]
        if "42P01" in sqlstate or "lab_tools" in lowered:
            return "", "Ejecute docs/supabase/lab_tools.sql en el SQL Editor de Supabase."
        return "", str(exc)[:160]
    finally:
        try:
            connection.close()
        except Exception:
            pass
    return "", "Registro vacío."


def _register_fallback(cursor, connection, payload: dict) -> tuple[str, str]:
    mac = payload["mac_host"]
    cursor.execute("select tool_number from public.lab_tools where mac_host = %(m)s", {"m": mac})
    row = cursor.fetchone()
    if row and row[0]:
        code = str(row[0])
        cursor.execute(
            """
            update public.lab_tools
            set last_seen = now(),
                station_id = coalesce(nullif(%(st)s, ''), station_id),
                installed_version = coalesce(nullif(%(ver)s, ''), installed_version)
            where tool_number = %(code)s
            """,
            {"st": payload["station_id"], "ver": payload["installed_version"], "code": code},
        )
        connection.commit()
        return code, ""

    cursor.execute("select pg_advisory_xact_lock(89001703)")
    cursor.execute(
        """
        select coalesce(max((regexp_match(tool_number, '^TOOL-(\\d+)$'))[1]::int), 0) + 1
        from public.lab_tools
        """
    )
    row = cursor.fetchone()
    next_n = int(row[0] if row and row[0] else 1)
    code = f"TOOL-{next_n:03d}"
    cursor.execute(
        """
        insert into public.lab_tools (tool_number, station_id, mac_host, installed_version, country_code)
        values (%(code)s, %(st)s, %(mac)s, %(ver)s, %(cc)s)
        """,
        {
            "code": code,
            "st": payload["station_id"],
            "mac": mac,
            "ver": payload["installed_version"],
            "cc": payload.get("country") or "GT",
        },
    )
    connection.commit()
    return code, ""


def get_or_register_tool_identity(
    *,
    station_id: str = "",
    country_code: str = "GT",
    installed_version: str = "",
    mac_host: str = "",
    env_path: Path | None = None,
) -> dict:
    """Handshake con Supabase; conserva caché local si no hay red."""
    from tc_net_cert.storage.supabase_runs import config_path as default_env

    env_file = env_path or default_env()
    version = (installed_version or __version__).strip()
    mac = normalize_mac_host(mac_host)
    station = (station_id or "").strip()
    country = (country_code or "GT").upper()[:2]

    if not mac:
        cache = read_tool_identity_cache()
        if cache.get("tool_number"):
            return {"ok": True, **tool_identity_status(), "error": "Sin MAC de gestión; se usa caché."}
        return {"ok": False, "error": "Falta mac_host (management_mac en config).", **tool_identity_status()}

    if not env_file.is_file():
        cache = read_tool_identity_cache()
        if cache.get("tool_number"):
            return {"ok": True, **tool_identity_status(), "error": "supabase.env no configurado; caché local."}
        return {"ok": False, "error": "supabase.env no configurado.", **tool_identity_status()}

    tool_number, error = _register_remote(
        env_file,
        station_id=station,
        mac_host=mac,
        installed_version=version,
        country=country,
    )
    if not tool_number:
        cache = read_tool_identity_cache()
        if cache.get("tool_number"):
            return {"ok": True, **tool_identity_status(), "error": error or "Sin red; caché local."}
        return {"ok": False, "error": error or "No se registró la herramienta.", **tool_identity_status()}

    now = datetime.now(timezone.utc).isoformat()
    identity = {
        "tool_number": tool_number,
        "station_id": station,
        "mac_host": mac,
        "installed_version": version,
        "country_code": country,
        "registered_at": cache_registered_at(),
        "last_registered_at": now,
        "last_seen": now,
        "source": "supabase",
    }
    write_tool_identity_cache(identity)
    return {"ok": True, **tool_identity_status(), "error": error}


def cache_registered_at() -> str:
    prior = read_tool_identity_cache().get("registered_at")
    return str(prior) if prior else datetime.now(timezone.utc).isoformat()


def _bootstrap_sql_path() -> Path:
    return Path(__file__).resolve().parent.parent / "sql" / "lab_tools_bootstrap.sql"


def _lab_tools_exists(env_path: Path) -> bool:
    connection, _ = _connect(env_path)
    if connection is None:
        return False
    try:
        with connection.cursor() as cursor:
            cursor.execute("select to_regclass('public.lab_tools')")
            row = cursor.fetchone()
        return bool(row and row[0])
    except Exception:
        return False
    finally:
        try:
            connection.close()
        except Exception:
            pass


def apply_lab_tools_schema(env_path: Path) -> dict:
    """Create lab_tools via direct Postgres (si el rol lo permite)."""
    from tc_net_cert.storage.supabase_runs import _settings, _settings_error

    if _settings_error(env_path):
        return {"ok": False, "error": _settings_error(env_path)}
    if _lab_tools_exists(env_path):
        return {"ok": True, "error": "", "already_present": True}
    sql_path = _bootstrap_sql_path()
    if not sql_path.is_file():
        return {"ok": False, "error": "Falta sql/lab_tools_bootstrap.sql en el paquete."}
    text = sql_path.read_text(encoding="utf-8")
    grant_at = text.find("\ngrant ")
    body = text[:grant_at].strip() if grant_at > 0 else text.strip()
    grants = text[grant_at + 1 :].strip() if grant_at > 0 else ""
    try:
        import psycopg
    except ImportError:
        return {"ok": False, "error": "Falta cliente psycopg."}
    targets = list(reversed(_settings(env_path)))
    last_error = "No se pudo aplicar DDL (use Supabase SQL Editor con lab_tools_paso1_minimo.sql)."
    for target in targets:
        try:
            with psycopg.connect(**target, connect_timeout=25, autocommit=True) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(body)
                    for stmt in grants.split(";"):
                        part = stmt.strip()
                        if part:
                            cursor.execute(part)
            if _lab_tools_exists(env_path):
                return {"ok": True, "error": "", "already_present": False}
        except Exception as exc:
            last_error = str(exc)[:220]
    return {"ok": False, "error": last_error}
