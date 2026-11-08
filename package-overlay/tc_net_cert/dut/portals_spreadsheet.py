"""Export / import portal login profiles for Excel (CSV con punto y coma)."""

from __future__ import annotations

import csv
import io
from pathlib import Path

from tc_net_cert.dut.portals import infer_portal_kind, load_portals, portals_path, save_portal, valid_model
from tc_net_cert.dut.huawei import allowed_host, valid_password, valid_user

_HEADER = ("marca", "modelo", "host", "usuario", "contraseña", "tipo")


def portal_brand(model: str, kind: str = "") -> str:
    name = (model or "").upper().replace(" ", "")
    inferred = (kind or infer_portal_kind(model, {})).lower()
    if inferred == "huawei-ont" or name.startswith(("HG", "HN")):
        return "HUAWEI"
    if inferred == "emta-hitron" or "CGNV" in name.replace("-", ""):
        return "HITRON"
    if inferred == "emta-broadcom" or name.startswith("CG-") or name in {"KAON", "CG3000"}:
        return "KAON"
    if inferred.startswith("emta"):
        return "EMTA"
    if name.startswith("HG"):
        return "HUAWEI"
    return "OTRO"


def list_portal_rows(path: Path | None = None) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for model, item in sorted(load_portals(path).items()):
        kind = str(item.get("kind") or item.get("portal_kind") or "")
        rows.append(
            {
                "brand": portal_brand(model, kind),
                "model": model,
                "host": str(item.get("host") or ""),
                "username": str(item.get("username") or ""),
                "password": str(item.get("password") or ""),
                "kind": kind or infer_portal_kind(model, item),
            }
        )
    return rows


def export_portals_csv(path: Path | None = None) -> tuple[bytes, str]:
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";")
    writer.writerow(_HEADER)
    for row in list_portal_rows(path):
        writer.writerow([row["brand"], row["model"], row["host"], row["username"], row["password"], row["kind"]])
    payload = "\ufeff" + buffer.getvalue()
    return payload.encode("utf-8"), "perfiles-portal-techcorp.csv"


def _norm_header(cell: str) -> str:
    text = (cell or "").strip().lower().replace("á", "a").replace("é", "e").replace("í", "i").replace("ó", "o").replace("ú", "u")
    aliases = {
        "marca": "marca",
        "brand": "marca",
        "modelo": "modelo",
        "model": "modelo",
        "host": "host",
        "ip": "host",
        "portal": "host",
        "usuario": "usuario",
        "user": "usuario",
        "username": "usuario",
        "contraseña": "password",
        "contrasena": "password",
        "password": "password",
        "clave": "password",
        "tipo": "kind",
        "kind": "kind",
        "tecnologia": "kind",
    }
    return aliases.get(text, text)


def import_portals_csv(raw: str, path: Path | None = None) -> tuple[int, list[str]]:
    """Merge rows into portals.yaml. Returns count saved and warning lines."""
    store = path or portals_path()
    text = raw.lstrip("\ufeff").strip()
    if not text:
        raise ValueError("El archivo está vacío.")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=";,\t")
    except csv.Error:
        dialect = csv.excel
        dialect.delimiter = ";"
    reader = csv.reader(io.StringIO(text), dialect)
    rows = list(reader)
    if not rows:
        raise ValueError("No hay filas en el archivo.")
    header = [_norm_header(cell) for cell in rows[0]]
    if "modelo" not in header:
        header = list(_HEADER)
        data_rows = rows
    else:
        data_rows = rows[1:]
    index = {name: header.index(name) for name in header if name in _HEADER or name == "password"}
    required = {"modelo", "host", "usuario", "password"}
    if not required.issubset(set(index)):
        raise ValueError("Faltan columnas. Use: marca;modelo;host;usuario;contraseña;tipo")
    saved = 0
    notes: list[str] = []
    for line_no, cells in enumerate(data_rows, start=2):
        if not any(str(c).strip() for c in cells):
            continue
        def col(key: str, default: str = "") -> str:
            pos = index.get(key)
            if pos is None or pos >= len(cells):
                return default
            return str(cells[pos]).strip()

        model = col("modelo")
        if not valid_model(model):
            notes.append(f"Fila {line_no}: modelo inválido «{model}», omitida.")
            continue
        host = col("host", "192.168.1.1")
        user = col("usuario", "admin")
        password = col("password")
        if not allowed_host(host) or not valid_user(user) or not valid_password(password):
            notes.append(f"Fila {line_no}: datos incompletos para {model}, omitida.")
            continue
        kind = col("kind")
        save_portal(store, model, host, user, password, kind=kind)
        saved += 1
    if saved == 0:
        raise ValueError("Ninguna fila válida. Revise modelo, host, usuario y contraseña.")
    return saved, notes

