"""Logical bench groups (Banco A / B) over LAN1..LAN8."""

from __future__ import annotations

from tc_net_cert.core.models import AppConfig, BenchDefinition

DEFAULT_BENCHES: dict[str, BenchDefinition] = {
    "A": BenchDefinition(id="A", label="Banco A", ports=["LAN1", "LAN2", "LAN3", "LAN4"]),
    "B": BenchDefinition(id="B", label="Banco B", ports=["LAN5", "LAN6", "LAN7", "LAN8"]),
}


def resolve_benches(config: AppConfig) -> dict[str, BenchDefinition]:
    if config.benches:
        return dict(config.benches)
    names = sorted(config.ports.keys())
    if len(names) > 4:
        return dict(DEFAULT_BENCHES)
    return {
        "A": BenchDefinition(
            id="A",
            label="Banco A",
            ports=names if names else list(DEFAULT_BENCHES["A"].ports),
        )
    }


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
