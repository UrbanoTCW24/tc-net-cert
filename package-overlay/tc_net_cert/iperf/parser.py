"""Parse iperf3 --json. Console text is never the measurement source."""

from __future__ import annotations

import json
from dataclasses import dataclass


@dataclass
class IperfMeasurement:
    ok: bool
    error: str
    seconds: float | None
    mbps_avg: float | None
    mbps_min: float | None
    mbps_max: float | None
    retransmits: int | None
    max_snd_cwnd: int | None
    cpu_host: float | None
    reverse_mbps: float | None = None


def parse_iperf_json(payload: str) -> IperfMeasurement:
    text = payload.strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        return _fail("iperf3 did not return JSON")
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        return _fail(f"invalid iperf3 JSON: {exc}")
    if not isinstance(data, dict):
        return _fail("iperf3 JSON root is not an object")
    error = str(data.get("error") or "")
    signal = "terminated by signal" in error.lower()
    end_block = data.get("end") or {}
    summary = end_block.get("sum_sent") or end_block.get("sum") or {}
    if error and not signal:
        return _fail(error)
    if not summary:
        return _fail(error or "iperf3 JSON has no end.sum_sent")
    rows, trimmed = _active_intervals(data.get("intervals") or [])
    intervals = [row["mbps"] for row in rows]
    good_seconds = sum(row["seconds"] for row in rows)
    good_bytes = sum(row["bytes"] for row in rows if row["bytes"] is not None)
    streams = end_block.get("streams") or []
    cwnds = []
    for stream in streams:
        sender = stream.get("sender") or {}
        if sender.get("max_snd_cwnd") is not None:
            cwnds.append(int(sender["max_snd_cwnd"]))
    cpu = (end_block.get("cpu_utilization_percent") or {}).get("host_total")
    reverse = end_block.get("sum_sent_bidir_reverse") or {}
    reverse_mbps = None
    if reverse.get("bits_per_second") is not None:
        reverse_mbps = float(reverse["bits_per_second"]) / 1_000_000
    bps = summary.get("bits_per_second")
    reported = float(summary["seconds"]) if summary.get("seconds") is not None else None
    mbps = float(bps) / 1_000_000 if bps is not None else None
    transferred = summary.get("bytes")
    # A cancel or the watchdog writes 0 Mbps after the stop. That tail is not the rate.
    if rows and (trimmed or signal) and good_seconds >= 1:
        weighed = sum(row["mbps"] * row["seconds"] for row in rows) / good_seconds
        if good_bytes > 0:
            mbps = (good_bytes * 8) / good_seconds / 1_000_000
        else:
            mbps = weighed
        reported = good_seconds
    elif (
        isinstance(transferred, (int, float))
        and transferred > 0
        and good_seconds > 0
        and reported is not None
        and reported > good_seconds + 2
    ):
        mbps = (float(transferred) * 8) / good_seconds / 1_000_000
        reported = good_seconds
    elif _blank(mbps, reported):
        stream_bytes, stream_seconds = _stream_bytes(end_block)
        if good_bytes > 0 and good_seconds >= 1:
            mbps = (good_bytes * 8) / good_seconds / 1_000_000
            reported = good_seconds
        elif stream_bytes > 0 and stream_seconds >= 1:
            mbps = (stream_bytes * 8) / stream_seconds / 1_000_000
            reported = stream_seconds
    if _blank(mbps, reported):
        return _fail(error or "iperf3 transferred 0 bytes")
    return IperfMeasurement(
        ok=True,
        error="",
        seconds=reported,
        mbps_avg=mbps,
        mbps_min=min(intervals) if intervals else (float(bps) / 1_000_000 if bps is not None else None),
        mbps_max=max(intervals) if intervals else (float(bps) / 1_000_000 if bps is not None else None),
        retransmits=int(summary["retransmits"]) if summary.get("retransmits") is not None else None,
        max_snd_cwnd=max(cwnds) if cwnds else None,
        cpu_host=float(cpu) if cpu is not None else None,
        reverse_mbps=reverse_mbps,
    )


def _active_intervals(raw: list) -> tuple[list[dict[str, float | None]], bool]:
    """Keep intervals that carried traffic. Drop the 0 Mbps tail from a close or a kill."""
    rows: list[dict[str, float | None]] = []
    for interval in raw:
        if not isinstance(interval, dict):
            continue
        block = interval.get("sum") or interval.get("sum_sent") or {}
        seconds = block.get("seconds")
        bps = block.get("bits_per_second")
        if bps is None or not isinstance(seconds, (int, float)) or float(seconds) < 0.5:
            continue
        if float(seconds) >= 5 and float(bps) == 0:
            continue
        transferred = block.get("bytes")
        rows.append(
            {
                "seconds": float(seconds),
                "mbps": float(bps) / 1_000_000,
                "bytes": float(transferred) if isinstance(transferred, (int, float)) else None,
            }
        )
    trimmed = False
    while rows and (rows[-1]["mbps"] or 0) <= 0:
        rows.pop()
        trimmed = True
    return rows, trimmed


def _blank(mbps: float | None, seconds: float | None) -> bool:
    if mbps is None or mbps <= 0:
        return True
    return seconds is None or seconds < 1


def _stream_bytes(end_block: dict) -> tuple[float, float]:
    total = 0.0
    longest = 0.0
    for stream in end_block.get("streams") or []:
        sender = stream.get("sender") or stream.get("receiver") or {}
        seconds = sender.get("seconds")
        transferred = sender.get("bytes")
        if not isinstance(transferred, (int, float)) or not isinstance(seconds, (int, float)):
            continue
        if transferred <= 0 or seconds < 0.5:
            continue
        total += float(transferred)
        longest = max(longest, float(seconds))
    return total, longest


def _fail(message: str) -> IperfMeasurement:
    return IperfMeasurement(
        ok=False,
        error=message,
        seconds=None,
        mbps_avg=None,
        mbps_min=None,
        mbps_max=None,
        retransmits=None,
        max_snd_cwnd=None,
        cpu_host=None,
    )
