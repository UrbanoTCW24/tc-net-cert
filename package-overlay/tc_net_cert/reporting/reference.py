"""Reference profiles by technology and model.

ONT (GPON) profiles compare optical power. EMTA (DOCSIS) profiles compare
RF levels. LAN throughput on this station stays 900 Mbps for both.
"""

from __future__ import annotations

import re
from html import escape
from pathlib import Path

import yaml

_ONT_LIMITS = {
    "rx_low": -27.0,
    "rx_high": -8.0,
    "tx_low": 0.5,
    "tx_high": 5.0,
    "voltage_low": 3100.0,
    "voltage_high": 3500.0,
    "bias_low": 0.0,
    "bias_high": 90.0,
    "temperature_low": None,
    "temperature_high": 75.0,
    "throughput_mbps": 900.0,
    "jitter_ms": 10.0,
    "loss_percent": 1.0,
    "wifi_24_mbps": 0.0,
    "wifi_50_mbps": 0.0,
    "ds_low": -10.0,
    "ds_high": 10.0,
    "us_low": 30.0,
    "us_high": 61.0,
    "mer_min": 28.0,
    "mer_low": 28.0,
    "mer_high": 35.0,
    "ds_channels_min": 16.0,
    "uncorrectables_max": 1000.0,
    "ont_state": "O5",
}
_EMTA_LIMITS = {
    **_ONT_LIMITS,
    "ont_state": "",
    "temperature_high": 75.0,
    "jitter_ms": 30.0,
    "ds_low": -10.0,
    "ds_high": 10.0,
    "us_low": 30.0,
    "us_high": 61.0,
    "mer_min": 28.0,
    "mer_low": 28.0,
    "mer_high": 35.0,
    "ds_channels_min": 16.0,
    "uncorrectables_max": 1000.0,
}
BUILTIN_PROFILES = (
    {"name": "HG8145X6-10", "technology": "ONT", "technology_label": "GPON", "model": "HG8145X6-10", **_ONT_LIMITS},
    {"name": "HG8245W5-6T", "technology": "ONT", "technology_label": "GPON", "model": "HG8245W5-6T", **_ONT_LIMITS},
    {"name": "CG-2200", "technology": "EMTA", "technology_label": "DOCSIS", "model": "CG-2200", **_EMTA_LIMITS},
    {"name": "CG-3000", "technology": "EMTA", "technology_label": "DOCSIS", "model": "CG-3000", **_EMTA_LIMITS},
    {"name": "CGNV5", "technology": "EMTA", "technology_label": "DOCSIS", "model": "CGNV5", **_EMTA_LIMITS},
)
DEFAULT_REFERENCE = {
    "name": "ont-gpon",
    "technology": "ONT",
    "technology_label": "GPON",
    "model": "ONT",
    **_ONT_LIMITS,
}
_RANGES = (
    ("rx_low", "rx_high"),
    ("tx_low", "tx_high"),
    ("voltage_low", "voltage_high"),
    ("bias_low", "bias_high"),
    ("temperature_low", "temperature_high"),
)
_NAME_RE_TEXT = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$"


def reference_path() -> Path:
    return Path.home() / ".config" / "tc-net-cert" / "reference.yaml"


_STRINGS = {"technology", "technology_label", "model", "ont_state"}
_STORED = tuple(key for key in _ONT_LIMITS) + ("technology", "technology_label", "model")
_RULES = {"exists", "range", "equal", "one", "pattern"}
_REQUIRED = (
    "rx",
    "tx",
    "voltage",
    "bias",
    "temperature",
    "ont_state",
    "jitter",
    "loss",
    "throughput",
    "ds",
    "us",
    "mer",
    "ds_channels",
    "uncorrectables",
    "brand",
    "gpon",
    "serial",
    "mac",
    "mgmt_ip",
    "ssid_24",
    "psk_24",
    "ssid_50",
    "psk_50",
    "hardware",
    "software",
    "model_name",
    "docsis",
)


def _alias(model: str) -> str:
    token = model.upper().replace(" ", "")
    if token in {"CG3000", "CG-3000"}:
        return "CG-3000"
    if model in {"Kaon", "KAON"}:
        return "CG-2200"
    hitron = re.match(r"CGNV\d+", token.replace("-", ""))
    if hitron:
        return hitron.group(0)
    return model


def _raw(file: Path) -> dict:
    if not file.is_file():
        return {}
    loaded = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
    return loaded if isinstance(loaded, dict) else {}


def _saved_profiles(path: Path | None) -> dict:
    profiles = _raw(path or reference_path()).get("profiles")
    return profiles if isinstance(profiles, dict) else {}


