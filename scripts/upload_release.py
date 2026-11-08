#!/usr/bin/env python3
"""Publica un wheel en Supabase Storage y genera update_offer.json (HQ — no en wheel)."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from urllib.parse import quote

import requests


def _parse_env(path: Path) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        fields[key.strip()] = value.strip()
    return fields


def _release_settings(fields: dict[str, str]) -> tuple[str, str, str]:
    url = (
        fields.get("project_url")
        or fields.get("supabase_url")
        or fields.get("url")
        or ""
    ).rstrip("/")
    key = fields.get("service_role_key") or fields.get("storage_service_key") or fields.get("service_role") or ""
    bucket = fields.get("storage_bucket") or fields.get("release_bucket") or "lab-releases"
    return url, key, bucket


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_version_from_wheel(path: Path) -> str:
    import zipfile

    with zipfile.ZipFile(path) as zf:
        name = next(n for n in zf.namelist() if n.endswith("tc_net_cert/version.py"))
        text = zf.read(name).decode()
    match = re.search(r'__version__\s*=\s*"([^"]+)"', text)
    return match.group(1) if match else "0.0.0"


def upload_storage(project_url: str, service_key: str, bucket: str, object_path: str, body: bytes) -> None:
    endpoint = f"{project_url}/storage/v1/object/{bucket}/{quote(object_path, safe='/')}"
    headers = {
        "Authorization": f"Bearer {service_key}",
        "Content-Type": "application/octet-stream",
        "x-upsert": "true",
    }
    response = requests.post(endpoint, data=body, headers=headers, timeout=120)
    if response.status_code not in {200, 201}:
        raise RuntimeError(f"Storage upload failed ({response.status_code}): {response.text[:200]}")


def upsert_lab_release(env_path: Path, *, channel: str, version: str, whl_url: str, sha256: str, mandatory: bool) -> str:
    """Registra la fila OTA en public.lab_releases (usa credenciales Postgres del env)."""
    last = "sin conexión"
    try:
        from tc_net_cert.storage.supabase_runs import _settings
    except ImportError:
        import sys

        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "package-overlay"))
        from tc_net_cert.storage.supabase_runs import _settings

    settings = _settings(env_path)
    if not settings:
        return "Sin pooler/direct en env; omitiendo lab_releases."
    statement = """
        insert into public.lab_releases (channel, version, whl_url, sha256, mandatory, is_active)
        values (%(channel)s, %(version)s, %(whl_url)s, %(sha256)s, %(mandatory)s, true)
        on conflict (channel, version) do update set
            whl_url = excluded.whl_url,
            sha256 = excluded.sha256,
            mandatory = excluded.mandatory,
            is_active = true,
            updated_at = now()
    """
    payload = {
        "channel": channel,
        "version": version,
        "whl_url": whl_url,
        "sha256": sha256,
        "mandatory": mandatory,
    }
    try:
        import psycopg
    except ImportError:
        return "Falta psycopg; omitiendo lab_releases."
    for target in settings:
        try:
            with psycopg.connect(**target, connect_timeout=8) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(statement, payload)
                connection.commit()
            return ""
        except Exception as exc:
            last = str(exc).split("\n", 1)[0][:120]
    return f"No se pudo registrar lab_releases ({last})."


def public_url(project_url: str, bucket: str, object_path: str, public: bool) -> str:
    if public:
        return f"{project_url}/storage/v1/object/public/{bucket}/{quote(object_path, safe='/')}"
    return f"{project_url}/storage/v1/object/sign/{bucket}/{quote(object_path, safe='/')}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sube tc-net-cert wheel a Supabase Storage.")
    parser.add_argument("--whl", type=Path, required=True, help="Ruta al archivo .whl")
    parser.add_argument(
        "--env",
        type=Path,
        default=Path.home() / ".config" / "tc-net-cert" / "supabase.env",
        help="Archivo con project_url y service_role_key (HQ)",
    )
    parser.add_argument("--bucket", default="", help="Bucket Storage (default: lab-releases)")
    parser.add_argument("--prefix", default="releases", help="Prefijo de carpeta en el bucket")
    parser.add_argument("--channel", default="lab")
    parser.add_argument("--mandatory", action="store_true")
    parser.add_argument("--public", action="store_true", help="Bucket/objeto público (URL sin firma)")
    parser.add_argument("--dry-run", action="store_true", help="Solo genera JSON local, sin subir")
    parser.add_argument("--out", type=Path, default=Path("dist_usb/update_offer.json"))
    args = parser.parse_args(argv)

    whl = args.whl.resolve()
    if not whl.is_file():
        print("No existe el wheel.", file=sys.stderr)
        return 2

    version = _read_version_from_wheel(whl)
    digest = _sha256(whl)
    object_name = f"{args.prefix}/{whl.name}"

    offer = {
        "version": version,
        "url": "",
        "mandatory": bool(args.mandatory),
        "channel": args.channel,
        "sha256": digest,
        "wheel": whl.name,
    }

    if args.dry_run:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(offer, indent=2) + "\n", encoding="utf-8")
        print(f"[dry-run] {args.out}")
        return 0

    if not args.env.is_file():
        print(f"Falta {args.env} (project_url + service_role_key para HQ).", file=sys.stderr)
        return 2

    fields = _parse_env(args.env)
    project_url, service_key, default_bucket = _release_settings(fields)
    bucket = args.bucket or default_bucket

    if not project_url or not service_key:
        print(
            "supabase.env debe incluir project_url= y service_role_key= (solo máquina de publicación HQ).",
            file=sys.stderr,
        )
        return 2

    body = whl.read_bytes()
    upload_storage(project_url, service_key, bucket, object_name, body)
    offer["url"] = public_url(project_url, bucket, object_name, args.public)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(offer, indent=2) + "\n", encoding="utf-8")

    version_path = args.out.parent / "version.json"
    version_path.write_text(
        json.dumps(
            {
                "name": "tc-net-cert",
                "version": version,
                "wheel": whl.name,
                "sha256": digest,
                "channel": args.channel,
                "url": offer["url"],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    hint = upsert_lab_release(
        args.env,
        channel=args.channel,
        version=version,
        whl_url=offer["url"],
        sha256=digest,
        mandatory=bool(args.mandatory),
    )
    if hint:
        print(hint)

    print(f"Publicado: {offer['url']}")
    print(f"Manifiesto: {args.out}")
    print("Las estaciones verán el botón tras «Buscar actualización» en el panel.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
