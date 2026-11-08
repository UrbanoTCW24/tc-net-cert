"""Seal a finished run. sha256sums.txt is written last and is not hashed inside itself."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def seal_run(run_dir: Path, manifest: dict) -> dict[str, str]:
    """Hash evidence, write manifest.json, then write sha256sums.txt including the manifest."""
    manifest_path = run_dir / "manifest.json"
    sums_path = run_dir / "sha256sums.txt"
    if manifest_path.exists():
        manifest_path.unlink()
    if sums_path.exists():
        sums_path.unlink()
    hashes = _hash_tree(run_dir)
    manifest = dict(manifest)
    manifest["files"] = [
        {"path": name, "sha256": digest} for name, digest in sorted(hashes.items())
    ]
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    hashes[manifest_path.name] = sha256_file(manifest_path)
    lines = [f"{digest}  {name}" for name, digest in sorted(hashes.items())]
    sums_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return hashes


def verify_sums(run_dir: Path) -> list[str]:
    sums_path = run_dir / "sha256sums.txt"
    problems: list[str] = []
    if not sums_path.exists():
        return ["sha256sums.txt is missing"]
    for line in sums_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        digest, name = line.split(maxsplit=1)
        path = run_dir / name.strip()
        if not path.exists():
            problems.append(f"missing {name}")
            continue
        actual = sha256_file(path)
        if actual != digest:
            problems.append(f"hash mismatch {name}")
    return problems


def _hash_tree(run_dir: Path) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for path in sorted(run_dir.rglob("*")):
        if not path.is_file():
            continue
        if path.name in {"sha256sums.txt", "manifest.json"}:
            continue
        hashes[str(path.relative_to(run_dir))] = sha256_file(path)
    return hashes