def _active_saved(path: Path | None) -> dict:
    raw = _raw(path or reference_path())
    profiles = raw.get("profiles") if isinstance(raw.get("profiles"), dict) else {}
    item = profiles.get(str(raw.get("active") or ""))
    return item if isinstance(item, dict) else {}


def _merge(name: str, base: dict, saved: dict) -> dict:
    merged = dict(base)
    merged["name"] = name
    for key, value in saved.items():
        if key in _STRINGS:
            merged[key] = str(value)
        elif key in _ONT_LIMITS and key != "ont_state":
            merged[key] = None if value is None or value == "" else float(value)
    merged["rules"] = _choice_map(saved.get("rules"), _RULES)
    merged["required"] = _flag_map(saved.get("required"))
    if merged.get("technology") == "EMTA":
        merged["ont_state"] = ""
        merged["technology_label"] = "DOCSIS"
    else:
        merged["technology"] = "ONT"
        merged["technology_label"] = "GPON"
        merged["ont_state"] = str(merged.get("ont_state") or "O5")
    return merged


def profile_catalog(path: Path | None = None) -> list[dict]:
    """Built-in model profiles, with any saved numbers for that same model."""
    saved = _saved_profiles(path)
    rows = [_merge(item["name"], item, saved.get(item["name"]) or {}) for item in BUILTIN_PROFILES]
    known = {item["name"] for item in rows}
    for name, item in saved.items():
        if name not in known and isinstance(item, dict):
            rows.append(_merge(name, DEFAULT_REFERENCE, item))
    return rows


def _normalize_gpon_state_code(raw: str) -> str:
    token = (raw or "").strip().upper().split()[0]
    if token.startswith("O") and len(token) == 2 and token[1].isdigit():
        return token
    if token.isdigit():
        width = 2 if len(token) <= 2 else len(token)
        return f"O{token.zfill(width)[-2:]}"
    return token


def ont_state_expectation(reference: dict) -> str:
    allowed = reference.get("allowed_gpon_states")
    if isinstance(allowed, list) and allowed:
        labels = []
        for item in allowed:
            code = _normalize_gpon_state_code(str(item))
            labels.append(code if code else str(item))
        return " o ".join(labels)
    return str(reference.get("ont_state") or "O5")


def ont_state_passes(reference: dict, raw_state: str) -> bool:
    read = _normalize_gpon_state_code(raw_state)
    if not read:
        return False
    allowed = reference.get("allowed_gpon_states")
    if isinstance(allowed, list) and allowed:
        accepted = {_normalize_gpon_state_code(str(item)) for item in allowed}
        accepted |= {code[1:] if code.startswith("O") else code for code in accepted}
        token = read[1:] if read.startswith("O") else read
        return read in accepted or token in accepted
    expected = _normalize_gpon_state_code(str(reference.get("ont_state") or "O5"))
    return read == expected


def load_reference(
    path: Path | None = None,
    *,
    model: str = "",
    kind: str = "",
    country_code: str = "",
    station: str = "",
) -> dict:
    from tc_net_cert.config.remote_sync import apply_profile_overlay, resolve_country_code

    catalog = profile_catalog(path)
    by_name = {item["name"]: item for item in catalog}
    wanted = {"ont": "ONT", "emta": "EMTA"}.get(kind, "")
    chosen = by_name.get(model) or by_name.get(_alias(model))
    if chosen and wanted and chosen["technology"] != wanted:
        chosen = None
    if chosen is None and wanted == "EMTA":
        chosen = by_name.get("CG-2200")
    country = country_code or resolve_country_code(station)
    if chosen is not None:
        return apply_profile_overlay(chosen, country_code=country, station=station)
    merged = _merge(str(DEFAULT_REFERENCE["name"]), DEFAULT_REFERENCE, _active_saved(path))
    return apply_profile_overlay(merged, country_code=country, station=station)


