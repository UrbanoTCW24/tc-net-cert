"""Certificate PDF built in memory. The sealed run folder is not rewritten."""

from __future__ import annotations

from tc_net_cert.core.models import CertificationReport
from tc_net_cert.reporting.certificate import certificate_sheet

_PAGE_W = 595
_PAGE_H = 842
_LEFT = 36
_RIGHT = 559
_GREEN = (0.086, 0.639, 0.290)
_RED = (0.725, 0.110, 0.110)
_INK = (0.102, 0.102, 0.102)
_MUTED = (0.361, 0.420, 0.478)
_PASS = {"CUMPLE", "PASS", "OK", "SÍ", "SI", "OK!"}
_FAIL = {"NO CUMPLE", "FAIL", "NO", "NO!", "OBSERVACIÓN", "OBSERVACION"}


def certificate_pdf(report: CertificationReport) -> bytes:
    """The same face as the certificate, as a PDF. The Wi-Fi key read from the portal is printed."""
    sheet = certificate_sheet(report)
    canvas = _Canvas()
    if sheet["ont"]:
        _ont(canvas, report, sheet)
    elif sheet["emta"]:
        _emta(canvas, report, sheet)
    else:
        _matrix(canvas, report, sheet)
    return canvas.bytes()


def _ont(canvas: _Canvas, report: CertificationReport, sheet: dict) -> None:
    _masthead(canvas, "REPORTE TÉCNICO DE CERTIFICACIÓN CPE")
    _identity(canvas, sheet, serial=False)
    _verdict(canvas, "RESULTADO DE CERTIFICACIÓN", sheet["headline_cpe"], sheet)
    _table(
        canvas,
        ("SECCIÓN", "PARÁMETRO", "MEDIDO", "REFERENCIA", "CUMPLIMIENTO"),
        (78, 130, 110, 130, 75),
        [
            (row["section"], row["name"], row["read"], row["expected"], row["state"])
            for row in sheet["cpe_rows"]
        ],
    )
    _note(canvas, report.disclaimer)
    canvas.page()
    _masthead(canvas, f"ORDEN: {sheet['order']} · Hoja 2")
    canvas.gap(8)
    canvas.text("DETALLE DE THROUGHPUT", 11, bold=True)
    canvas.gap(8)
    _table(
        canvas,
        ("PRUEBA", "MEDIDO", "REFERENCIA", "CUMPLIMIENTO"),
        (180, 120, 140, 83),
        [
            (row["name"], row["read"], row["reference"], row["state"])
            for row in sheet["throughput_rows"]
        ],
    )
    if sheet.get("wan_rows"):
        canvas.gap(10)
        canvas.text("SERVICIOS WAN", 11, bold=True)
        canvas.gap(8)
        _table(
            canvas,
            ("NOMBRE", "SERVICIO", "ESTADO", "VLAN", "IP"),
            (80, 120, 90, 60, 173),
            [
                (row["name"], row["service"], row["status"], row["vlan"], row["address"])
                for row in sheet["wan_rows"]
            ],
        )
    _note(canvas, "Documento generado por TECHCORP LAB. www.techcorpca.com")


