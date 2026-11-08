"""Per-bench last portal reading (serial, MAC, model) for dual-station labs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _path() -> Path:
    return Path.home() / ".local" / "state" / "tc-net-cert" / "bench-identities.json"


def load_all() -> dict[str, dict[str, Any]]:
    path = _path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for key, value in payload.items():
        if isinstance(value, dict):
            result[str(key).upper()] = value
    return result


def load_bench(bench_id: str) -> dict[str, Any]:
    return dict(load_all().get(bench_id.upper(), {}))


def bench_wifi_reading_path(bench_id: str) -> Path:
    return Path.home() / ".local" / "state" / "tc-net-cert" / f"wifi-reading-bench-{bench_id.upper()}.json"


def bench_inventory_path(bench_id: str) -> Path:
    return Path.home() / ".local" / "state" / "tc-net-cert" / f"bench-inventory-{bench_id.upper()}.json"


def load_bench_inventory(bench_id: str) -> dict[str, Any]:
    path = bench_inventory_path(bench_id)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def load_all_inventories() -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for bench_id in ("A", "B"):
        loaded = load_bench_inventory(bench_id)
        if loaded:
            result[bench_id] = loaded
    return result


def clear_bench_reading(bench_id: str) -> None:
    """Drop UI inventory and bench identity after certify (or before a new run)."""
    bid = str(bench_id or "").strip().upper()
    if bid not in {"A", "B"}:
        return
    inv = bench_inventory_path(bid)
    try:
        inv.unlink(missing_ok=True)
    except OSError:
        pass
    wifi = bench_wifi_reading_path(bid)
    try:
        wifi.unlink(missing_ok=True)
    except OSError:
        pass
    path = _path()
    try:
        all_data = load_all()
    except Exception:
        all_data = {}
    if bid in all_data:
        del all_data[bid]
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if all_data:
                path.write_text(json.dumps(all_data, ensure_ascii=False, indent=2), encoding="utf-8")
                path.chmod(0o600)
            else:
                path.unlink(missing_ok=True)
        except OSError:
            pass


def clear_all_bench_readings() -> None:
    for bench_id in ("A", "B"):
        clear_bench_reading(bench_id)


_INVENTORY_KEYS = (
    "serial",
    "mac",
    "brand",
    "model",
    "gateway",
    "iface",
    "family",
    "hardware",
    "software",
    "device_type",
    "ont_state",
    "tx_power",
    "rx_power",
    "voltage",
    "bias",
    "temperature",
    "ssid_24",
    "ssid_50",
    "wifi_password_24",
    "wifi_password_50",
    "run_id_hint",
    "run_reused",
    "warning",
    "bench_id",
)


def store_bench_inventory(bench_id: str, found: dict) -> None:
    serial = str(found.get("serial") or "").strip()
    if not serial:
        return
    path = bench_inventory_path(bench_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    row: dict[str, Any] = {"bench_id": bench_id.upper(), "ok": bool(found.get("ok"))}
    for key in _INVENTORY_KEYS:
        if key in found and found[key] not in {None, ""}:
            value = found[key]
            if isinstance(value, (str, int, float, bool)):
                row[key] = value
            elif isinstance(value, list):
                row[key] = value[:20]
    path.write_text(json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8")
    path.chmod(0o600)


def store_bench(bench_id: str, found: dict) -> None:
    serial = str(found.get("serial") or "").strip()
    if not serial or len(serial) > 40 or " " in serial:
        return
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    all_data = load_all()
    entry = {
        "serial": serial,
        "mac": str(found.get("mac") or "").strip()[:17],
        "brand": str(found.get("brand") or "").strip()[:40],
        "model": str(found.get("model") or "").strip()[:40],
        "gateway": str(found.get("gateway") or "").strip()[:15],
        "run_id_hint": str(found.get("run_id_hint") or "").strip()[:16],
        "run_reused": bool(found.get("run_reused")),
        "family": str(found.get("family") or "").strip()[:8],
        "ssid_24": str(found.get("ssid_24") or "").strip()[:32],
        "ssid_50": str(found.get("ssid_50") or "").strip()[:32],
        "wifi_ready": bool(
            found.get("ssid_24")
            and found.get("ssid_50")
            and found.get("wifi_password_24")
            and found.get("wifi_password_50")
        ),
    }
    all_data[bench_id.upper()] = entry
    path.write_text(json.dumps(all_data, ensure_ascii=False, indent=2), encoding="utf-8")
    path.chmod(0o600)
    try:
        from tc_net_cert.wifi.throughput import store_reading

        store_reading(found, bench_wifi_reading_path(bench_id))
    except Exception:
        pass
    try:
        store_bench_inventory(bench_id, found)
    except Exception:
        pass