def save_reference(name: str, values: dict, path: Path | None = None) -> dict:
    import re

    label = name.strip()
    if not re.fullmatch(_NAME_RE_TEXT, label):
        raise ValueError("El nombre del perfil de referencia no es válido.")
    base = next((dict(item) for item in BUILTIN_PROFILES if item["name"] == label), dict(DEFAULT_REFERENCE))
    cleaned = _merge(label, base, {})
    technology = str(values.get("technology") or cleaned["technology"])
    if technology not in {"ONT", "EMTA"}:
        raise ValueError("La tecnología de referencia tiene que ser ONT o EMTA.")
    cleaned["technology"] = technology
    cleaned["technology_label"] = "DOCSIS" if technology == "EMTA" else "GPON"
    cleaned["model"] = label if label in {item["name"] for item in BUILTIN_PROFILES} else cleaned["model"]
    for key in _ONT_LIMITS:
        if key == "ont_state" or key not in values:
            continue
        incoming = values[key]
        cleaned[key] = None if incoming is None or incoming == "" else float(incoming)
    if technology == "ONT":
        state = str(values.get("ont_state") or cleaned.get("ont_state") or "O5").strip()
        if not state or len(state) > 16 or any(char in state for char in "\n\r<>"):
            raise ValueError("El estado ONT de referencia no es válido.")
        cleaned["ont_state"] = state
        ranges = _RANGES
    else:
        cleaned["ont_state"] = ""
        ranges = (("ds_low", "ds_high"), ("us_low", "us_high"), ("mer_low", "mer_high"))
        if cleaned.get("mer_low") is not None:
            cleaned["mer_min"] = float(cleaned["mer_low"])
    for low_key, high_key in ranges:
        low = cleaned[low_key]
        high = cleaned[high_key]
        if low is not None and high is not None and float(low) >= float(high):
            raise ValueError("En la referencia, el mínimo tiene que ser menor que el máximo.")
    cleaned["rules"] = _choice_map(values.get("rules"), _RULES)
    cleaned["required"] = _flag_map(values.get("required"))
    if technology == "EMTA":
        if cleaned.get("mer_min") is not None and float(cleaned["mer_min"]) < 0:
            raise ValueError("El MER de referencia no puede ser negativo.")
        if cleaned.get("ds_channels_min") is None or float(cleaned["ds_channels_min"]) < 1:
            raise ValueError("Los canales downstream de referencia tienen que ser al menos 1.")
        if cleaned.get("uncorrectables_max") is None or float(cleaned["uncorrectables_max"]) < 0:
            raise ValueError("El máximo de incorregibles no puede ser negativo.")
    file = path or reference_path()
    file.parent.mkdir(parents=True, exist_ok=True)
    raw = _raw(file)
    profiles = raw.get("profiles") if isinstance(raw.get("profiles"), dict) else {}
    for item in BUILTIN_PROFILES:
        profiles.setdefault(item["name"], {key: item[key] for key in _STORED})
    stored = {key: cleaned[key] for key in _STORED}
    stored["rules"] = cleaned.get("rules") or {}
    stored["required"] = cleaned.get("required") or {}
    profiles[label] = stored
    raw["active"] = label
    raw["profiles"] = profiles
    file.write_text(yaml.safe_dump(raw, sort_keys=False, allow_unicode=True), encoding="utf-8")
    file.chmod(0o600)
    return cleaned


def install_builtin_profiles(path: Path | None = None) -> None:
    """Keep the known models in the file without changing numbers already saved."""
    file = path or reference_path()
    file.parent.mkdir(parents=True, exist_ok=True)
    raw = _raw(file)
    profiles = raw.get("profiles") if isinstance(raw.get("profiles"), dict) else {}
    changed = False
    for item in BUILTIN_PROFILES:
        if item["name"] not in profiles:
            profiles[item["name"]] = {key: item[key] for key in _STORED}
            changed = True
    if not changed and file.is_file():
        return
    raw["active"] = str(raw.get("active") or DEFAULT_REFERENCE["name"])
    raw["profiles"] = profiles
    file.write_text(yaml.safe_dump(raw, sort_keys=False, allow_unicode=True), encoding="utf-8")
    file.chmod(0o600)


def _choice_map(value: object, allowed: set[str]) -> dict:
    if not isinstance(value, dict):
        return {}
    return {key: str(item) for key, item in value.items() if key in _REQUIRED and str(item) in allowed}


def _flag_map(value: object) -> dict:
    if not isinstance(value, dict):
        return {}
    return {key: bool(item) for key, item in value.items() if key in _REQUIRED}


def expected_window(low: object, high: object, unit: str) -> str:
    """Certificate phrase for a range. A blank side stays open: ≤ max or ≥ min."""

    def show(value: object) -> str | None:
        if value is None or value == "":
            return None
        return _trim(float(value))

    left = show(low)
    right = show(high)
    suffix = f" {unit}" if unit else ""
    if left and right:
        return f"{left} … {right}{suffix}"
    if right:
        return f"≤ {right}{suffix}"
    if left:
        return f"≥ {left}{suffix}"
    return "—"


