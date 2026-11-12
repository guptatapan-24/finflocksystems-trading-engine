"""Phase 9 endpoints: session records and the daily Intelligence Report. Read-only; nothing here writes except the
frozen report artifact (a stored copy of exactly what was shown). Access follows the branch-wide rule in api_utils."""
from datetime import datetime

from django.conf import settings

from apps.core.views import method_guard, response

from .api_utils import error, staff_required
from .models import SessionRecord
from .services import report_export, session as session_service
from .services import session_report

MAX_LIST = 120
FORMATS = {"json", "markdown", "md", "html", "pdf"}


def _parse_day(value):
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def _headline(record):
    h = (record.content or {}).get("headline") or {}
    return {"closed_trades": h.get("closed_trades"), "net_pnl": h.get("net_pnl"), "currency": record.currency,
            "decisions_total": h.get("decisions_total"), "open_at_asof": h.get("open_at_asof")}


@staff_required
def sessions(request):
    invalid = method_guard(request, "GET")
    if invalid:
        return invalid
    rows = SessionRecord.objects.order_by("-session_date", "zone")[:MAX_LIST]
    return response([{"zone": r.zone, "session_date": r.session_date.isoformat(), "status": r.status, "currency": r.currency,
                      "built_at": r.built_at, "finalized_at": r.finalized_at, "headline": _headline(r),
                      "narrative_status": (r.narrative or {}).get("status")} for r in rows],
                    meta={"total": SessionRecord.objects.count(), "enabled": session_service.conf.session_records_enabled()})


@staff_required
def session_detail(request, zone, day):
    invalid = method_guard(request, "GET")
    if invalid:
        return invalid
    zone = zone.upper()
    when = _parse_day(day)
    if zone not in session_service.SESSIONS:
        return error(404, "UNKNOWN_EXCHANGE", f"zone must be one of {', '.join(session_service.SESSIONS)}")
    if when is None:
        return error(400, "INVALID_DATE", "date must be YYYY-MM-DD")
    record = SessionRecord.objects.filter(zone=zone, session_date=when).first()
    if record is not None:
        return response({"zone": zone, "session_date": day, "status": record.status, "stored": True, "content": record.content,
                         "narrative": record.narrative, "content_hash": record.content_hash})
    content = session_service.build_content(zone, when)  # live view: computed from stored rows now, not saved
    return response({"zone": zone, "session_date": day, "status": "LIVE", "stored": False, "content": content,
                     "narrative": {}, "content_hash": session_service.content_hash(content)})


@staff_required
def daily_brief(request, zone, day):
    """The pre-open brief for one exchange and date: the stored one, or a live view computed now from stored items."""
    from .models import DailyBrief
    from .services import daily_brief as brief_service

    invalid = method_guard(request, "GET")
    if invalid:
        return invalid
    zone = zone.upper()
    when = _parse_day(day)
    if zone not in session_service.SESSIONS:
        return error(404, "UNKNOWN_EXCHANGE", f"zone must be one of {', '.join(session_service.SESSIONS)}")
    if when is None:
        return error(400, "INVALID_DATE", "date must be YYYY-MM-DD")
    row = DailyBrief.objects.filter(zone=zone, brief_date=when).first()
    if row is not None:
        return response({"zone": zone, "date": day, "stored": True, "final": row.final, "content": row.content, "content_hash": row.content_hash})
    _, _, open_dt, _ = session_service.window(zone, when)
    content = brief_service.build_content(zone, when, until=open_dt)
    return response({"zone": zone, "date": day, "stored": False, "final": False, "content": content, "content_hash": brief_service.content_hash(content)})


@staff_required
def daily_report(request):
    invalid = method_guard(request, "GET")
    if invalid:
        return invalid
    fmt = (request.GET.get("format") or "json").lower()
    if fmt not in FORMATS:
        return error(400, "INVALID_FORMAT", "format must be json, markdown, html or pdf")
    if fmt == "pdf" and not getattr(settings, "INTELLIGENCE_REPORT_PDF_ENABLED", True):
        return error(501, "REPORT_PDF_DISABLED", "PDF export is switched off (INTELLIGENCE_REPORT_PDF_ENABLED)")
    raw_day = request.GET.get("date")
    day = _parse_day(raw_day) if raw_day else None
    if raw_day and day is None:
        return error(400, "INVALID_DATE", "date must be YYYY-MM-DD")
    zones = None
    if request.GET.get("zone"):
        zone = request.GET["zone"].upper()
        if zone not in session_service.SESSIONS:
            return error(404, "UNKNOWN_EXCHANGE", f"zone must be one of {', '.join(session_service.SESSIONS)}")
        zones = [zone]
    report, frozen = session_report.daily_report(day, zones=zones)
    key = report["date"] + (f":{zones[0]}" if zones else "")
    report, digest, artifact = report_export.freeze_daily(key, report, frozen)
    try:
        download = report_export.respond(fmt, report, digest, artifact is not None,
                                         lambda ext: report_export.daily_filename(report, ext, zones[0] if zones else None))
    except report_export.ExportUnavailable:
        return error(501, "REPORT_PDF_UNAVAILABLE", "The PDF library is not installed on this server")
    if download is not None:
        return download
    return response({**report, "content_hash": digest, "frozen": artifact is not None, "artifact_id": getattr(artifact, "pk", None)})
