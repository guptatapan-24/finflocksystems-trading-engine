"""Phase 4 endpoints: live monitor, alerts, commentary, report. Staff-only, CSRF-protected."""
import hashlib
from datetime import timedelta

from django.conf import settings
from django.http import HttpResponse
from django.utils import timezone

from apps.core.views import method_guard, response

from .api_utils import error, json_body, run_idempotent, staff_required
from .models import EpisodeLiveSnapshot, LiveAlert, LlmRunLog, TradeEpisode
from .services import commentary as commentary_service
from .services import report as report_service
from .services import report_export
from .views import _episode_row

MAX_LIMIT = 200
HISTORY_POINTS = 120


def _snapshot_row(snap):
    if snap is None:
        return None
    return {"id": snap.pk, "observed_at": snap.observed_at, "quote_freshness": snap.quote_freshness,
            "marks": snap.marks, "mark_pnl": None if snap.mark_pnl is None else str(snap.mark_pnl),
            "pnl_bps": None if snap.pnl_bps is None else str(snap.pnl_bps),
            "pct_of_max_loss": None if snap.pct_of_max_loss is None else str(snap.pct_of_max_loss),
            "pct_of_max_profit": None if snap.pct_of_max_profit is None else str(snap.pct_of_max_profit),
            "mae_bps_so_far": None if snap.mae_bps_so_far is None else str(snap.mae_bps_so_far),
            "mfe_bps_so_far": None if snap.mfe_bps_so_far is None else str(snap.mfe_bps_so_far),
            "distances": snap.distances, "material": snap.material}


def _alert_row(alert):
    return {"id": alert.pk, "episode_id": alert.episode_id, "rule_id": alert.rule_id, "severity": alert.severity,
            "text": alert.text, "details": alert.details, "occurrences": alert.occurrences,
            "first_seen": alert.first_seen, "last_seen": alert.last_seen, "acknowledged_by": alert.acknowledged_by}


LIVE_NOTE = ("Marks come from the engine's last stored reference prices. Implied volatility is only measured when the "
             "strategy evaluates (once per bar), so live IV is not shown between evaluations.")


@staff_required
def episode_live(request, episode_id):
    invalid = method_guard(request, "GET")
    if invalid:
        return invalid
    episode = TradeEpisode.objects.filter(pk=episode_id).select_related("metrics_row").first()
    if episode is None:
        return error(404, "EPISODE_NOT_FOUND", "Trade episode not found")
    snapshots = list(EpisodeLiveSnapshot.objects.filter(episode=episode).order_by("-observed_at")[:HISTORY_POINTS])
    alerts = LiveAlert.objects.filter(episode=episode).order_by("-last_seen")[:50]
    return response({
        "episode": _episode_row(episode),
        "latest": _snapshot_row(snapshots[0] if snapshots else None),
        "series": [{"t": s.observed_at, "mark_pnl": None if s.mark_pnl is None else str(s.mark_pnl)} for s in reversed(snapshots)],
        "alerts": [_alert_row(a) for a in alerts], "notes": [LIVE_NOTE],
        "is_open": episode.status == TradeEpisode.Status.OPEN,
    })


@staff_required
def open_episodes(request):
    invalid = method_guard(request, "GET")
    if invalid:
        return invalid
    episodes = list(TradeEpisode.objects.filter(status=TradeEpisode.Status.OPEN).order_by("-opened_at")[:100])
    ids = [e.pk for e in episodes]
    latest = {}
    for snap in EpisodeLiveSnapshot.objects.filter(episode_id__in=ids).order_by("episode_id", "-observed_at"):
        latest.setdefault(snap.episode_id, snap)
    open_alerts = {}
    for alert in LiveAlert.objects.filter(episode_id__in=ids):
        bucket = open_alerts.setdefault(alert.episode_id, {"total": 0, "high": 0})
        bucket["total"] += 1
        bucket["high"] += alert.severity == "HIGH"
    marker = hashlib.sha256(repr((ids, [(k, v.pk) for k, v in sorted(latest.items())],
                                  sorted((k, tuple(v.values())) for k, v in open_alerts.items()))).encode()).hexdigest()[:24]
    etag = f'"{marker}"'
    if request.headers.get("If-None-Match") == etag:
        reply = HttpResponse(status=304)
        reply["ETag"] = etag
        return reply
    rows = [{**_episode_row(e), "latest": _snapshot_row(latest.get(e.pk)), "alerts": open_alerts.get(e.pk, {"total": 0, "high": 0})}
            for e in episodes]
    reply = response(rows, meta={"total": len(rows), "notes": [LIVE_NOTE]})
    reply["ETag"] = etag
    return reply