def _emta(canvas: _Canvas, report: CertificationReport, sheet: dict) -> None:
    pages = int(sheet.get("emta_page_count") or 3)
    title = "REPORTE TECNICO DE CERTIFICACION CPE — EMTA / DOCSIS"
    _masthead(canvas, title)
    _identity(canvas, sheet, serial=True)
    _verdict(canvas, "RESULTADO DE CERTIFICACION", sheet["headline"], sheet)
    canvas.text(str(sheet.get("emta_tally") or ""), 9, color=_MUTED)
    profile = str(sheet.get("emta_profile") or "").strip()
    if profile:
        canvas.text(profile, 9, bold=True, color=(0.047, 0.176, 0.420))
        canvas.gap(4)
    headers = ("PARAMETRO", "LEIDO", "ESPERADO", "CUMPLE")
    widths = (180, 130, 150, 63)
    canvas.columns(headers, widths, header=True)
    canvas.rule()
    for row in sheet["rows"]:
        if canvas.low():
            canvas.page()
            canvas.columns(headers, widths, header=True)
            canvas.rule()
        canvas.columns((row["name"], row["read"], row["expected"], row["state"]), widths, state=row["state"])
        canvas.rule(light=True)
    _note(canvas, f"Hoja 1/{pages}.")
    canvas.page()
    _masthead(canvas, f"TECHCORP — Anexo RF · ORDEN: {sheet['order']} · Hoja 2/{pages}")
    canvas.text("ANALISIS DE ESPECTRO DE RF — CERTIFICACION", 11, bold=True)
    canvas.gap(4)
    counts = sheet.get("rf_channel_counts") or {}
    ds_power = (report.dut.ds_power or "").strip() or "NO_MEDIDO"
    us_power = (report.dut.us_power or "").strip() or "NO_MEDIDO"
    canvas.text(f"DS promedio {ds_power} · US promedio {us_power}", 9, color=_MUTED)
    if counts.get("ds") or counts.get("us"):
        canvas.text(
            " · ".join(part for part in (counts.get("ds"), counts.get("us")) if part),
            9,
            color=_MUTED,
        )
    canvas.gap(4)
    canvas.text(
        "El medidor grafico del espectro aparece en el HTML/PDF del navegador. "
        "Aqui se resume la lectura y el desglose fisico.",
        9,
        color=_MUTED,
    )
    canvas.gap(8)
    canvas.text("DESGLOSE DE CERTIFICACION FISICA", 11, bold=True)
    canvas.gap(6)
    physical_headers = ("PRUEBA", "LEIDO", "CRITERIO", "RESULTADO")
    physical_widths = (140, 120, 180, 83)
    canvas.columns(physical_headers, physical_widths, header=True)
    canvas.rule()
    for row in sheet.get("physical_rows") or []:
        if canvas.low():
            canvas.page()
            canvas.columns(physical_headers, physical_widths, header=True)
            canvas.rule()
        canvas.columns((row["name"], row["read"], row["expected"], row["state"]), physical_widths, state=row["state"])
        canvas.rule(light=True)
    _note(canvas, f"Hoja 2/{pages}.")
    canvas.page()
    _masthead(canvas, f"TECHCORP — Canales RF · ORDEN: {sheet['order']} · Hoja 3/{pages}")
    canvas.text("DETALLE DE CANALES RF — DOWNSTREAM", 11, bold=True)
    canvas.gap(4)
    _channel_table(canvas, "", sheet.get("ds_rows") or [], empty="Sin lectura de canales downstream en esta unidad.")
    if sheet.get("ds_correctable_total") or sheet.get("ds_uncorrectable_total"):
        canvas.text(
            f"Total corregibles {sheet.get('ds_correctable_total') or '—'} · incorregibles {sheet.get('ds_uncorrectable_total') or '—'}",
            8,
            color=_MUTED,
        )
    canvas.gap(6)
    canvas.text("DETALLE DE CANALES RF — UPSTREAM", 11, bold=True)
    canvas.gap(4)
    _channel_table(canvas, "", sheet.get("us_rows") or [], empty="Sin lectura de canales upstream en esta unidad.")
    _note(canvas, f"Hoja 3/{pages}. {report.disclaimer}")


def _matrix(canvas: _Canvas, report: CertificationReport, sheet: dict) -> None:
    _masthead(canvas, "REPORTE TÉCNICO DE CERTIFICACIÓN CPE")
    _identity(canvas, sheet, serial=True)
    _verdict(canvas, "RESULTADO L4 · MATRIZ P2P", sheet["headline"], sheet)
    rows = [
        (section, parameter, measured, reference, state)
        for section, parameter, measured, reference, state in [*sheet["identity"], *sheet["failures"]]
    ]
    rows.extend(
        (link["section"], link["parameter"], link["measured"], link["reference"], link["state"])
        for link in sheet["links"]
    )
    _table(
        canvas,
        ("SECCIÓN", "PARÁMETRO", "MEDIDO", "REFERENCIA", "ESTADO"),
        (78, 150, 110, 110, 75),
        rows,
    )
    _note(canvas, report.disclaimer)


def _masthead(canvas: _Canvas, subtitle: str) -> None:
    canvas.text("TECHCORP", 18, bold=True, color=(0.047, 0.176, 0.420))
    canvas.gap(4)
    canvas.text(subtitle, 9, color=_MUTED)
    canvas.gap(2)
    canvas.text("www.techcorpca.com", 9, color=(0.114, 0.306, 0.537))
    canvas.gap(10)


