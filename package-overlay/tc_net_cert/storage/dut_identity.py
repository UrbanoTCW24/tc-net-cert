"""Last DUT serial read on this station (for reusing the same TC order)."""

from __future__ import annotations

import json
from pathlib import Path


def identity_path() -> Path:
    return Path.home() / ".local" / "state" / "tc-net-cert" / "last-dut-identity.json"


def store_identity(*, serial: str, mac: str = "", brand: str = "", model: str = "") -> None:
    text = (serial or "").strip()
    if not text or len(text) > 40 or " " in text:
        return
    path = identity_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "serial": text,
                "mac": (mac or "").strip()[:17],
                "brand": (brand or "").strip()[:40],
                "model": (model or "").strip()[:40],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)


def cached_serial() -> str:
    path = identity_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    text = str(payload.get("serial") or "").strip()
    if not text or len(text) > 40 or " " in text:
        return ""
    return text