_ONT_PHRASE = {
    "O1": "Estado inicial",
    "O2": "Espera",
    "O3": "Número de serie",
    "O4": "Ranging",
    "O5": "Operación",
    "O6": "POPUP",
    "O7": "Parada de emergencia",
}


def optical_chart(
    rx: float | None,
    tx: float | None,
    reference: dict,
    model: str = "",
    ont_state: str = "",
    link: str = "",
    detail: dict | None = None,
) -> str:
    """GPON level meter at 1490 nm (RX) and 1310 nm (TX). Bars use this unit's reading."""
    extra = detail or {}
    rx_low = float(reference["rx_low"])
    rx_high = float(reference["rx_high"])
    tx_low = float(reference["tx_low"])
    tx_high = float(reference["tx_high"])
    expected_state = str(reference.get("ont_state") or "O5").strip().upper()
    raw_state = (ont_state or "").strip().upper()
    state = raw_state.split()[0] if raw_state else ""
    link_text = (link or "").strip() or "SIN DATO"
    unit_name = (model or "").strip() or "esta unidad"
    rx_text = "SIN DATO" if rx is None else f"{rx:.2f} dBm"
    tx_text = "SIN DATO" if tx is None else f"{tx:.2f} dBm"
    kind = "Medido" if rx is not None or tx is not None else "Sin dato"
    rx_mark, rx_color = _optic_mark(rx, rx_low, rx_high, "#0284c7")
    tx_mark, tx_color = _optic_mark(tx, tx_low, tx_high, "#16a34a")
    phrase = _ONT_PHRASE.get(state, "")
    shown_state = f"{state} · {phrase}" if phrase else (state or "SIN DATO")
    state_color = "#1a1a1a"
    if state and state == expected_state:
        state_color = "#16a34a"
    elif state:
        state_color = "#b91c1c"
    chips = [
        f'<div class="chip"><small>RX 1490 nm</small><b style="color:{rx_color}">{rx_text}</b></div>',
        f'<div class="chip"><small>TX 1310 nm</small><b style="color:{tx_color}">{tx_text}</b></div>',
        f'<div class="chip"><small>Estado ONT</small><b style="color:{state_color}">{escape(shown_state)}</b></div>',
        f'<div class="chip"><small>Tipo</small><b>{escape(kind)}</b></div>',
    ]
    levels = [
        f'<div><small>RX OPTICAL POWER (1490 nm)</small><b style="color:{rx_color}">{rx_text}</b>'
        f'<span>Esp. {_trim(rx_low)} a {_trim(rx_high)} dBm</span>'
        f'<strong style="color:{rx_color}">{rx_mark}</strong></div>',
        f'<div><small>TX OPTICAL POWER (1310 nm)</small><b style="color:{tx_color}">{tx_text}</b>'
        f'<span>Esp. {_trim(tx_low)} a {_trim(tx_high)} dBm</span>'
        f'<strong style="color:{tx_color}">{tx_mark}</strong></div>',
        f'<div><small>ESTADO ONT</small><b style="color:{state_color}">{escape(shown_state)}</b></div>',
        f'<div><small>LINK ETHERNET</small><b>{escape(link_text)}</b></div>',
    ]
    for label, key in (
        ("SERVICIO OLT", "olt_service"),
        ("EMS", "ems_status"),
        ("ACS", "acs_status"),
        ("ENLACE ÓPTICO", "optic_link"),
    ):
        text = str(extra.get(key) or "").strip()
        if text and text != "--":
            levels.append(f"<div><small>{label}</small><b>{escape(text)}</b></div>")
    return (
        '<section class="optic">'
        "<h3>ESPECTRO DE SEÑAL ÓPTICA</h3>"
        '<div class="optic-top">'
        '<p class="legend"><span class="swatch rx"></span>1490 nm (RX) '
        '<span class="swatch tx"></span>1310 nm (TX)</p>'
        f'<div class="chips">{"".join(chips)}</div></div>'
        + _optic_meter(rx, tx, rx_low, rx_high, tx_low, tx_high, state, phrase, extra)
        + f'<p class="optic-note">Medido en {escape(unit_name)}: RX {rx_text} · TX {tx_text}. '
        "O5 es operación: la ONT quedó registrada y pasa tráfico.</p>"
        "</section>"
        '<section class="optic levels-card">'
        "<h3>NIVELES ÓPTICOS GPON · CUMPLIMIENTO</h3>"
        f'<div class="levels">{"".join(levels)}</div></section>'
    )