def _identity(canvas: _Canvas, sheet: dict, *, serial: bool) -> None:
    canvas.text(f"ORDEN: {sheet['order']}", 11, bold=True, align="right")
    if serial:
        canvas.text(f"SN: {sheet['serial']}", 10, align="right")
    canvas.text(str(sheet["long_date"]), 10, align="right", color=_MUTED)
    canvas.gap(8)


def _verdict(canvas: _Canvas, kicker: str, headline: str, sheet: dict) -> None:
    color = _GREEN if sheet["approved"] else _RED
    canvas.text(kicker, 9, bold=True)
    canvas.gap(2)
    canvas.text(str(headline), 18, bold=True, color=color)
    canvas.text(f"{sheet['stamp']}", 16, bold=True, color=color, align="right")
    canvas.text(f"{sheet['short_stamp']} · {sheet['summary']}", 9, color=_MUTED)
    canvas.gap(12)


def _table(canvas: _Canvas, headers: tuple[str, ...], widths: tuple[int, ...], rows: list[tuple]) -> None:
    canvas.rule()
    canvas.columns(headers, widths, header=True)
    canvas.rule()
    if not rows:
        canvas.text("Sin filas en este certificado.", 9, color=_MUTED)
        canvas.gap(8)
        return
    for row in rows:
        if canvas.low():
            canvas.page()
            canvas.columns(headers, widths, header=True)
            canvas.rule()
        canvas.columns(tuple(str(item) for item in row), widths, state=str(row[-1]))
        canvas.rule(light=True)


def _channel_table(canvas: _Canvas, title: str, rows: list, empty: str) -> None:
    canvas.text(title, 9, bold=True, color=(0.047, 0.176, 0.420))
    widths = (32, 78, 52, 100, 62, 46, 70, 83)
    headers = ("CANAL", "ESTADO", "MOD", "ID / FREC", "POTENCIA", "SNR", "CORR", "INCORR")
    if not rows:
        canvas.text(empty, 9, color=_MUTED)
        canvas.gap(4)
        return
    canvas.columns(headers, widths, header=True)
    canvas.rule()
    for row in rows:
        if canvas.low():
            canvas.page()
            canvas.columns(headers, widths, header=True)
            canvas.rule()
        canvas.columns(
            (
                row["channel"],
                row["state"],
                row["modulation"],
                row["identity"],
                row["power"],
                row["snr"],
                row["correctable"],
                row["uncorrectable"],
            ),
            widths,
        )
        canvas.rule(light=True)
    canvas.gap(4)


def _note(canvas: _Canvas, text: str) -> None:
    canvas.gap(6)
    for line in _wrap(text, 108):
        canvas.text(line, 8, color=_MUTED)


