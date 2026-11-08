"""Software self-test. Namespace checks run only as root and do not need a DUT."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

from tc_net_cert.assessment.bottleneck import assess_bottleneck
from tc_net_cert.assessment.verdict import judge_throughput
from tc_net_cert.audit.integrity import seal_run, verify_sums
from tc_net_cert.config.loader import config_hash, load_config, parse_config
from tc_net_cert.core.models import Bottleneck, UsbInfo, Verdict
from tc_net_cert.ethtool.link import parse_ethtool
from tc_net_cert.iperf.parser import parse_iperf_json
from tc_net_cert.namespaces.manager import build_plan
from tc_net_cert.network.management import assert_safe_to_move
from tc_net_cert.reporting.render import render_reports
from tc_net_cert.reporting.sample import sample_report
from tc_net_cert.version import EMTA_SCOPE_EXCLUSIONS

import yaml


def logical_checks() -> list[tuple[str, bool, str]]:
    checks: list[tuple[str, bool, str]] = []
    from tc_net_cert.config.loader import _DEFAULT_FILE

    config, _path = load_config(_DEFAULT_FILE)
    checks.append(("default-config", config.active_profile == "gigabit", config.active_profile))
    digest_a = config_hash(config)
    digest_b = config_hash(parse_config(yaml.safe_load(Path(__file__).resolve().parents[1].joinpath("config/defaults/config.yaml").read_text(encoding="utf-8"))))
    checks.append(("config-hash", digest_a == digest_b and len(digest_a) == 64, digest_a[:12]))
    parsed = parse_ethtool(
        "Speed: 1000Mb/s\nDuplex: Full\nAuto-negotiation: on\nLink detected: yes\n"
    )
    checks.append(("ethtool-parse", parsed["speed_mbps"] == 1000 and parsed["detected"] is True, str(parsed["speed_mbps"])))
    measurement = parse_iperf_json(
        '{"end":{"sum_sent":{"bits_per_second":941000000,"seconds":120,"retransmits":2},"streams":[{"sender":{"max_snd_cwnd":128000}}]}}'
    )
    checks.append(("iperf-json", measurement.ok and measurement.mbps_avg is not None and measurement.mbps_avg > 900, str(measurement.mbps_avg)))
    plan = build_plan(
        [
            ("LAN1", "enp1s0", "74:78:27:81:60:44"),
            ("LAN2", "enp2s0", "c8:4d:44:27:16:8f"),
            ("LAN3", "enp3s0", "00:e0:4c:68:04:4a"),
            ("LAN4", "enp4s0", "00:e0:4c:68:04:8e"),
        ]
    )
    checks.append(
        (
            "namespace-plan",
            [item.namespace for item in plan.endpoints] == ["ns-lan1", "ns-lan2", "ns-lan3", "ns-lan4"],
            ",".join(item.namespace for item in plan.endpoints),
        )
    )
    from tc_net_cert.core.models import InterfaceRecord

    protected = InterfaceRecord(name="eno1", mac="aa:bb:cc:dd:ee:ff", protected=True, protection_reasons=["default route"])
    blocked = False
    try:
        assert_safe_to_move(protected, safe_mode=True)
    except Exception:
        blocked = True
    checks.append(("safe-mode", blocked, "management NIC blocked"))
    profile = config.acceptance()
    ok_verdict, _reasons = judge_throughput(941, profile, Bottleneck.NONE, "meets minimum")
    bad, bad_reasons = judge_throughput(718, profile, Bottleneck.DUT_LIMIT, "DUT LIMIT")
    host, host_reasons = judge_throughput(710, profile, Bottleneck.HOST_LIMIT, "HOST LIMIT")
    checks.append(("throughput-pass", ok_verdict == Verdict.PASS, ok_verdict.value))
    checks.append(("throughput-dut-fail", bad == Verdict.FAIL and any("718" in item for item in bad_reasons), bad.value))
    checks.append(("throughput-host-inconclusive", host == Verdict.INCONCLUSIVE, host.value))
    usb_limit, _note = assess_bottleneck(
        throughput_mbps=280,
        minimum_mbps=900,
        link_mbps=1000,
        expected_link_mbps=1000,
        cpu_percent=20,
        cpu_busy_percent=90,
        usb=UsbInfo(is_usb=True, usb_speed_mbps=480, usb_generation="USB 2.0"),
        nic_max_mbps=1000,
    )
    checks.append(("usb-bottleneck", usb_limit == Bottleneck.USB_LIMIT, usb_limit.value))
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp) / "run"
        folder.mkdir()
        report = sample_report()
        render_reports(report, folder)
        (folder / "raw").mkdir()
        (folder / "raw" / "iperf3").mkdir(parents=True)
        (folder / "raw" / "iperf3" / "sample.json").write_text("{}\n", encoding="utf-8")
        seal_run(folder, {"run_id": report.run_id, "tool_version": report.tool_version, "schema_version": report.schema_version, "config_hash": report.config_hash})
        problems = verify_sums(folder)
        html = (folder / "report.html").read_text(encoding="utf-8")
        checks.append(("report-files", (folder / "report.json").exists() and (folder / "summary.csv").exists(), "written"))
        checks.append(("report-hashes", not problems, "; ".join(problems) or "sha256 ok"))
        checks.append(("report-verdict-text", "FINAL RESULT" in html and "PASS" in html, "html"))
        checks.append(("emta-scope", all(item in EMTA_SCOPE_EXCLUSIONS for item in ("DOCSIS provisioning", "CMTS registration")), "excluded"))
    return checks


def privileged_checks() -> list[tuple[str, bool, str]]:
    if os.geteuid() != 0:
        return [("namespaces", False, "BLOCKED: re-run with sudo to move a veth pair and recover from SIGINT")]
    checks = [_veth_check(), _sigint_check()]
    return checks


def _veth_check() -> tuple[str, bool, str]:
    names = ("ns-tcnc-a", "ns-tcnc-b")
    _cleanup_veth()
    result: tuple[str, bool, str] = ("veth-namespace", False, "not completed")
    try:
        subprocess.run(["ip", "link", "add", "veth-tcnc-a", "type", "veth", "peer", "name", "veth-tcnc-b"], check=True, capture_output=True, text=True)
        for name, iface, address in (
            ("ns-tcnc-a", "veth-tcnc-a", "10.255.255.1/30"),
            ("ns-tcnc-b", "veth-tcnc-b", "10.255.255.2/30"),
        ):
            subprocess.run(["ip", "netns", "add", name], check=True, capture_output=True, text=True)
            subprocess.run(["ip", "link", "set", iface, "netns", name], check=True, capture_output=True, text=True)
            subprocess.run(["ip", "netns", "exec", name, "ip", "addr", "add", address, "dev", iface], check=True, capture_output=True, text=True)
            subprocess.run(["ip", "netns", "exec", name, "ip", "link", "set", iface, "up"], check=True, capture_output=True, text=True)
            subprocess.run(["ip", "netns", "exec", name, "ip", "link", "set", "lo", "up"], check=True, capture_output=True, text=True)
        ping = subprocess.run(
            ["ip", "netns", "exec", "ns-tcnc-a", "ping", "-c", "1", "-W", "2", "10.255.255.2"],
            capture_output=True,
            text=True,
        )
        if ping.returncode != 0:
            result = ("veth-ping", False, ping.stderr.strip() or ping.stdout.strip() or "ping failed")
        else:
            iperf = subprocess.run(["iperf3", "--version"], capture_output=True, text=True)
            if iperf.returncode != 0:
                result = ("veth-iperf", False, "iperf3 is not installed. Install it with: sudo apt-get install -y iperf3")
            else:
                server = subprocess.Popen(
                    ["ip", "netns", "exec", "ns-tcnc-b", "iperf3", "-s", "-B", "10.255.255.2", "-p", "5201", "-1"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                try:
                    client = subprocess.run(
                        ["ip", "netns", "exec", "ns-tcnc-a", "iperf3", "-c", "10.255.255.2", "-B", "10.255.255.1", "-t", "1", "-J"],
                        capture_output=True,
                        text=True,
                        timeout=15,
                    )
                finally:
                    server.kill()
                parsed = parse_iperf_json(client.stdout)
                ok = client.returncode == 0 and parsed.ok
                detail = f"iperf {parsed.mbps_avg:.1f} Mbps" if parsed.mbps_avg is not None else parsed.error or client.stderr
                result = ("veth-iperf", ok, detail)
    except subprocess.CalledProcessError as exc:
        result = ("veth-namespace", False, (exc.stderr or exc.stdout or str(exc)).strip())
    finally:
        _cleanup_veth()
        leftover = subprocess.run(["ip", "netns", "list"], capture_output=True, text=True)
        if any(name in leftover.stdout for name in names):
            result = ("veth-cleanup", False, leftover.stdout.strip())
    return result


def _sigint_check() -> tuple[str, bool, str]:
    script = """
