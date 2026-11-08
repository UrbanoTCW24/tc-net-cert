"""Pull DUT acceptance rules from Supabase and cache them for offline labs."""

from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

CACHE_PATH = Path("/var/lib/tc-net-cert/profiles_cache.json")
_COUNTRY_RE = re.compile(r"(?:^|[-_])(GT|DO|SV|CR|HN|NI|PA|MX|US)(?:$|[-_])", re.IGNORECASE)

_memory: dict[str, object] | None = None
_memory_mtime: float = 0.0
_startup_lock = threading.Lock()
_startup_done = False


def resolve_country_code(station: str = "") -> str:
    override = os.environ.get("TC_NET_CERT_COUNTRY", "").strip().upper()
    if len(override) == 2:
        return override
    match = _COUNTRY_RE.search(station or "")
    if match:
        return match.group(1).upper()
    return "GT"


def cache_path() -> Path:
    override = os.environ.get("TC_NET_CERT_PROFILES_CACHE", "").strip()
    if override:
        return Path(override)
    return CACHE_PATH


def _read_disk() -> dict:
    path = cache_path()
    if not path.is_file():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def cached_snapshot() -> dict:
    """Last known sync payload (profiles list + metadata)."""
    global _memory, _memory_mtime
    path = cache_path()
    try:
        mtime = path.stat().st_mtime if path.is_file() else 0.0
    except OSError:
        mtime = 0.0
    if _memory is not None and mtime == _memory_mtime:
        return dict(_memory)
    payload = _read_disk()
    _memory = payload
    _memory_mtime = mtime
    return dict(payload)


def _write_cache(payload: dict) -> None:
    global _memory, _memory_mtime
    path = cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    path.write_text(text + "\n", encoding="utf-8")
    try:
        path.chmod(0o644)
    except OSError:
        pass
    try:
        _memory_mtime = path.stat().st_mtime
    except OSError:
        _memory_mtime = time.time()
    _memory = payload


def _row_to_profile(row: dict) -> dict:
    allowed = row.get("allowed_gpon_states") or []
    if isinstance(allowed, str):
        allowed = [part.strip() for part in allowed.split(",") if part.strip()]
    if not isinstance(allowed, list):
        allowed = []
    clean_allowed = [str(item).strip() for item in allowed if str(item).strip()]
    gateway = str(row.get("default_gateway") or "192.168.1.1").strip()
    return {
        "model_id": str(row.get("model_id") or "").strip(),
        "country_code": str(row.get("country_code") or "").strip().upper(),
        "min_throughput_mbps": float(row.get("min_throughput_mbps") or 900.0),
        "allowed_gpon_states": clean_allowed,
        "default_gateway": gateway,
        "optical_rx_low": float(row.get("optical_rx_low") if row.get("optical_rx_low") is not None else -27.0),
        "optical_rx_high": float(row.get("optical_rx_high") if row.get("optical_rx_high") is not None else -8.0),
    }


def _fetch_rows(env_path: Path, country_code: str) -> tuple[list[dict], str]:
    from tc_net_cert.storage.supabase_runs import _settings, _settings_error

    hint = _settings_error(env_path)
    if hint:
        return [], hint
    settings = _settings(env_path)
    if not settings:
        return [], "Sin credenciales de base de datos en este equipo."
    try:
        import psycopg
    except ImportError:
        return [], "Falta el cliente psycopg en el entorno virtual."
    statement = """
        select
            model_id,
            country_code,
            min_throughput_mbps,
            allowed_gpon_states,
            default_gateway::text,
            optical_rx_low,
            optical_rx_high
        from public.dut_profiles
        where country_code = %(country)s or country_code = '*'
        order by model_id, country_code
    """
    last_error = "No se pudo leer dut_profiles."
    for target in settings:
        try:
            with psycopg.connect(**target, connect_timeout=8) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(statement, {"country": country_code})
                    columns = [col.name for col in cursor.description]
                    rows: list[dict] = []
                    for record in cursor.fetchall():
                        rows.append({columns[i]: record[i] for i in range(len(columns))})
            return [_row_to_profile(item) for item in rows if item.get("model_id")], ""
        except Exception:
            last_error = "No se pudo leer dut_profiles (revisa la tabla en Supabase)."
    return [], last_error