class _Canvas:
    def __init__(self) -> None:
        self.pages: list[list[str]] = []
        self.ops: list[str] = []
        self.y = _PAGE_H - 40
        self.page()

    def page(self) -> None:
        if self.ops:
            self.pages.append(self.ops)
        self.ops = []
        self.y = _PAGE_H - 40

    def low(self) -> bool:
        return self.y < 56

    def gap(self, points: int) -> None:
        self.y -= points

    def text(
        self,
        value: str,
        size: int,
        *,
        bold: bool = False,
        color: tuple[float, float, float] = _INK,
        align: str = "left",
    ) -> None:
        shown = _plain(value)
        width = len(shown) * size * 0.5
        x = _LEFT if align == "left" else max(_LEFT, _RIGHT - width)
        self.ops.append(_text(x, self.y - size, size, shown, bold, color))
        self.y -= size + 3

    def rule(self, *, light: bool = False) -> None:
        color = "0.90 0.92 0.95" if light else "0.84 0.86 0.89"
        self.ops.append(f"{color} RG 0.6 w {_LEFT} {self.y:.1f} m {_RIGHT} {self.y:.1f} l S")
        self.y -= 4

    def columns(self, cells: tuple[str, ...], widths: tuple[int, ...], *, header: bool = False, state: str = "") -> None:
        x = _LEFT
        size = 8 if not header else 8
        for index, (cell, width) in enumerate(zip(cells, widths)):
            color = _MUTED if header else _INK
            if not header and index == len(cells) - 1:
                color = _state_color(str(cell), state)
            shown = _clip(_plain(cell), width, size)
            self.ops.append(_text(x, self.y - size, size, shown, header or index == len(cells) - 1, color))
            x += width
        self.y -= size + 6

    def bytes(self) -> bytes:
        if self.ops:
            self.pages.append(self.ops)
        total = len(self.pages)
        objects: list[bytes] = []

        def add(body: bytes) -> int:
            objects.append(body)
            return len(objects)

        font1 = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
        font2 = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>")
        page_ids: list[int] = []
        content_ids: list[int] = []
        for index, ops in enumerate(self.pages, start=1):
            footer = _text(_LEFT, 28, 8, f"Hoja {index}/{total} · TECHCORP LAB · www.techcorpca.com", False, _MUTED)
            # An opaque white page. Firefox in dark theme otherwise leaves the sheet transparent and the text disappears.
            background = "1 1 1 rg 0 0 595 842 re f"
            stream = (background + "\n" + "\n".join([*ops, footer]) + "\n").encode("latin-1", "replace")
            content_ids.append(
                add(b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n" + stream + b"endstream")
            )
            page_ids.append(0)
        kids = []
        for number, content_id in zip(page_ids, content_ids):
            del number
            kids.append(
                add(
                    b"<< /Type /Page /Parent 0 0 R /MediaBox [0 0 595 842] "
                    + f"/Resources << /Font << /F1 {font1} 0 R /F2 {font2} 0 R >> >> ".encode("ascii")
                    + f"/Contents {content_id} 0 R >>".encode("ascii")
                )
            )
        # Parent id is filled after the pages catalog exists. Rewrite the placeholder.
        pages_id = add(
            b"<< /Type /Pages /Count "
            + str(total).encode("ascii")
            + b" /Kids ["
            + b" ".join(f"{kid} 0 R".encode("ascii") for kid in kids)
            + b"] >>"
        )
        for kid in kids:
            objects[kid - 1] = objects[kid - 1].replace(b"/Parent 0 0 R", f"/Parent {pages_id} 0 R".encode("ascii"))
        catalog = add(b"<< /Type /Catalog /Pages " + str(pages_id).encode("ascii") + b" 0 R >>")
        return _assemble(objects, catalog)


def _assemble(objects: list[bytes], catalog: int) -> bytes:
    chunks = [b"%PDF-1.4\n"]
    offsets = [0]
    for number, body in enumerate(objects, start=1):
        offsets.append(sum(len(chunk) for chunk in chunks))
        chunks.append(f"{number} 0 obj\n".encode("ascii") + body + b"\nendobj\n")
    xref = sum(len(chunk) for chunk in chunks)
    out = [f"xref\n0 {len(objects) + 1}\n", "0000000000 65535 f \n"]
    out.extend(f"{offset:010d} 00000 n \n" for offset in offsets[1:])
    out.append(f"trailer\n<< /Size {len(objects) + 1} /Root {catalog} 0 R >>\nstartxref\n{xref}\n%%EOF\n")
    return b"".join(chunks) + "".join(out).encode("ascii")


def _text(x: float, y: float, size: int, value: str, bold: bool, color: tuple[float, float, float]) -> str:
    font = "F2" if bold else "F1"
    red, green, blue = color
    return f"{red:.3f} {green:.3f} {blue:.3f} rg BT /{font} {size} Tf {x:.1f} {y:.1f} Td ({_pdf(value)}) Tj ET"


def _state_color(cell: str, state: str) -> tuple[float, float, float]:
    token = (cell or state).strip().upper()
    if token in {"OBSERVACION", "OBSERVACIÓN"}:
        return (0.706, 0.325, 0.035)
    if token in _PASS:
        return _GREEN
    if token in _FAIL:
        return _RED
    return _INK


def _plain(value: object) -> str:
    text = " ".join(str(value or "—").split())
    return (
        text.replace("≥", ">=")
        .replace("≤", "<=")
        .replace("—", "-")
        .replace("–", "-")
        .replace("−", "-")
        .replace("“", '"')
        .replace("”", '"')
    )


def _clip(value: str, width: int, size: int) -> str:
    limit = max(4, int(width / (size * 0.48)))
    if len(value) <= limit:
        return value
    return value[: limit - 3] + "..."


def _wrap(value: str, limit: int) -> list[str]:
    words = _plain(value).split(" ")
    lines: list[str] = []
    current = ""
    for word in words:
        trial = word if not current else f"{current} {word}"
        if len(trial) > limit and current:
            lines.append(current)
            current = word
        else:
            current = trial
    if current:
        lines.append(current)
    return lines or [""]


def _pdf(value: str) -> str:
    raw = value.encode("latin-1", "replace").decode("latin-1")
    return raw.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
