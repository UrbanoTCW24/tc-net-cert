"""Import certification run folders from another station (merge, non-destructive)."""

from __future__ import annotations

import hashlib
import json
import shutil
import tarfile
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from tc_net_cert.storage.db import upsert_run
from tc_net_cert.storage.runs import RUN_ID_RE, find_run, list_runs


@dataclass
class ImportAction:
    run_id: str
    action: str  # copy, skip, conflict, copied
    detail: str = ""


@dataclass
class ImportResult:
    ok: bool
    dry_run: bool
    actions: list[ImportAction] = field(default_factory=list)
    copied: int = 0
    skipped: int = 0
    conflicts: int = 0
    reindexed: int = 0
    error: str = ""


def resolve_runs_root(path: Path) -> Path | None:
    """Accept a runs/ folder, a backup dir, or a tarball (handled elsewhere)."""
    path = path.resolve()
    if not path.exists():
        return None
    if path.is_file():
        return None
    if any(list_runs(path)):
        return path
    nested = path / "runs"
    if nested.is_dir() and any(list_runs(nested)):
        return nested
    var_nested = path / "var" / "lib" / "tc-net-cert" / "runs"
    if var_nested.is_dir() and any(list_runs(var_nested)):
        return var_nested
    return None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _discover_source_runs(source_root: Path) -> list[Path]:
    return list_runs(source_root)


def _backup_run_dir(run_dir: Path) -> Path:
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    backup = run_dir.with_name(f"{run_dir.name}.bak-import-{stamp}")
    shutil.copytree(run_dir, backup, dirs_exist_ok=False)
    return backup


def _copy_run_tree(source: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, dest, dirs_exist_ok=False)


def _reindex_run(runs_root: Path, run_dir: Path) -> bool:
    report_path = run_dir / "report.json"
    if not report_path.is_file():
        return False
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    dut = report.get("dut") if isinstance(report.get("dut"), dict) else {}
    upsert_run(
        runs_root,
        run_id=str(report.get("run_id") or run_dir.name),
        started_at=str(report.get("started_at_utc") or ""),
        finished_at=str(report.get("finished_at_utc") or ""),
        verdict=str(report.get("global_verdict") or ""),
        dut_type=str(dut.get("type") or ""),
        dut_model=str(dut.get("model") or ""),
        dut_serial=str(dut.get("serial") or ""),
        station=str(report.get("station") or ""),
        operator=str(report.get("operator") or ""),
        path=str(run_dir),
    )
    return True


def _merge_seq_counter(dest_root: Path, source_root: Path) -> None:
    highest = 0
    for root in (dest_root, source_root):
        counter = root / ".tc-seq"
        if counter.is_file():
            try:
                highest = max(highest, int(counter.read_text(encoding="utf-8").strip() or "0"))
            except ValueError:
                pass
        for path in root.rglob("TC-*"):
            if not path.is_dir() or RUN_ID_RE.fullmatch(path.name) is None:
                continue
            try:
                highest = max(highest, int(path.name.split("-", 1)[1]))
            except (IndexError, ValueError):
                continue
    if highest <= 0:
        return
    dest_root.mkdir(parents=True, exist_ok=True)
    (dest_root / ".tc-seq").write_text(f"{highest}\n", encoding="utf-8")


def import_runs(
    source: Path,
    dest_root: Path,
    *,
    dry_run: bool = True,
    force: bool = False,
    reindex: bool = True,
) -> ImportResult:
    """Merge run trees from source into dest_root. Never deletes source."""
    result = ImportResult(ok=True, dry_run=dry_run)
    extracted: tempfile.TemporaryDirectory[str] | None = None
    try:
        source_path = source.resolve()
        lowered = source_path.name.lower()
        if source_path.is_file() and (lowered.endswith(".tar.gz") or lowered.endswith(".tgz")):
            extracted = tempfile.TemporaryDirectory(prefix="tc-import-")
            with tarfile.open(source_path, "r:*") as archive:
                archive.extractall(extracted.name, filter="data")
            source_root = resolve_runs_root(Path(extracted.name))
        else:
            source_root = resolve_runs_root(source_path)
        if source_root is None:
            result.ok = False
            result.error = "No se encontró carpeta runs con órdenes TC- en el origen."
            return result
        dest_root = dest_root.resolve()
        dest_root.mkdir(parents=True, exist_ok=True)

        for src_dir in _discover_source_runs(source_root):
            run_id = src_dir.name
            if RUN_ID_RE.fullmatch(run_id) is None:
                continue
            rel = src_dir.relative_to(source_root)
            planned_dest = dest_root / rel
            existing = find_run(dest_root, run_id)
            dest_dir = existing if existing is not None else planned_dest

            src_report = src_dir / "report.json"
            if not src_report.is_file():
                result.actions.append(ImportAction(run_id, "skip", "sin report.json en origen"))
                result.skipped += 1
                continue

            if existing is not None and existing.is_dir():
                dest_report = existing / "report.json"
                if dest_report.is_file() and _sha256(dest_report) == _sha256(src_report):
                    result.actions.append(ImportAction(run_id, "skip", "ya presente (mismo report.json)"))
                    result.skipped += 1
                    continue
                if dest_report.is_file() and not force:
                    result.actions.append(
                        ImportAction(run_id, "conflict", "distinto contenido; usa --force o revisa manualmente")
                    )
                    result.conflicts += 1
                    result.ok = False
                    continue
                if dry_run and force:
                    result.actions.append(ImportAction(run_id, "would_replace", str(existing)))
                    result.copied += 1
                    continue
                if not dry_run and force and dest_report.is_file():
                    backup = _backup_run_dir(existing)
                    result.actions.append(ImportAction(run_id, "backup", str(backup)))

            if dry_run:
                label = "would_copy" if existing is None else "would_replace"
                target = planned_dest if existing is None else existing
                result.actions.append(ImportAction(run_id, label, str(target)))
                result.copied += 1
                continue

            if existing is None:
                _copy_run_tree(src_dir, planned_dest)
                dest_dir = planned_dest
            elif force:
                shutil.rmtree(existing)
                _copy_run_tree(src_dir, existing)
                dest_dir = existing
            result.actions.append(ImportAction(run_id, "copied", str(dest_dir)))
            result.copied += 1
            if reindex:
                if _reindex_run(dest_root, dest_dir):
                    result.reindexed += 1

        if not dry_run and result.copied:
            _merge_seq_counter(dest_root, source_root)
    except (OSError, tarfile.TarError) as exc:
        result.ok = False
        result.error = str(exc)[:240]
    finally:
        if extracted is not None:
            extracted.cleanup()
    if result.conflicts:
        result.ok = False
    return result


def format_import_report(result: ImportResult) -> str:
    lines = ["TECHCORP LAB — IMPORTACIÓN DE HISTORIAL", ""]
    if result.dry_run:
        lines.append("Modo: simulación (--dry-run). No se copió nada.")
    if result.error:
        lines.append(f"Error: {result.error}")
    for item in result.actions:
        lines.append(f"  {item.run_id}: {item.action} {item.detail}".rstrip())
    lines.extend(
        [
            "",
            f"Copiadas/simuladas: {result.copied}  Omitidas: {result.skipped}  Conflictos: {result.conflicts}",
        ]
    )
    if not result.dry_run and result.reindexed:
        lines.append(f"Filas reindexadas en index.sqlite: {result.reindexed}")
    lines.append("")
    lines.append("Estado: OK" if result.ok else "Estado: REVISAR CONFLICTOS O ERRORES")
    lines.append("")
    return "\n".join(lines)
