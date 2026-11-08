"""Say the exact reason a measurement stopped."""

from __future__ import annotations

import re

from tc_net_cert.core.models import LinkFacts

_PIPE_BYTES = 65536
_END_RE = re.compile(r'"end"\s*:\s*([0-9]+(?:\.[0-9]+)?)')


def port_down(link: LinkFacts) -> bool:
    state = (link.operstate or "").lower()
    return state in {"down", "lowerlayerdown"}


def last_interval_second(payload: str) -> float | None:
    found = _END_RE.findall(payload or "")
    if not found:
        return None
    try:
        return float(found[-1])
    except ValueError:
        return None


def explain_cut(
    source: str,
    destination: str,
    *,
    source_down: bool,
    destination_down: bool,
    source_flaps: int,
    destination_flaps: int,
    timed_out: bool,
    measured: bool,
    seconds: float | None,
    requested: int,
    exit_code: int | None = None,
    output_bytes: int = 0,
    timeout_s: float | None = None,
    iperf_error: str = "",
) -> str:
    if measured and not timed_out:
        return ""
    pair = f"{source} → {destination}"
    if "UNIT_POWER_OFF_OR_REBOOT" in iperf_error:
        return (
            f"UNIT_POWER_OFF_OR_REBOOT: la unidad se apagó o reinició durante {pair}. "
            "El carrier cayó y la medición se detuvo."
        )
    if "BIND_ERROR" in iperf_error or "Cannot assign requested address" in iperf_error:
        return (
            f"BIND_ERROR: Cannot assign requested address en {pair}. "
            "iperf3 no abrió el socket y no hubo transmisión."
        )
    if "FAIL_NO_TRAFFIC" in iperf_error:
        return (
            f"FAIL_NO_TRAFFIC: {pair} no movió tráfico real. "
            "La velocidad se quedó en 0 Mbps y el indicador se apagó."
        )
    down = [name for name, flag in ((source, source_down), (destination, destination_down)) if flag]
    bounced = [name for name, flaps in ((source, source_flaps), (destination, destination_flaps)) if flaps]
    if len(down) >= 2:
        return (
            f"La unidad reinició o apagó los puertos: {down[0]} y {down[1]} "
            f"quedaron sin enlace al mismo tiempo. Por eso no hubo medición en {pair}."
        )
    if len(down) == 1:
        return (
            f"Se cortó {down[0]}: su carrier pasó a down durante la medición. "
            f"Por eso no hubo medición en {pair}."
        )
    if len(bounced) >= 2 and not down:
        return (
            f"La unidad reinició: {bounced[0]} y {bounced[1]} cayeron y volvieron "
            f"durante la medición. Por eso no hubo medición en {pair}."
        )
    if len(bounced) == 1:
        return (
            f"Se cortó {bounced[0]} y volvió: el carrier cayó y regresó durante la medición. "
            f"Por eso no se completó {pair}."
        )
    error = _iperf_cause(iperf_error)
    if error:
        return f"iperf3 rechazó {pair}: {error}. Los dos enlaces siguieron arriba."
    last = seconds if seconds is not None else None
    stopped = f"{timeout_s:.0f}" if timeout_s else str(requested + 45)
    if timed_out and output_bytes == _PIPE_BYTES and (last is None or last + 5 < requested):
        seen = f"{last:.0f}" if last is not None else "el arranque"
        return (
            f"iperf3 quedó bloqueado escribiendo el JSON de {pair}. "
            f"La salida llenó el tubo de {_PIPE_BYTES} bytes en el segundo {seen} de {requested} "
            f"y el proceso no pudo seguir. El temporizador lo detuvo a los {stopped} s. "
            f"Los dos enlaces siguieron arriba: no fue corte de cable ni reinicio."
        )
    if timed_out or exit_code == 124:
        seen = f"el segundo {last:.0f}" if last is not None else "sin ningún intervalo"
        return (
            f"El temporizador detuvo iperf3 de {pair} a los {stopped} s porque el proceso no había salido. "
            f"El JSON alcanzó {seen} de {requested} s ({output_bytes} bytes). "
            f"Los dos enlaces siguieron arriba: no fue corte de cable ni reinicio."
        )
    return (
        f"iperf3 no entregó resultado de {pair} (salida {output_bytes} bytes). "
        f"Los dos enlaces siguieron arriba: no fue corte de cable ni reinicio."
    )


def _iperf_cause(text: str) -> str:
    for line in (text or "").splitlines():
        cleaned = " ".join(line.split())
        if not cleaned or cleaned == "TIMEOUT":
            continue
        lower = cleaned.lower()
        if "expecting" in lower or "did not return json" in lower or "invalid iperf3" in lower:
            continue
        if any(word in lower for word in ("unable", "busy", "refused", "error", "reset", "timeout")):
            return cleaned[:180]
    return ""