@staff_required
def alerts(request):
    invalid = method_guard(request, "GET")
    if invalid:
        return invalid
    try:
        since = int(request.GET.get("since", 0))
        limit = min(max(int(request.GET.get("limit", 100)), 1), MAX_LIMIT)
    except ValueError:
        return error(400, "INVALID_PAGINATION", "since and limit must be integers")
    query = LiveAlert.objects.filter(pk__gt=since)
    if request.GET.get("severity") in {"LOW", "MEDIUM", "HIGH"}:
        query = query.filter(severity=request.GET["severity"])
    if request.GET.get("episode_id", "").isdigit():
        query = query.filter(episode_id=int(request.GET["episode_id"]))
    rows = list(query.order_by("pk")[:limit])
    return response([_alert_row(a) for a in rows], meta={"cursor": rows[-1].pk if rows else since, "limit": limit})


@staff_required
def live_commentary(request, episode_id):
    invalid = method_guard(request, "POST")
    if invalid:
        return invalid
    payload = json_body(request)
    if payload is None:
        return error(400, "INVALID_REQUEST", "Request body must be a JSON object")
    episode = TradeEpisode.objects.filter(pk=episode_id, status=TradeEpisode.Status.OPEN).first()
    if episode is None:
        return error(404, "EPISODE_NOT_OPEN", "Commentary is only available for an open trade")

    def handler():
        limit = int(getattr(settings, "INTELLIGENCE_RERUN_LIMIT_PER_MINUTE", 5))
        recent = LlmRunLog.objects.filter(scope="LIVE_COMMENTARY", created_at__gte=timezone.now() - timedelta(minutes=1)).count()
        if limit > 0 and recent >= limit:
            raise commentary_service.CommentaryError(429, "RATE_LIMITED", "Too many commentary requests; retry later")
        return 200, commentary_service.comment_on(episode)

    return run_idempotent(request, f"live-commentary:{episode_id}", payload, handler)


@staff_required
def episode_report(request, episode_id):
    invalid = method_guard(request, "GET")
    if invalid:
        return invalid
    episode = (TradeEpisode.objects.filter(pk=episode_id).select_related("metrics_row", "evidence_snapshot").first())
    if episode is None:
        return error(404, "EPISODE_NOT_FOUND", "Trade episode not found")
    fmt = (request.GET.get("format") or "json").lower()
    if fmt not in {"json", "markdown", "md", "html", "pdf"}:
        return error(400, "INVALID_FORMAT", "format must be json, markdown, html or pdf")
    if fmt == "pdf" and not getattr(settings, "INTELLIGENCE_REPORT_PDF_ENABLED", True):
        return error(501, "REPORT_PDF_DISABLED", "PDF export is switched off (INTELLIGENCE_REPORT_PDF_ENABLED)")
    report, digest, artifact = report_export.freeze(episode, report_service.build_report(episode))
    frozen = artifact is not None
    try:
        download = report_export.respond(fmt, report, digest, frozen, lambda ext: report_export.filename(report, episode, ext))
    except report_export.ExportUnavailable:
        return error(501, "REPORT_PDF_UNAVAILABLE", "The PDF library is not installed on this server")
    if download is not None:
        return download
    return response({**report, "content_hash": digest, "frozen": frozen, "artifact_id": getattr(artifact, "pk", None)})
