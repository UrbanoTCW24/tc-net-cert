"""Load, validate, hash, and save station configuration.

Thresholds stay in YAML. The program refuses a profile whose speed profile
and acceptance profile disagree, so a run cannot mix two criteria silently.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

import yaml

from tc_net_cert.core.exceptions import ConfigError
from tc_net_cert.core.models import (
    AcceptanceProfile,
    AppConfig,
    BenchDefinition,
    DhcpSettings,
    DutIdentity,
    DutProfile,
    IperfSettings,
    PingSettings,
    PortConfig,
    SpeedProfile,
)
from tc_net_cert.version import CERTIFICATION_TITLE, SCHEMA_VERSION

_MAC_RE = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")
_DEFAULT_FILE = Path(__file__).resolve().parent / "defaults" / "config.yaml"
_ALLOWED_DHCP = {"internal", "dhcpcd", "dhclient"}
_ALLOWED_MODES = {"dhcp", "static"}
_ALLOWED_FLAP = {"FAIL", "WARNING"}


def normalize_mac(value: str) -> str:
    text = value.strip().lower().replace("-", ":")
    if not _MAC_RE.match(text):
        raise ConfigError(f"Invalid MAC address: {value}")
    return text


def default_config_path() -> Path:
    env = os.environ.get("TC_NET_CERT_CONFIG")
    if env:
        return Path(env)
    if os.geteuid() == 0:
        system = Path("/etc/tc-net-cert/config.yaml")
        if system.exists():
            return system
    user = Path.home() / ".config" / "tc-net-cert" / "config.yaml"
    if user.exists():
        return user
    if os.geteuid() == 0:
        return Path("/etc/tc-net-cert/config.yaml")
    return user


def resolve_config_path(explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit
    env = os.environ.get("TC_NET_CERT_CONFIG")
    if env:
        return Path(env)
    system = Path("/etc/tc-net-cert/config.yaml")
    user = Path.home() / ".config" / "tc-net-cert" / "config.yaml"
    cwd = Path.cwd() / "tc-net-cert.yaml"
    if cwd.exists():
        return cwd
    if system.exists():
        return system
    if user.exists():
        return user
    return _DEFAULT_FILE


def load_config(path: Path | None = None) -> tuple[AppConfig, Path]:
    config_path = resolve_config_path(path)
    if not config_path.exists():
        raise ConfigError(f"Configuration file not found: {config_path}")
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ConfigError("Configuration root must be a mapping")
    return parse_config(raw), config_path


def parse_config(raw: dict) -> AppConfig:
    schema = str(raw.get("schema_version") or SCHEMA_VERSION)
    network = raw.get("network") or {}
    if not isinstance(network, dict):
        raise ConfigError("network must be a mapping")
    mode = str(network.get("mode") or raw.get("network_mode") or "dhcp").lower()
    if mode not in _ALLOWED_MODES:
        raise ConfigError("network.mode must be 'dhcp' or 'static'")
    management_mac = ""
    raw_mgmt = network.get("management_mac") or raw.get("management_mac") or ""
    if str(raw_mgmt).strip():
        management_mac = normalize_mac(str(raw_mgmt))

    ports = _parse_ports(raw.get("ports") or {})
    static_addresses = _parse_cidr_map(network.get("static_addresses") or {})
    static_gateways = _parse_ip_map(network.get("static_gateways") or {})
    _reject_duplicate_values(static_addresses, "static address")

    speed_profiles = {
        str(name): SpeedProfile.model_validate(value)
        for name, value in (raw.get("speed_profiles") or {}).items()
    }
    profiles = {
        str(name): AcceptanceProfile.model_validate(value)
        for name, value in (raw.get("profiles") or {}).items()
    }
    dut_profiles = {
        str(name): DutProfile.model_validate(value)
        for name, value in (raw.get("dut_profiles") or {}).items()
    }
    if not profiles:
        raise ConfigError("At least one acceptance profile is required")
    active = str(raw.get("active_profile") or "gigabit")
    if active not in profiles:
        raise ConfigError(f"active_profile '{active}' is not defined")
    speed_name = str(raw.get("active_speed_profile") or "1G")
    if speed_name not in speed_profiles:
        raise ConfigError(f"active_speed_profile '{speed_name}' is not defined")
    speed = speed_profiles[speed_name]
    acceptance = profiles[active]
    if speed.expected_speed_mbps != acceptance.expected_link_speed:
        raise ConfigError(
            f"Profile '{active}' expects link {acceptance.expected_link_speed} Mbps "
            f"but speed profile '{speed_name}' expects {speed.expected_speed_mbps} Mbps"
        )
    if speed.minimum_throughput_mbps != acceptance.minimum_throughput_mbps:
        raise ConfigError(
            f"Profile '{active}' minimum throughput does not match speed profile '{speed_name}'"
        )
    if acceptance.link_flap_result not in _ALLOWED_FLAP:
        raise ConfigError("link_flap_result must be FAIL or WARNING")

    dut_raw = raw.get("dut") or {}
    dut = DutIdentity.model_validate(dut_raw)
    if dut.type not in dut_profiles:
        raise ConfigError(f"Unknown DUT profile '{dut.type}'")
    if dut.mac:
        dut.mac = normalize_mac(dut.mac)

    dhcp = DhcpSettings.model_validate(raw.get("dhcp") or {})
    if dhcp.backend not in _ALLOWED_DHCP:
        raise ConfigError("dhcp.backend must be internal, dhcpcd, or dhclient")
    iperf = IperfSettings.model_validate(raw.get("iperf") or {})
    if iperf.duration_seconds not in iperf.allowed_durations:
        raise ConfigError(
            f"iperf.duration_seconds must be one of {iperf.allowed_durations}"
        )
    if iperf.streams not in iperf.allowed_streams:
        raise ConfigError(f"iperf.streams must be one of {iperf.allowed_streams}")
    ping = PingSettings.model_validate(raw.get("ping") or {})

    port_names = list(ports.keys())
    pairs = _parse_pairs(raw.get("simultaneous_pairs") or [])
    for left, right in pairs:
        if left == right:
            raise ConfigError(f"Simultaneous pair cannot use the same port twice: {left}")
    pairs = [(left, right) for left, right in pairs if left in ports and right in ports]
    if not pairs and len(port_names) >= 2:
        pairs = default_simultaneous_pairs(port_names)
    benches = _parse_benches(raw.get("benches"), port_names)

    return AppConfig(
        schema_version=schema,
        safe_mode=bool(raw.get("safe_mode", True)),
        operator=str(raw.get("operator") or ""),
        station=str(raw.get("station") or ""),
        runs_dir=str(raw.get("runs_dir") or ""),
        report_title=str(raw.get("report_title") or CERTIFICATION_TITLE),
        active_profile=active,
        active_speed_profile=speed_name,
        ports=ports,
        network_mode=mode,
        management_mac=management_mac,
        static_addresses=static_addresses,
        static_gateways=static_gateways,
        simultaneous_pairs=[[left, right] for left, right in pairs],
        speed_profiles=speed_profiles,
        profiles=profiles,
        dut_profiles=dut_profiles,
        dhcp=dhcp,
        iperf=iperf,
        ping=ping,
        dut=dut,
        benches=benches,
        long_duration_seconds=int(raw.get("long_duration_seconds") or 300),
        require_profile_port_count=_require_profile_port_count(raw, len(ports)),
    )


def config_hash(config: AppConfig) -> str:
    payload = config.model_dump(mode="json")
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def save_config(config: AppConfig, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    document = config_to_document(config)
    path.write_text(yaml.safe_dump(document, sort_keys=False, allow_unicode=True), encoding="utf-8")


def config_to_document(config: AppConfig) -> dict:
    return {
        "schema_version": config.schema_version,
        "safe_mode": config.safe_mode,
        "operator": config.operator,
        "station": config.station,
        "runs_dir": config.runs_dir,
        "report_title": config.report_title,
        "active_profile": config.active_profile,
        "active_speed_profile": config.active_speed_profile,
        "network": {
            "mode": config.network_mode,
            "management_mac": config.management_mac,
            "static_addresses": config.static_addresses,
            "static_gateways": config.static_gateways,
        },
        "ports": {name: {"mac": port.mac} for name, port in config.ports.items()},
        "simultaneous_pairs": config.simultaneous_pairs,
        "speed_profiles": {
            name: profile.model_dump(mode="json") for name, profile in config.speed_profiles.items()
        },
        "profiles": {
            name: profile.model_dump(mode="json") for name, profile in config.profiles.items()
        },
        "dut_profiles": {
            name: profile.model_dump(mode="json") for name, profile in config.dut_profiles.items()
        },
        "dhcp": config.dhcp.model_dump(mode="json"),
        "iperf": config.iperf.model_dump(mode="json"),
        "ping": config.ping.model_dump(mode="json"),
        "dut": config.dut.model_dump(mode="json"),
        "benches": {
            key: bench.model_dump(mode="json") for key, bench in config.benches.items()
        },
        "long_duration_seconds": config.long_duration_seconds,
        "require_profile_port_count": config.require_profile_port_count,
    }


def runs_directory(config: AppConfig) -> Path:
    if config.runs_dir:
        return Path(config.runs_dir).expanduser()
    if os.geteuid() == 0:
        return Path("/var/lib/tc-net-cert/runs")
    return Path.home() / ".local" / "share" / "tc-net-cert" / "runs"


def _parse_ports(raw: object) -> dict[str, PortConfig]:
    if not isinstance(raw, dict):
        raise ConfigError("ports must be a mapping")
    ports: dict[str, PortConfig] = {}
    seen: dict[str, str] = {}
    for name, value in raw.items():
        logical = str(name).upper()
        if not re.fullmatch(r"LAN[1-9][0-9]?", logical):
            raise ConfigError(f"Port name must look like LAN1..LAN9, got {name}")
        if isinstance(value, str):
            mac = normalize_mac(value)
        elif isinstance(value, dict) and "mac" in value:
            mac = normalize_mac(str(value["mac"]))
        else:
            raise ConfigError(f"Port {name} needs a mac")
        if mac in seen:
            raise ConfigError(f"Duplicate MAC {mac} on {seen[mac]} and {logical}")
        seen[mac] = logical
        ports[logical] = PortConfig(mac=mac)
    return ports


def _parse_cidr_map(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise ConfigError("static_addresses must be a mapping")
    import ipaddress

    result: dict[str, str] = {}
    for name, value in raw.items():
        logical = str(name).upper()
        try:
            network = ipaddress.ip_interface(str(value))
        except ValueError as exc:
            raise ConfigError(f"Invalid static address for {logical}: {value}") from exc
        if not isinstance(network, ipaddress.IPv4Interface):
            raise ConfigError(f"{logical} must use an IPv4 CIDR address")
        result[logical] = f"{network.ip}/{network.network.prefixlen}"
    return result


def _parse_ip_map(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise ConfigError("static_gateways must be a mapping")
    import ipaddress

    result: dict[str, str] = {}
    for name, value in raw.items():
        logical = str(name).upper()
        try:
            ip = ipaddress.ip_address(str(value))
        except ValueError as exc:
            raise ConfigError(f"Invalid gateway for {logical}: {value}") from exc
        result[logical] = str(ip)
    return result


def _require_profile_port_count(raw: dict, port_count: int) -> bool:
    """Three-NIC lab PCs are supported without a special CLI flag."""
    if port_count < 4:
        return False
    return bool(raw.get("require_profile_port_count", True))


def _parse_benches(raw: object, port_names: list[str]) -> dict[str, BenchDefinition]:
    if not isinstance(raw, dict) or not raw:
        return {}
    benches: dict[str, BenchDefinition] = {}
    for key, value in raw.items():
        bench_id = str(key).upper()
        if not isinstance(value, dict):
            continue
        ports = [str(item).upper() for item in (value.get("ports") or [])]
        benches[bench_id] = BenchDefinition(
            id=bench_id,
            label=str(value.get("label") or f"Banco {bench_id}"),
            ports=ports,
        )
    return benches


def default_simultaneous_pairs(port_names: list[str]) -> list[tuple[str, str]]:
    """Build simultaneous pairs from the LAN names that exist on this station."""
    names = [str(name).upper() for name in port_names]
    if len(names) >= 8:
        return [
            (names[0], names[2]),
            (names[1], names[3]),
            (names[4], names[6]),
            (names[5], names[7]),
        ]
    if len(names) >= 4:
        return [(names[0], names[2]), (names[1], names[3])]
    if len(names) == 3:
        return [(names[0], names[2]), (names[1], names[2])]
    if len(names) == 2:
        return [(names[0], names[1])]
    return []


def _parse_pairs(raw: object) -> list[tuple[str, str]]:
    if not isinstance(raw, list):
        raise ConfigError("simultaneous_pairs must be a list")
    pairs: list[tuple[str, str]] = []
    for item in raw:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise ConfigError("Each simultaneous pair must be [SOURCE, DESTINATION]")
        pairs.append((str(item[0]).upper(), str(item[1]).upper()))
    return pairs


def _reject_duplicate_values(values: dict[str, str], label: str) -> None:
    seen: dict[str, str] = {}
    for name, value in values.items():
        host = value.split("/", 1)[0]
        if host in seen:
            raise ConfigError(f"Duplicate {label} {host} on {seen[host]} and {name}")
        seen[host] = name