def _optic_mark(value: float | None, low: float, high: float, color: str) -> tuple[str, str]:
    if value is None:
        return "SIN DATO", "#3d4a5c"
    if low <= value <= high:
        return "CUMPLE", color
    return "NO CUMPLE", "#b91c1c"


def _optic_meter(
    rx: float | None,
    tx: float | None,
    rx_low: float,
    rx_high: float,
    tx_low: float,
    tx_high: float,
    state: str,
    phrase: str,
    detail: dict,
) -> str:
    """Two level gauges. A missing dBm stays SIN DATO and does not draw a bar."""
    banner = f"{state} · {phrase}" if state and phrase else (state or "SIN DATO")
    banner_color = "#4ade80" if state == "O5" else ("#fb7185" if state else "#94a3b8")
    side = ""
    parts = [
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 720 248" width="100%" class="rf-meter" role="img" aria-label="Espectro de señal óptica">',
        '<rect x="0" y="0" width="720" height="248" rx="12" fill="#0b1220"/>',
        f'<text x="20" y="24" font-size="13" font-family="Arial" font-weight="700" fill="{banner_color}">{escape(banner)}</text>',
        *(
            [f'<text x="700" y="24" font-size="11" font-family="Arial" fill="#94a3b8" text-anchor="end">{escape(side)}</text>']
            if side
            else []
        ),
    ]
    _gauge(parts, 16, "DOWNSTREAM ÓPTICO · RX", "1490 nm", rx, rx_low, rx_high, "#38bdf8")
    _gauge(parts, 368, "UPSTREAM ÓPTICO · TX", "1310 nm", tx, tx_low, tx_high, "#4ade80")
    parts.append("</svg>")
    return "".join(parts)


def _gauge(
    parts: list[str],
    x: float,
    title: str,
    wavelength: str,
    value: float | None,
    low: float,
    high: float,
    good: str,
) -> None:
    top, bottom = 48.0, 214.0
    pad = max(1.5, (high - low) * 0.45)
    scale_low = low - pad
    scale_high = high + pad
    if value is not None:
        scale_low = min(scale_low, value - 1.2)
        scale_high = max(scale_high, value + 1.2)
    span = scale_high - scale_low or 1.0

    def y_px(level: float) -> float:
        ratio = (scale_high - level) / span
        return top + max(0.0, min(1.0, ratio)) * (bottom - top)

    inside = value is not None and low <= value <= high
    color = good if inside else "#fb7185"
    stroke = good if inside else "#b91c1c"
    parts.append(f'<rect x="{x}" y="36" width="336" height="196" rx="10" fill="#111827"/>')
    parts.append(
        f'<text x="{x + 16}" y="56" font-size="11" font-family="Arial" font-weight="700" fill="#e2e8f0">{escape(title)}</text>'
    )
    parts.append(
        f'<text x="{x + 320}" y="56" font-size="11" font-family="Arial" fill="#94a3b8" text-anchor="end">{wavelength}</text>'
    )
    track_x = x + 168
    parts.append(f'<rect x="{track_x}" y="{top}" width="18" height="{bottom - top}" rx="4" fill="#1e293b"/>')
    band_top = y_px(high)
    band_bottom = y_px(low)
    parts.append(
        f'<rect x="{track_x}" y="{band_top:.1f}" width="18" height="{max(1.0, band_bottom - band_top):.1f}" fill="#14532d" opacity="0.85"/>'
    )
    for limit in (high, low):
        y = y_px(limit)
        parts.append(
            f'<line x1="{track_x - 8}" y1="{y:.1f}" x2="{track_x + 26}" y2="{y:.1f}" stroke="#4ade80" stroke-dasharray="3 3"/>'
        )
        parts.append(
            f'<text x="{track_x - 14}" y="{y + 3:.1f}" font-size="10" font-family="Arial" fill="#86efac" text-anchor="end">{_trim(limit)}</text>'
        )
    if value is None:
        parts.append(
            f'<text x="{x + 168}" y="150" font-size="14" font-family="Arial" fill="#64748b" text-anchor="middle">SIN DATO</text>'
        )
        return
    y = y_px(value)
    parts.append(
        f'<rect x="{track_x + 3}" y="{y:.1f}" width="12" height="{max(2.0, bottom - y):.1f}" rx="3" fill="{color}" stroke="{stroke}" stroke-width="1.4"/>'
    )
    parts.append(
        f'<text x="{track_x + 36}" y="{y + 4:.1f}" font-size="13" font-family="Arial" font-weight="700" fill="#f8fafc">{value:.2f} dBm</text>'
    )


def _trim(value: float) -> str:
    return f"{value:.2f}".rstrip("0").rstrip(".")
