"""Consulta de releases de software (wheel) publicados en Supabase."""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

from tc_net_cert.version import __version__

OFFER_CACHE = Path("/var/lib/tc-net-cert/update_offer.json")
_DEFAULT_CHANNEL = "lab"
# HQ edita release/fleet-manifest.json en GitHub; las estaciones leen esta URL (override en supabase.env).
DEFAULT_FLEET_MANIFEST_URL = (
    "https://raw.githubusercontent.com/UrbanoTCW24/tc-net-cert/main/release/fleet-manifest.json"
)


def _parse_env(path: Path) -> dict[str, str]:
    fields: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return fields
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        fields[key.strip()] = value.strip()
    return fields


def _version_tuple(version: str) -> tuple[int, ...]:
    token = (version or "0").strip().split("-")[0]
    parts = [int(piece) for piece in re.findall(r"\d+", token)]
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


def version_newer(remote: str, current: str) -> bool:
    return _version_tuple(remote) > _version_tuple(current)


def read_cached_offer() -> dict:
    if not OFFER_CACHE.is_file():
        return {}
    try:
        loaded = json.loads(OFFER_CACHE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def write_cached_offer(offer: dict) -> None:
    OFFER_CACHE.parent.mkdir(parents=True, exist_ok=True)
    OFFER_CACHE.write_text(json.dumps(offer, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        OFFER_CACHE.chmod(0o644)
    except OSError:
        pass


def _manifest_to_offer(loaded: dict) -> dict:
    if not isinstance(loaded, dict):
        return {}
    remote_ver = str(loaded.get("version") or "").strip()
    url = str(loaded.get("url") or loaded.get("whl_url") or "").strip()
    if not remote_ver or not url:
        return {}
    return {
        "version": remote_ver,
        "url": url,
        "sha256": str(loaded.get("sha256") or "").strip(),
        "mandatory": bool(loaded.get("mandatory")),
        "channel": str(loaded.get("channel") or _DEFAULT_CHANNEL),
    }


def _fetch_manifest_url(fields: dict[str, str]) -> dict:
    candidates: list[str] = []
    custom = (fields.get("release_manifest_url") or fields.get("update_manifest_url") or "").strip()
    if custom.startswith("https://"):
        candidates.append(custom)
    if DEFAULT_FLEET_MANIFEST_URL not in candidates:
        candidates.append(DEFAULT_FLEET_MANIFEST_URL)
    try:
        import requests
    except ImportError:
        return {}
    stamp = int(time.time())
    for base in candidates:
        for url in (base, f"{base}?t={stamp}"):
            try:
                response = requests.get(url, timeout=12)
                if response.status_code != 200:
                    continue
                offer = _manifest_to_offer(response.json())
                if offer:
                    return offer
            except Exception:
                continue
    return {}


def _fetch_from_database(env_path: Path, channel: str) -> dict:
    from tc_net_cert.storage.supabase_runs import _settings, _settings_error

    if _settings_error(env_path):
        return {}
    settings = _settings(env_path)
    if not settings:
        return {}
    try:
        import psycopg
    except ImportError:
        return {}
    statement = """
        select version, whl_url, sha256, mandatory, channel
        from public.lab_releases
        where channel = %(channel)s and is_active = true
        order by updated_at desc
        limit 1
    """
    for target in settings:
        try:
            with psycopg.connect(**target, connect_timeout=8) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(statement, {"channel": channel})
                    row = cursor.fetchone()
                    if not row:
                        return {}
                    version, whl_url, sha256, mandatory, channel_name = row
            url = str(whl_url or "").strip()
            if not url:
                return {}
            return {
                "version": str(version or ""),
                "url": url,
                "sha256": str(sha256 or ""),
                "mandatory": bool(mandatory),
                "channel": str(channel_name or channel),
                "source": "supabase",
            }
        except Exception:
            continue
    return {}


def sync_release_offer(
    *,
    env_path: Path,
    channel: str = _DEFAULT_CHANNEL,
    current_version: str = "",
) -> dict:
    """Pull remoto y actualiza caché local si hay URL válida."""
    current = current_version or __version__
    fields = _parse_env(env_path)
    offer = _fetch_from_database(env_path, channel)
    if not offer:
        offer = _fetch_manifest_url(fields)
        if offer:
            offer.setdefault("channel", channel)
            offer["source"] = "manifest"
    prior = read_cached_offer()
    if not offer and prior:
        offer = dict(prior)
        offer["source"] = "cache"
    elif not offer:
        return {
            "ok": False,
            "error": "No hay release publicado (tabla lab_releases o release_manifest_url).",
            "app_version": current,
            "update_pending": False,
        }
    url = str(offer.get("url") or "").strip()
    remote_ver = str(offer.get("version") or "").strip()
    pending = bool(url and remote_ver and version_newer(remote_ver, current))
    if url:
        write_cached_offer(offer)
    return {
        "ok": True,
        "app_version": current,
        "update_pending": pending,
        "update_version": remote_ver if pending else "",
        "update_url": url if pending else "",
        "update_mandatory": bool(offer.get("mandatory")) if pending else False,
        "update_channel": str(offer.get("channel") or channel),
        "source": offer.get("source") or "unknown",
        "error": "",
    }


def effective_update_status(*, current_version: str = "") -> dict:
    current = current_version or __version__
    offer = read_cached_offer()
    url = str(offer.get("url") or "").strip()
    remote_ver = str(offer.get("version") or "").strip()
    pending = bool(url and remote_ver and version_newer(remote_ver, current))
    return {
        "update_pending": pending,
        "update_version": remote_ver if pending else "",
        "update_mandatory": bool(offer.get("mandatory")) if pending else False,
        "update_channel": str(offer.get("channel") or _DEFAULT_CHANNEL),
        "offer_source": offer.get("source") or ("cache" if offer else "none"),
    }


def resolve_update_url() -> str:
    offer = read_cached_offer()
    return str(offer.get("url") or "").strip()


_release_lock = threading.Lock()
_release_started = False


def check_release_background(env_path: Path) -> None:
    global _release_started

    def _run() -> None:
        try:
            sync_release_offer(env_path=env_path)
        except Exception:
            pass

    with _release_lock:
        if _release_started:
            return
        _release_started = True
    threading.Thread(target=_run, name="tc-release-sync", daemon=True).start()
