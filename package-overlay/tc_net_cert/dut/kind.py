"""Resolve ONT vs EMTA and DOCSIS generation for certificates and orchestration."""

from __future__ import annotations

import re

from tc_net_cert.core.models import DutIdentity

_EMTA_MODELS = re.compile(
    r"^(CG-?\d{4}|CGNV\d+|KAON|HITRON|HISTRON)",
    re.I,
)


def infer_dut_type(dut: DutIdentity | dict) -> str:
    """Return ``ont`` or ``emta``. Inventory and portal reads beat a stale config default."""
    if isinstance(dut, DutIdentity):
        data = dut.model_dump()
    else:
        data = dict(dut or {})
    explicit = str(data.get("type") or "").strip().lower()
    if explicit == "emta":
        return "emta"
    device_type = str(data.get("device_type") or "").lower()
    if "docsis" in device_type:
        return "emta"
    model = str(data.get("model") or "").strip()
    if _EMTA_MODELS.match(model.replace(" ", "")):
        return "emta"
    if any(
        str(data.get(key) or "").strip()
        for key in ("docsis_status", "ds_channels", "us_channels", "ds_power", "us_power")
    ):
        return "emta"
    if explicit == "ont":
        return "ont"
    return "ont"


def docsis_generation(device_type: str, model: str = "") -> str:
    """``3.1``, ``3.0``, or empty when unknown."""
    text = f"{device_type} {model}".lower()
    if "3.1" in text or "3.1" in text.replace(" ", ""):
        return "3.1"
    if "3.0" in text or re.search(r"\b30\b", text):
        return "3.0"
    token = (model or "").upper().replace(" ", "")
    if token in {"CG-3000", "CG3000"}:
        return "3.1"
    if token in {"CG-2200", "CG2200", "CGNV5"}:
        return "3.0"
    return ""