import signal, subprocess, sys, time
name = "ns-tcnc-sigint"
subprocess.run(["ip", "netns", "del", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
subprocess.run(["ip", "netns", "add", name], check=True)
def cleanup(signum, frame):
    subprocess.run(["ip", "netns", "del", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    raise SystemExit(0)
signal.signal(signal.SIGINT, cleanup)
print("READY", flush=True)
time.sleep(30)
"""
    proc = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True)
    assert proc.stdout is not None
    line = proc.stdout.readline()
    if "READY" not in line:
        proc.kill()
        return ("sigint-cleanup", False, "helper did not become ready")
    proc.send_signal(signal.SIGINT)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        return ("sigint-cleanup", False, "helper ignored SIGINT")
    listed = subprocess.run(["ip", "netns", "list"], capture_output=True, text=True)
    if "ns-tcnc-sigint" in listed.stdout:
        subprocess.run(["ip", "netns", "del", "ns-tcnc-sigint"], check=False)
        return ("sigint-cleanup", False, "namespace survived SIGINT")
    return ("sigint-cleanup", True, "namespace removed")


def _cleanup_veth() -> None:
    for name in ("ns-tcnc-a", "ns-tcnc-b", "ns-tcnc-sigint"):
        subprocess.run(["ip", "netns", "del", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["ip", "link", "del", "veth-tcnc-a"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def all_passed(checks: list[tuple[str, bool, str]]) -> bool:
    return all(ok for _name, ok, _detail in checks)
