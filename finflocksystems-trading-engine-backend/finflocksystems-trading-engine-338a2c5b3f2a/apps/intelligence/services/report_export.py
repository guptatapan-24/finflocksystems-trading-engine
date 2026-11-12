"""Downloadable reports (R21): freeze, hash and render.

A report is the structured dict built by `report.build_report`. It is hashed (SHA-256 of its canonical JSON), stored
once as a `ReportArtifact` when the trade is closed, and rendered to HTML or PDF from that dict. Rendering is
deterministic, so the same stored report always produces the same file. Missing data is printed as missing by the
report builder; nothing is filled in here.
"""
import hashlib
import html
import json
import re
from io import BytesIO

from django.utils import timezone

from apps.intelligence.models import ReportArtifact

PDF_TITLE = "Intelligence Report"
FOOTER_NOTE = "Finflock intelligence layer - advisory only"
# Helvetica (the built-in PDF font) covers Latin-1 only: map the few other characters we may print.
_REPLACEMENTS = {"→": "->", "←": "<-", "—": "-", "–": "-", "≥": ">=", "≤": "<=", "≈": "~",
                 "’": "'", "‘": "'", "“": '"', "”": '"', "•": "-", " ": " ", "−": "-"}


class ExportUnavailable(Exception):
    """The PDF library is not installed."""


