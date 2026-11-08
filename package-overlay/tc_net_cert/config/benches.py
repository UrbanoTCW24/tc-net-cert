"""Logical bench groups (Banco A / B) over LAN1..LAN8."""

from __future__ import annotations

from tc_net_cert.core.models import AppConfig, BenchDefinition

DEFAULT_BENCHES: dict[str, BenchDefinition] = {
    "A": BenchDefinition(id="A", label="Banco A", ports=["LAN1", "LAN2", "LAN3", "LAN4"]),
    "B": BenchDefinition(id="B", label="Banco B", ports=["LAN5", "LAN6", "LAN7", "LAN8"]),
}


def resolve_benches(config: AppConfig) -> dict[str, BenchDefinition]:
    if config.benches:
        merged: dict[str, BenchDefinition] = {}
        for key, bench in config.benches.items():
            bench_id = str(key).upper()
            ports = list(bench.ports) if bench.ports else list(DEFAULT_BENCHES.get(bench_id, bench).ports)
            merged[bench_id] = BenchDefinition(id=bench_id, label=bench.label, ports=ports)
        for bench_id, default in DEFAULT_BENCHES.items():
            merged.setdefault(bench_id, default)
        return merged
    names = sorted(config.ports.keys())
    if len(names) > 4:
        return dict(DEFAULT_BENCHES)
    return dict(DEFAULT_BENCHES)


def live_logical_ports(config: AppConfig) -> list[str]:
    """LAN1..LAN8 ordenados para medición en vivo (incluye slots sin NIC)."""
    order: list[str] = []
    for bench_id in ("A", "B"):
        bench = resolve_benches(config).get(bench_id)
        if not bench:
            continue
        for name in bench.ports:
            if name not in order:
                order.append(name)
    if order:
        return order
    return sorted(config.ports.keys(), key=lambda item: int(item.replace("LAN", "") or "0"))


def live_ports_mac_map(config: AppConfig) -> dict[str, str]:
    mapped = {name: port.mac.lower() for name, port in config.ports.items()}
    for name in live_logical_ports(config):
        mapped.setdefault(name, "")
    return mapped


def bench_ports(config: AppConfig, bench_id: str) -> dict[str, str]:
    benches = resolve_benches(config)
    bench = benches.get(bench_id.upper())
    if bench is None:
        return {}
    return {name: config.ports[name].mac for name in bench.ports if name in config.ports}


def bench_port_list(config: AppConfig, bench_id: str) -> list[str]:
    benches = resolve_benches(config)
    bench = benches.get(bench_id.upper())
    if bench is None:
        return []
    return [name for name in bench.ports if name in config.ports]


def filter_simultaneous_pairs(
    pairs: list[list[str]],
    port_names: set[str],
) -> list[list[str]]:
    """Keep only pairs whose endpoints are in the active port set (per-bench cert)."""
    if not port_names:
        return []
    known = {str(name).upper() for name in port_names}
    filtered: list[list[str]] = []
    for pair in pairs:
        if len(pair) != 2:
            continue
        left, right = str(pair[0]).upper(), str(pair[1]).upper()
        if left in known and right in known:
            filtered.append([left, right])
    return filtered


def scope_config_ports(config: AppConfig, ports: dict) -> AppConfig:
    """Shrink YAML-derived fields when certifying one bench (LAN1-4 or LAN5-8)."""
    scoped = config.model_copy(deep=True)
    scoped.ports = ports
    scoped.simultaneous_pairs = filter_simultaneous_pairs(config.simultaneous_pairs, set(ports.keys()))
    return scoped
