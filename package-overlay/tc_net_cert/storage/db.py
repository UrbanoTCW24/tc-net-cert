"""Local index of certification runs. No network service is involved."""

from __future__ import annotations

import sqlite3
from pathlib import Path


def index_path(runs_root: Path) -> Path:
    return runs_root / "index.sqlite"


def connect(runs_root: Path) -> sqlite3.Connection:
    runs_root.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(index_path(runs_root))
    connection.row_factory = sqlite3.Row
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS runs (
            run_id TEXT PRIMARY KEY,
            started_at TEXT,
            finished_at TEXT,
            verdict TEXT,
            dut_type TEXT,
            dut_model TEXT,
            dut_serial TEXT,
            station TEXT,
            operator TEXT,
            path TEXT
        )
        """
    )
    connection.commit()
    return connection


def upsert_run(
    runs_root: Path,
    *,
    run_id: str,
    started_at: str,
    finished_at: str,
    verdict: str,
    dut_type: str,
    dut_model: str,
    dut_serial: str,
    station: str,
    operator: str,
    path: str,
) -> None:
    connection = connect(runs_root)
    try:
        connection.execute(
            """
            INSERT INTO runs (
                run_id, started_at, finished_at, verdict, dut_type, dut_model,
                dut_serial, station, operator, path
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET
                finished_at=excluded.finished_at,
                verdict=excluded.verdict,
                path=excluded.path
            """,
            (run_id, started_at, finished_at, verdict, dut_type, dut_model, dut_serial, station, operator, path),
        )
        connection.commit()
    finally:
        connection.close()


def latest_runs(runs_root: Path, limit: int = 20) -> list[dict]:
    if not index_path(runs_root).exists():
        return []
    connection = connect(runs_root)
    try:
        rows = connection.execute(
            "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()