def sync_profiles(*, country_code: str = "", station: str = "", env_path: Path | None = None) -> dict:
    """Pull remote rules and refresh the local cache. Safe while a run is active."""
    from tc_net_cert.storage.supabase_runs import config_path

    env_file = env_path or config_path()
    country = (country_code or resolve_country_code(station)).upper()
    profiles, error = _fetch_rows(env_file, country)
    prior = cached_snapshot()
    used_cache = False
    if not profiles:
        if prior.get("profiles"):
            profiles = prior.get("profiles") if isinstance(prior.get("profiles"), list) else []
            used_cache = True
            if error:
                error = f"{error} Se conservó la caché local ({len(profiles)} perfiles)."
        elif error:
            return {"ok": False, "error": error, "country_code": country, "profiles": [], "source": "none"}
    payload = {
        "version": 1,
        "country_code": country,
        "synced_at": datetime.now(timezone.utc).isoformat(),
        "profiles": profiles,
        "source": "cache" if used_cache else "remote",
        "error": error if used_cache and error else "",
    }
    if profiles and not used_cache:
        _write_cache(payload)
    elif profiles and used_cache:
        payload = dict(prior)
        payload["error"] = error
    elif not profiles:
        return {"ok": False, "error": error or "No hay perfiles.", "country_code": country, "profiles": [], "source": "none"}
    return {
        "ok": True,
        "country_code": country,
        "profiles": profiles,
        "synced_at": payload.get("synced_at"),
        "source": payload.get("source"),
        "error": payload.get("error") or "",
        "count": len(profiles),
    }


def sync_profiles_background(station: str, env_path: Path | None = None) -> None:
    global _startup_done

    def _run() -> None:
        try:
            sync_profiles(station=station, env_path=env_path)
        except Exception:
            pass

    with _startup_lock:
        if _startup_done:
            return
        _startup_done = True
    thread = threading.Thread(target=_run, name="tc-profiles-sync", daemon=True)
    thread.start()


def profiles_for_country(country_code: str) -> list[dict]:
    country = country_code.upper()
    snapshot = cached_snapshot()
    rows = snapshot.get("profiles") if isinstance(snapshot.get("profiles"), list) else []
    by_model: dict[str, dict] = {}
    for row in rows:
        if not isinstance(row, dict) or not row.get("model_id"):
            continue
        cc = str(row.get("country_code") or "").upper()
        model = str(row["model_id"])
        if cc == "*":
            by_model.setdefault(model, row)
        elif cc == country:
            by_model[model] = row
    return list(by_model.values())


def lookup_profile(model: str, country_code: str) -> dict | None:
    if not model:
        return None
    token = model.strip()
    for row in profiles_for_country(country_code):
        mid = str(row.get("model_id") or "")
        if mid == token or mid == token.upper():
            return row
    return None


def apply_profile_overlay(merged: dict, *, country_code: str = "", station: str = "") -> dict:
    country = country_code or resolve_country_code(station)
    row = lookup_profile(str(merged.get("name") or merged.get("model") or ""), country)
    if not row:
        return merged
    out = dict(merged)
    out["throughput_mbps"] = float(row.get("min_throughput_mbps") or out.get("throughput_mbps") or 900.0)
    out["rx_low"] = float(row.get("optical_rx_low") if row.get("optical_rx_low") is not None else out.get("rx_low"))
    out["rx_high"] = float(row.get("optical_rx_high") if row.get("optical_rx_high") is not None else out.get("rx_high"))
    allowed = row.get("allowed_gpon_states")
    if isinstance(allowed, list) and allowed:
        out["allowed_gpon_states"] = [str(item) for item in allowed]
    gateway = str(row.get("default_gateway") or "").strip()
    if gateway:
        out["default_gateway"] = gateway
    out["remote_profile"] = row.get("model_id")
    return out


def merge_portals_with_remote(portals: dict, *, country_code: str = "", station: str = "") -> dict:
    country = country_code or resolve_country_code(station)
    out = {key: dict(value) if isinstance(value, dict) else value for key, value in portals.items()}
    for row in profiles_for_country(country):
        model = str(row.get("model_id") or "")
        if not model:
            continue
        host = str(row.get("default_gateway") or "192.168.1.1")
        current = out.get(model)
        if not isinstance(current, dict):
            out[model] = {"host": host, "username": "", "password": "", "brand": "Huawei"}
        elif not str(current.get("host") or "").strip():
            current["host"] = host
    return out


def get_or_register_tool_identity(
    *,
    station_id: str = "",
    country_code: str = "",
    installed_version: str = "",
    mac_host: str = "",
    env_path: Path | None = None,
) -> dict:
    """Registra TOOL-NNN en Supabase y persiste /var/lib/tc-net-cert/tool_identity.json."""
    from tc_net_cert.config.tool_registry import get_or_register_tool_identity as _register

    country = (country_code or resolve_country_code(station_id)).upper()
    return _register(
        station_id=station_id,
        country_code=country,
        installed_version=installed_version,
        mac_host=mac_host,
        env_path=env_path,
    )


def profile_sync_status(station: str = "") -> dict:
    snapshot = cached_snapshot()
    profiles = snapshot.get("profiles") if isinstance(snapshot.get("profiles"), list) else []
    return {
        "country_code": snapshot.get("country_code") or resolve_country_code(station),
        "synced_at": snapshot.get("synced_at") or "",
        "count": len(profiles),
        "source": snapshot.get("source") or ("cache" if profiles else "none"),
        "cache_path": str(cache_path()),
    }
