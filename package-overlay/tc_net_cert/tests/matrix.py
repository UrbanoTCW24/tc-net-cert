"""Visual cross-port matrix. Every cell carries Mbps and a PASS/FAIL text verdict."""

from __future__ import annotations

from tc_net_cert.core.models import FlowResult, MatrixCell, Verdict


def build_matrix(names: list[str], flows: list[FlowResult]) -> dict[str, dict[str, MatrixCell]]:
    ordered = list(names)
    matrix = {
        src: {
            dst: MatrixCell(verdict=Verdict.SKIPPED if src == dst else Verdict.NOT_RUN, note="--" if src == dst else "")
            for dst in ordered
        }
        for src in ordered
    }
    for flow in flows:
        if flow.kind != "throughput":
            continue
        if flow.source not in matrix or flow.destination not in matrix[flow.source]:
            continue
        matrix[flow.source][flow.destination] = MatrixCell(
            mbps=flow.mbps_avg,
            verdict=flow.verdict,
            retransmits=flow.retransmits,
            note=flow.bottleneck.value if flow.bottleneck.value != "NONE" else "",
        )
    return matrix


def matrix_text(matrix: dict[str, dict[str, MatrixCell]]) -> str:
    names = list(matrix)
    header = "        " + "".join(f"{name:>16}" for name in names)
    lines = [header]
    for src in names:
        cells = []
        for dst in names:
            cell = matrix[src][dst]
            if src == dst:
                cells.append(f"{'--':>16}")
            elif cell.mbps is None:
                cells.append(f"{cell.verdict.value:>16}")
            else:
                cells.append(f"{cell.mbps:.0f} {cell.verdict.value:>10}")
        lines.append(f"{src:<8}" + "".join(cells))
    return "\n".join(lines)
