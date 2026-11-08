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


def store_bench(bench_id: str, found: dict) -> None:
    serial = str(found.get("serial") or "").strip()
    if not serial or len(serial) > 40 or " " in serial:
        return
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    all_data = load_all()
    all_data[bench_id.upper()] = {
        "serial": serial,
        "mac": str(found.get("mac") or "").strip()[:17],
        "brand": str(found.get("brand") or "").strip()[:40],
        "model": str(found.get("model") or "").strip()[:40],
        "gateway": str(found.get("gateway") or "").strip()[:15],
        "run_id_hint": str(found.get("run_id_hint") or "").strip()[:16],
        "run_reused": bool(found.get("run_reused")),
        "family": str(found.get("family") or "").strip()[:8],
    }
    path.write_text(json.dumps(all_data, ensure_ascii=False, indent=2), encoding="utf-8")
    path.chmod(0o600)
