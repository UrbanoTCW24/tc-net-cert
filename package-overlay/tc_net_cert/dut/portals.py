"""Saved modem portal logins, one profile per model.

The password stays in this file. It is not copied into a certificate.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import yaml

from tc_net_cert.dut.huawei import allowed_host, valid_password, valid_user

_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$")
_CG_MODEL = re.compile(r"CG-?\d{4}", flags=re.I)
_CGNV = re.compile(r"CGNV\d+\w*", flags=re.I)
_DEFAULTS = {
    "HG8145X6-10": {
        "host": "192.168.1.1",
        "username": "admin",
        "password": "c1@r0",
    },
    "HG8245W5-6T": {
        "host": "192.168.1.1",
        "username": "admin",
        "password": "c1@r0",
        "kind": "huawei-ont",
    },
    "CG-3000": {
        "host": "192.168.1.1",
        "username": "admin",
        "password": "Cl@r0",
        "kind": "emta-broadcom",
    },
    "CG-2200": {
        "host": "192.168.1.1",
        "username": "admin",
        "password": "Cl@r0",
        "kind": "emta-broadcom",
    },
    "CGNV5": {
        "host": "192.168.1.1",
        "username": "admin",
        "password": "Cl@r0",
        "kind": "emta-hitron",
    },
}


def portals_path() -> Path:
    import os

    override = os.environ.get("TC_NET_CERT_PORTALS", "").strip()
    if override:
        return Path(override)
    home = os.environ.get("HOME", "").strip() or str(Path.home())
    folder = Path(home) / ".config" / "tc-net-cert"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / "portals.yaml"


def valid_model(value: str) -> bool:
    return bool(_MODEL_RE.fullmatch(value))


def _username_looks_like_model(model: str, username: str) -> bool:
    """True when the saved login is the commercial model, not a portal user."""
    token = (model or "").strip().upper().replace(" ", "")
    user = (username or "").strip().upper().replace(" ", "")
    if not token or not user:
        return False
    if user == token:
        return True
    canon = canonical_profile_model(model).upper().replace(" ", "")
    if user == canon:
        return True
    if user in {"KAON", "CG-2200", "CG2200", "CG-3000", "CG3000"} and (
        token.startswith("CG") or token == "KAON"
    ):
        return True
    if _CG_MODEL.fullmatch(user.replace("-", "")) or _CGNV.fullmatch(user.replace("-", "")):
        return True
    return False


def normalize_emta_portal_username(model: str, username: str, item: dict | None = None) -> str:
    """Broadcom/Hitron EMTA portals expect ``admin``, not the model name in the user field."""
    kind = infer_portal_kind(model, item if isinstance(item, dict) else {})
    if not kind.startswith("emta"):
        return username
    if _username_looks_like_model(model, username):
        return "admin"
    return username


def repair_emta_portal_logins(path: Path | None = None) -> int:
    """Rewrite profiles that stored the model as usuario. Returns how many were fixed."""
    file = path or portals_path()
    current = load_portals(file)
    fixed = 0
    for model, item in list(current.items()):
        if not isinstance(item, dict):
            continue
        user = str(item.get("username") or "")
        if not _username_looks_like_model(model, user):
            continue
        kind = infer_portal_kind(model, item)
        if not kind.startswith("emta"):
            continue
        host = str(item.get("host") or "192.168.1.1")
        password = str(item.get("password") or "Cl@r0")
        save_portal(file, model, host, "admin", password, kind=kind)
        fixed += 1
    return fixed


def canonical_profile_model(model: str) -> str:
    """One commercial name per portal family (Kaon → CG-2200, CGNV5CLR → CGNV5)."""
    token = (model or "").upper().replace(" ", "")
    if token in {"CG3000", "CG-3000"}:
        return "CG-3000"
    if model in {"Kaon", "KAON"}:
        return "CG-2200"
    hitron = re.match(r"CGNV\d+", token.replace("-", ""))
    if hitron:
        return hitron.group(0)
    return model


def infer_portal_kind(profile_name: str, item: dict | None) -> str:
    """Classify a saved profile for fingerprint matching."""
    row = item if isinstance(item, dict) else {}
    explicit = str(row.get("kind") or row.get("portal_kind") or "").strip().lower()
    if explicit in {"ont", "huawei-ont"}:
        return "huawei-ont"
    if explicit in {"emta-hitron", "hitron"}:
        return "emta-hitron"
    if explicit in {"emta-broadcom", "broadcom", "kaon"}:
        return "emta-broadcom"
    if explicit == "emta":
        return "emta"
    name = profile_name.upper().replace(" ", "")
    if name.startswith("HG") or name.startswith("HN"):
        return "huawei-ont"
    if "CGNV" in name.replace("-", ""):
        return "emta-hitron"
    if name in {"KAON", "CG-2200", "CG-3000", "CG3000"} or name.startswith("CG-"):
        return "emta-broadcom"
    return "emta"


def _profile_meta(raw: dict, name: str) -> dict:
    item = raw.get(name) if isinstance(raw, dict) else None
    row = item if isinstance(item, dict) else {}
    hints = row.get("fingerprints")
    tokens = [str(part).lower() for part in hints] if isinstance(hints, list) else []
    return {
        "kind": infer_portal_kind(name, row),
        "fingerprints": [part for part in tokens if part],
    }


def _fingerprint_blob(home: str, challenge: str) -> str:
    return f"{home or ''}\n{challenge or ''}".lower()


def _hint_match(meta: dict, blob: str) -> bool:
    for token in meta.get("fingerprints") or []:
        if token in blob:
            return True
    return False


def match_saved_profile(
    fingerprint: dict,
    profiles: dict,
    home: str = "",
    challenge: str = "",
) -> dict[str, str]:
    """Pick the saved login that matches the probed portal fingerprint."""
    none = {"ok": "", "family": "", "model": "", "username": "", "password": "", "error": ""}
    if not isinstance(profiles, dict):
        profiles = {}
    cleaned: dict[str, dict[str, str]] = {}
    for model, item in profiles.items():
        name = str(model)
        if not isinstance(item, dict) or not valid_model(name):
            continue
        username = str(item.get("username") or "")
        password = str(item.get("password") or "")
        if valid_user(username) and valid_password(password):
            cleaned[name] = {"username": username, "password": password}
    blob = _fingerprint_blob(home, challenge)
    portal_kind = str(fingerprint.get("portal_kind") or "").strip()
    family = str(fingerprint.get("family") or "").strip()
    detected = canonical_profile_model(str(fingerprint.get("model") or ""))

    def success(name: str) -> dict[str, str]:
        saved = cleaned[name]
        kind = _profile_meta(profiles, name)["kind"]
        out_family = "ont" if kind == "huawei-ont" else "emta"
        return {
            "ok": "yes",
            "family": out_family,
            "model": name,
            "username": saved["username"],
            "password": saved["password"],
            "error": "",
        }

    if portal_kind == "huawei-ont" or family == "ont":
        model = str(fingerprint.get("model") or "")
        login = str(fingerprint.get("login") or "")
        if login and login != "rand-base64":
            label = model or "detectado"
            return {**none, "model": model, "error": f"El equipo {label} usa otra forma de acceso. No se intentó el ingreso."}
        if model and model in cleaned:
            return success(model)
        label = model or "detectado"
        return {**none, "model": model, "error": f"No hay un perfil guardado para {label}. No se intentó el ingreso."}

    if portal_kind == "emta-hitron":
        hitron_names = [name for name in cleaned if _profile_meta(profiles, name)["kind"] == "emta-hitron"]
        if not hitron_names:
            hitron_names = [name for name in cleaned if canonical_profile_model(name).startswith("CGNV")]
        if len(hitron_names) == 1:
            return success(hitron_names[0])
        if len(hitron_names) > 1:
            return {
                **none,
                "model": detected or "CGNV5",
                "error": f"Hay varios perfiles Hitron ({', '.join(hitron_names)}). Deja solo el del modelo conectado.",
            }
        return {**none, "model": detected or "CGNV5", "error": "No hay un perfil guardado para Hitron (CGNV5). No se intentó el ingreso."}

    broadcom = portal_kind == "emta-broadcom" or (family == "emta" and portal_kind != "emta-hitron")
    if broadcom or _emta_signature(blob):
        emta_kinds = {"emta-broadcom", "emta"}
        candidates = [name for name in cleaned if _profile_meta(profiles, name)["kind"] in emta_kinds]
        hint_hits = [name for name in candidates if _hint_match(_profile_meta(profiles, name), blob)]
        if len(hint_hits) == 1:
            return success(hint_hits[0])
        if detected:
            by_model = [
                name
                for name in candidates
                if canonical_profile_model(name) == detected or name == detected
            ]
            if len(by_model) == 1:
                return success(by_model[0])
            if len(by_model) > 1:
                return {
                    **none,
                    "model": detected,
                    "error": f"Varios perfiles coinciden con {detected}: {', '.join(by_model)}.",
                }
        if len(candidates) == 1:
            return success(candidates[0])
        if not candidates:
            label = detected or "EMTA"
            return {
                **none,
                "model": detected,
                "error": f"Portal {label} (Broadcom/Kaon) detectado; no hay perfil guardado. Escribe credenciales y pulsa Buscar.",
            }
        return {
            **none,
            "model": detected,
            "error": (
                f"Portal EMTA detectado"
                + (f" ({detected})" if detected else "")
                + f", pero hay varios perfiles ({', '.join(candidates)}). "
                "Guarda un perfil con el nombre del modelo o usa kind/fingerprints en portals.yaml."
            ),
        }

    hinted = [name for name in cleaned if _hint_match(_profile_meta(profiles, name), blob)]
    if len(hinted) == 1:
        return success(hinted[0])

    return {**none, "error": "El portal no coincide con un equipo conocido. No se intentó el ingreso."}


def _emta_signature(blob: str) -> bool:
    return "broadcom" in blob or "kaon" in blob or ("401" in blob and "basic" in blob)


def load_portals(path: Path | None = None) -> dict[str, dict[str, str]]:
    file = path or portals_path()
    if not file.is_file():
        return {}
    try:
        raw = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}
    profiles = raw.get("profiles") if isinstance(raw, dict) else None
    if not isinstance(profiles, dict):
        return {}
    loaded: dict[str, dict[str, str]] = {}
    for model, item in profiles.items():
        if not isinstance(item, dict) or not valid_model(str(model)):
            continue
        host = str(item.get("host") or "")
        username = normalize_emta_portal_username(str(model), str(item.get("username") or ""), item)
        password = str(item.get("password") or "")
        if not allowed_host(host) or not valid_user(username) or not valid_password(password):
            continue
        entry: dict[str, str | list[str]] = {"host": host, "username": username, "password": password}
        kind = str(item.get("kind") or item.get("portal_kind") or "").strip()
        if kind:
            entry["kind"] = kind
        hints = item.get("fingerprints")
        if isinstance(hints, list):
            entry["fingerprints"] = [str(part) for part in hints if str(part).strip()]
        loaded[str(model)] = entry  # type: ignore[assignment]
    return loaded  # type: ignore[return-value]


def ensure_portals(path: Path | None = None) -> dict[str, dict[str, str]]:
    file = path or portals_path()
    current = load_portals(file)
    if current:
        repair_emta_portal_logins(file)
        return load_portals(file)
    for model, item in _DEFAULTS.items():
        kind = str(item.get("kind") or "")
        save_portal(
            file,
            model,
            item["host"],
            item["username"],
            item["password"],
            kind=kind,
        )
    return load_portals(file)


def save_portal(
    path: Path,
    model: str,
    host: str,
    username: str,
    password: str,
    *,
    kind: str = "",
) -> None:
    if not valid_model(model) or not allowed_host(host) or not valid_user(username):
        raise ValueError("El perfil del módem no es válido.")
    current = load_portals(path)
    if not password:
        password = (current.get(model) or {}).get("password") or ""
    if not valid_password(password):
        raise ValueError("Escribe la contraseña del portal.")
    previous = current.get(model) if isinstance(current.get(model), dict) else {}
    row: dict[str, object] = {"host": host, "username": username, "password": password}
    for key in ("kind", "portal_kind", "fingerprints"):
        if isinstance(previous, dict) and previous.get(key) is not None:
            row[key] = previous[key]
    explicit_kind = str(kind or "").strip()
    if explicit_kind:
        row["kind"] = explicit_kind
    if not row.get("kind") and not row.get("portal_kind"):
        inferred = infer_portal_kind(model, row)
        if inferred:
            row["kind"] = inferred
    current[model] = row  # type: ignore[assignment]
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {"profiles": current}
    path.write_text(yaml.safe_dump(document, sort_keys=False, allow_unicode=True), encoding="utf-8")
    os.chmod(path, 0o600)


def delete_portal(path: Path, model: str) -> bool:
    name = str(model or "").strip()
    if not valid_model(name):
        return False
    current = load_portals(path)
    if name not in current:
        return False
    del current[name]
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {"profiles": current}
    path.write_text(yaml.safe_dump(document, sort_keys=False, allow_unicode=True), encoding="utf-8")
    os.chmod(path, 0o600)
    return True


def profile_for(model: str, path: Path | None = None) -> dict[str, str] | None:
    return ensure_portals(path).get(model)