def canonical_hash(report):
    return hashlib.sha256(json.dumps(report, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def normalise(report):
    """JSON round trip, so the hash, the stored content and every rendering see exactly the same values."""
    return json.loads(json.dumps(report, default=str))


def freeze(episode, report):
    """Return (normalised report, hash, artifact or None). The report is stored once if the trade is final. Open
    trades are never stored: their report is still changing, and the hash of a live view is marked as not frozen."""
    report = normalise(report)
    digest = canonical_hash(report)
    if episode.status not in ("CLOSED", "EXPIRED"):
        return report, digest, None
    artifact, _ = ReportArtifact.objects.get_or_create(
        kind=ReportArtifact.Kind.TRADE, scope_key=f"episode:{episode.pk}", content_hash=digest,
        defaults={"report_version": report.get("report_version", ""), "content": report,
                  "provenance": report.get("provenance") or {}})
    return report, digest, artifact


def freeze_daily(day_key, report, frozen):
    """Daily report: stored once, and only when every exchange's record is FINAL. Returns (report, hash, artifact or None)."""
    report = normalise(report)
    digest = canonical_hash(report)
    if not frozen:
        return report, digest, None
    artifact, _ = ReportArtifact.objects.get_or_create(
        kind=ReportArtifact.Kind.DAILY, scope_key=f"daily:{day_key}", content_hash=digest,
        defaults={"report_version": report.get("report_version", ""), "content": report,
                  "provenance": report.get("provenance") or {}})
    return report, digest, artifact


def daily_filename(report, extension, zone=None):
    return f"finflock_intelligence_report_{report.get('date', 'undated')}{'_' + zone if zone else ''}.{extension}"


def respond(fmt, report, digest, frozen, name_for):
    """HttpResponse for html / pdf / markdown (a download); None for json, which the caller wraps in its envelope.
    `name_for(extension)` gives the file name. Raises ExportUnavailable when the PDF library is missing."""
    from django.http import HttpResponse

    from .report import to_markdown

    if fmt in ("markdown", "md"):
        res = HttpResponse(to_markdown(report), content_type="text/markdown; charset=utf-8")
        extension = "md"
    elif fmt == "html":
        res = HttpResponse(to_html(report, digest, frozen), content_type="text/html; charset=utf-8")
        extension = "html"
    elif fmt == "pdf":
        res = HttpResponse(to_pdf(report, digest, frozen), content_type="application/pdf")
        extension = "pdf"
    else:
        return None
    res["Content-Disposition"] = f'attachment; filename="{name_for(extension)}"'
    return res


def filename(report, episode, extension, today=None):
    day = (episode.closed_at or today or timezone.now()).strftime("%Y%m%d")
    safe = lambda v: re.sub(r"[^A-Za-z0-9_]+", "-", str(v or "")).strip("-")[:32] or "x"  # noqa: E731
    return f"finflock_{safe(episode.strategy_key)}_{safe(episode.instrument_symbol)}_episode{episode.pk}_{day}.{extension}"


# ---- HTML ---------------------------------------------------------------------------------------------
def _h(value):
    return html.escape("" if value is None else str(value))


def to_html(report, digest, frozen):
    out = ["<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">",
           f"<title>{_h(PDF_TITLE)}: {_h(report['title'])}</title>",
           "<style>body{font-family:Arial,Helvetica,sans-serif;max-width:960px;margin:24px auto;padding:0 16px;color:#111;line-height:1.45}"
           "h1{font-size:22px;border-bottom:3px solid #1f3a5f;padding-bottom:6px}h2{font-size:16px;margin-top:26px;color:#1f3a5f}"
           "table{border-collapse:collapse;width:100%;font-size:12px;margin:8px 0}th,td{border:1px solid #bbb;padding:4px 6px;text-align:left;vertical-align:top}"
           "th{background:#e8edf3}dl{margin:8px 0}dt{font-weight:bold;float:left;clear:left;width:240px}dd{margin:0 0 4px 250px}"
           ".foot{margin-top:32px;font-size:11px;color:#555;border-top:1px solid #bbb;padding-top:6px}"
           "@media print{body{margin:0}h2{page-break-after:avoid}table{page-break-inside:auto}tr{page-break-inside:avoid}}</style></head><body>",
           f"<h1>{_h(PDF_TITLE)}: {_h(report['title'])}</h1>"]
    for section in report["sections"]:
        out.append(f"<h2>{_h(section['title'])}</h2>")
        for block in section["blocks"]:
            kind = block["type"]
            if kind == "kv":
                out.append("<dl>" + "".join(f"<dt>{_h(r['label'])}</dt><dd>{_h(r['value'])}</dd>" for r in block["rows"]) + "</dl>")
            elif kind == "text":
                out.append(f"<p>{_h(block['text'])}</p>")
            elif kind == "list":
                out.append("<ul>" + "".join(f"<li>{_h(i)}</li>" for i in block["items"]) + "</ul>")
            elif kind == "table":
                if not block["rows"]:
                    out.append("<p><em>None.</em></p>")
                else:
                    head = "".join(f"<th>{_h(c)}</th>" for c in block["columns"])
                    body = "".join("<tr>" + "".join(f"<td>{_h(c)}</td>" for c in row) + "</tr>" for row in block["rows"])
                    out.append(f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>")
    out.append(f"<div class=\"foot\">{_h(FOOTER_NOTE)} | report version {_h(report.get('report_version'))} | content hash "
               f"{_h(digest)} | {'frozen at close' if frozen else 'LIVE VIEW, not frozen (the trade is still open)'}</div></body></html>")
    return "".join(out)


# ---- PDF ----------------------------------------------------------------------------------------------
def _pdf_text(value):
    text = "" if value is None else str(value)
    for old, new in _REPLACEMENTS.items():
        text = text.replace(old, new)
    return text.encode("latin-1", "replace").decode("latin-1")


def to_pdf(report, digest, frozen):
    try:
        from reportlab.lib import colors
        from reportlab.lib.enums import TA_LEFT
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import mm
        from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    except ImportError as exc:  # pragma: no cover - exercised only when the dependency is missing
        raise ExportUnavailable("reportlab is not installed") from exc

    base = getSampleStyleSheet()
    body = ParagraphStyle("body", parent=base["BodyText"], fontName="Helvetica", fontSize=9, leading=12, alignment=TA_LEFT)
    small = ParagraphStyle("small", parent=body, fontSize=7.5, leading=9.5)
    head = ParagraphStyle("head", parent=body, fontName="Helvetica-Bold", fontSize=7.5, leading=9.5, textColor=colors.white)
    h1 = ParagraphStyle("h1", parent=base["Title"], fontName="Helvetica-Bold", fontSize=17, leading=21, alignment=TA_LEFT,
                        textColor=colors.HexColor("#1f3a5f"), spaceAfter=4)
    h2 = ParagraphStyle("h2", parent=base["Heading2"], fontName="Helvetica-Bold", fontSize=11.5, leading=14,
                        textColor=colors.HexColor("#1f3a5f"), spaceBefore=10, spaceAfter=3)
    key = ParagraphStyle("key", parent=body, fontName="Helvetica-Bold")
    para = lambda t, st=body: Paragraph(_xml(_pdf_text(t)), st)  # noqa: E731
    width = A4[0] - 30 * mm

    title = report["title"] if report["title"].startswith(PDF_TITLE) else f"{PDF_TITLE}: {report['title']}"
    story = [para(title, h1),
             para(f"Report version {report.get('report_version')} | {'frozen at close' if frozen else 'LIVE VIEW - the trade is still open, not frozen'}"
                  f" | content hash {digest[:16]}", small), Spacer(1, 4)]
    for section in report["sections"]:
        pieces = [para(section["title"], h2)]
        for block in section["blocks"]:
            kind = block["type"]
            if kind == "kv" and block["rows"]:
                data = [[para(r["label"], key), para(r["value"])] for r in block["rows"]]
                tbl = Table(data, colWidths=[width * 0.34, width * 0.66])
                tbl.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.HexColor("#cccccc")),
                                         ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2)]))
                pieces.append(tbl)
            elif kind == "text":
                pieces.append(para(block["text"]))
            elif kind == "list":
                pieces += [para(f"- {i}") for i in block["items"]]
            elif kind == "table":
                if not block["rows"]:
                    pieces.append(para("None."))
                    continue
                columns = block["columns"]
                cell_style = ParagraphStyle("tiny", parent=small, fontSize=7, leading=8.6) if len(columns) >= 8 else small
                data = [[para(c, head) for c in columns]] + [[para(c, cell_style) for c in row] for row in block["rows"]]
                weights = _column_weights(columns, block["rows"])
                tbl = Table(data, colWidths=[width * w for w in weights], repeatRows=1)
                tbl.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f3a5f")), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                                         ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#bbbbbb")),
                                         ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f3f5f8")]),
                                         ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2)]))
                pieces.append(tbl)
            pieces.append(Spacer(1, 3))
        story.append(KeepTogether(pieces[:2]))  # heading never sits alone at the bottom of a page
        story.extend(pieces[2:])

    footer = f"{FOOTER_NOTE} | hash {digest[:12]} | {'frozen' if frozen else 'live view'}"

    def decorate(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(colors.HexColor("#555555"))
        canvas.drawString(15 * mm, 9 * mm, _pdf_text(footer))
        canvas.drawRightString(A4[0] - 15 * mm, 9 * mm, f"Page {doc.page}")
        canvas.setStrokeColor(colors.HexColor("#1f3a5f"))
        canvas.setLineWidth(1.2)
        canvas.line(15 * mm, A4[1] - 11 * mm, A4[0] - 15 * mm, A4[1] - 11 * mm)
        canvas.restoreState()

    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm, topMargin=16 * mm, bottomMargin=16 * mm,
                            title=_pdf_text(title), author="Finflock intelligence layer",
                            subject=f"content hash {digest}", invariant=1, pageCompression=0)
    doc.build(story, onFirstPage=decorate, onLaterPages=decorate)
    return buffer.getvalue()


def _xml(text):
    return html.escape(text, quote=False).replace("\n", "<br/>")


def _column_weights(columns, rows):
    """Column widths: wide enough that no word is broken (longest word), with extra room for long text columns."""
    weights = []
    for i, column in enumerate(columns):
        cells = [str(column)] + [str(r[i]) for r in rows if i < len(r)]
        longest_word = max((len(w) for c in cells for w in c.split()), default=4)
        longest = max(len(c) for c in cells)
        weights.append(max(min(longest_word, 24) + 4, min(longest, 48) * 0.5, 8.0))  # +4: the cell's left and right padding
    total = sum(weights)
    return [w / total for w in weights]
