"""Allocate service-order directories TC-0001, TC-0002, and so on.

The number is global. It does not restart each day, and a used number is
not issued again. Older TCNC-YYYYMMDD-NNN folders stay where they are.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from datetime import datetime
from pathlib import Path

_LEGACY_RE = re.compile(r"^TCNC-\d{8}-\d{3}$")
_SERVICE_RE = re.compile(r"^TC-(\d{4,})$")
RUN_ID_RE = re.compile(r"^(?:TC-\d{4,}|TCNC-\d{8}-\d{3})$")


def allocate_run(runs_root: Path, when: datetime | None = None) -> tuple[str, Path]:
    moment = when or datetime.now().astimezone()
    folder = runs_root / moment.strftime("%Y") / moment.strftime("%Y-%m-%d")
    folder.mkdir(parents=True, exist_ok=True)
    lock_path = runs_root / ".tc-seq.lock"
    counter_path = runs_root / ".tc-seq"
    with lock_path.open("a+", encoding="utf-8") as handle:
        _lock(handle)
        number = _highest_service_order(runs_root)
        if counter_path.is_file():
            try:
                number = max(number, int(counter_path.read_text(encoding="utf-8").strip() or "0"))
            except ValueError:
                pass
        while True:
            number += 1
            run_id = f"TC-{number:04d}"
            run_dir = folder / run_id
            try:
                run_dir.mkdir(parents=True)
            except FileExistsError:
                continue
            counter_path.write_text(f"{number}\n", encoding="utf-8")
            return run_id, run_dir


def serial_forms(serial: str) -> set[str]:
    """The same unit can show the Huawei serial as letters or as hex."""
    text = (serial or "").upper().strip()
    forms = {text} if text else set()
    if re.fullmatch(r"48575443[0-9A-F]{8}", text):
        letters = "".join(chr(int(text[index : index + 2], 16)) for index in range(0, 8, 2))
        forms.add(letters + text[8:])
    tail = text[4:]
    if len(text) >= 12 and text[:4].isalpha() and all(char in "0123456789ABCDEF" for char in tail):
        forms.add("".join(f"{ord(char):02X}" for char in text[:4]) + tail)
    return forms


def order_for_serial(runs_root: Path, serial: str) -> tuple[str, Path] | None:
    """The newest order already issued to this serial. Empty when this unit is new."""
    wanted = serial_forms(serial)
    if not wanted or not runs_root.exists():
        return None
    found: tuple[str, Path] | None = None
    for path in list_runs(runs_root):
        report_path = path / "report.json"
        if not report_path.is_file():
            continue
        try:
            payload = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if _report_serials(payload) & wanted:
            found = (path.name, path)
    return found


def _report_serials(payload: dict) -> set[str]:
    forms: set[str] = set()
    dut = payload.get("dut") if isinstance(payload.get("dut"), dict) else {}
    forms |= serial_forms(str(dut.get("serial") or ""))
    bench_duts = payload.get("bench_duts")
    if isinstance(bench_duts, dict):
        for item in bench_duts.values():
            if isinstance(item, dict):
                forms |= serial_forms(str(item.get("serial") or ""))
    return forms


def resolve_order_for_serial(
    runs_root: Path,
    serial: str,
) -> tuple[str, Path, bool]:
    """Allocate or reuse a TC folder for one DUT serial. Third value: reused."""
    if not serial:
        run_id, run_dir = allocate_run(runs_root)
        return run_id, run_dir, False
    reused = order_for_serial(runs_root, serial)
    if reused is not None:
        try:
            refresh_order(reused[1])
        except OSError:
            reused = None
    if reused is not None:
        return reused[0], reused[1], True
    run_id, run_dir = allocate_run(runs_root)
    return run_id, run_dir, False


def refresh_order(run_dir: Path) -> None:
    """Archive the last certificate, then drop raw evidence for the new measurement."""
    archive_previous_measurement(run_dir)
    raw = run_dir / "raw"
    if raw.is_dir():
        shutil.rmtree(raw)


def archive_previous_measurement(run_dir: Path) -> None:
    """Keep the prior report when the same TC order is measured again."""
    report_path = run_dir / "report.json"
    if not report_path.is_file():
        return
    archive_dir = run_dir / "measurements"
    archive_dir.mkdir(parents=True, exist_ok=True)
    try:
        payload = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        payload = {}
    stamp = str(payload.get("finished_at_utc") or payload.get("started_at_utc") or "").strip()
    safe = re.sub(r"[^\dTZ\-+.]", "_", stamp)[:40] if stamp else str(report_path.stat().st_mtime_ns)
    dest = archive_dir / f"report-{safe}.json"
    if dest.exists():
        dest = archive_dir / f"report-{safe}-{report_path.stat().st_mtime_ns}.json"
    shutil.copy2(report_path, dest)
    for name in ("report.html", "summary.csv", "test-matrix.csv"):
        sidecar = run_dir / name
        if sidecar.is_file():
            shutil.copy2(sidecar, archive_dir / f"{dest.stem}-{name}")
    report_path.unlink(missing_ok=True)
    for name in ("report.html", "summary.csv", "test-matrix.csv"):
        (run_dir / name).unlink(missing_ok=True)


def list_archived_reports(run_dir: Path) -> list[Path]:
    folder = run_dir / "measurements"
    if not folder.is_dir():
        return []
    found = [path for path in folder.glob("report-*.json") if path.is_file()]
    return sorted(found, key=lambda path: path.stat().st_mtime_ns)


def find_run(runs_root: Path, run_id: str) -> Path | None:
    if not runs_root.exists() or RUN_ID_RE.fullmatch(run_id) is None:
        return None
    for path in runs_root.rglob(run_id):
        if path.is_dir() and path.name == run_id:
            return path
    return None


def list_runs(runs_root: Path) -> list[Path]:
    if not runs_root.exists():
        return []
    found = [
        path
        for pattern in ("TC-*", "TCNC-*")
        for path in runs_root.rglob(pattern)
        if path.is_dir() and (_SERVICE_RE.fullmatch(path.name) or _LEGACY_RE.fullmatch(path.name))
    ]
    return sorted(found, key=lambda path: (path.stat().st_mtime_ns, path.name))


def _highest_service_order(runs_root: Path) -> int:
    highest = 0
    if not runs_root.exists():
        return highest
    for path in runs_root.rglob("TC-*"):
        match = _SERVICE_RE.fullmatch(path.name) if path.is_dir() else None
        if match:
            highest = max(highest, int(match.group(1)))
    return highest


def _lock(handle) -> None:
    try:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    except ImportError:
        return
    except OSError:
        return


def session_state_path() -> Path:
    override = os.environ.get("TC_NET_CERT_STATE")
    if override:
        path = Path(override)
    elif os.geteuid() == 0:
        path = Path("/run/tc-net-cert")
    else:
        path = Path.home() / ".local" / "state" / "tc-net-cert"
    path.mkdir(parents=True, exist_ok=True)
    return path / "active-session.json"
