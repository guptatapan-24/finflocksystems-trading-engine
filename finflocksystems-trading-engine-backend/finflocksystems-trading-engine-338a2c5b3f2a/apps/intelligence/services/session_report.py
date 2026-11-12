"""Turns Session Records into the section/block report structure the exporters already render (JSON, Markdown, HTML,
PDF), and combines the exchanges of one day into "Intelligence Report - 23 October 2026".

Only stored data is shown. A section without data says so; a source that is not available says so; nothing is filled in.
"""
from datetime import date, datetime

from apps.intelligence.models import SessionRecord

from . import session as session_service
from .metrics import _zone
from .report import kv, table, text, bullets

DAILY_REPORT_VERSION = "D1"
EVIDENCE_ROWS = 14
DECISION_ORDER = ("ENTERED", "EXITED", "HOLDING", "ORDERS_WORKING", "TARGET_SET", "NO_TRADE", "AWAITING_QUOTES", "NOT_EVALUATED", "ERROR")


def _title_date(day):
    return f"{day.day} {day:%B %Y}"  # "23 October 2026" (no zero padding on the day)


def _local(zone, value, with_date=False):
    if not value:
        return ""
    try:
        moment = datetime.fromisoformat(str(value)).astimezone(_zone(zone))
    except ValueError:
        return str(value)
    return moment.strftime("%Y-%m-%d %H:%M" if with_date else "%H:%M:%S")


def _money(value, currency):
    return f"{value} {currency}"


def _disp(value):
    """Enum-like identifiers read better with spaces (and can wrap in a narrow table cell)."""
    return str(value).replace("_", " ") if value not in (None, "") else ""


def _counts(mapping):
    """{'ENTERED': 1, 'NO_TRADE': 3} -> '1 entered, 3 no trade'."""
    parts = [f"{mapping[k]} {_disp(k).lower()}" for k in DECISION_ORDER if mapping.get(k)]
    parts += [f"{v} {_disp(k).lower()}" for k, v in mapping.items() if k not in DECISION_ORDER and v]
    return ", ".join(parts) or "none"


def _short_time(value):
    try:
        return datetime.fromisoformat(str(value)).strftime("%Y-%m-%d %H:%M UTC")
    except ValueError:
        return str(value or "")


def _narrative_block(narrative, content=None):
    if not narrative:
        return [text("Model narrative: not generated for this record (it is produced after the session closes, and only when a language model is configured).")]
    if narrative.get("status") != "OK":
        return [text(f"Model narrative unavailable: {narrative.get('reason') or 'unknown reason'}.")]
    blocks = [text(f"Model-generated summary ({narrative.get('model')}, prompt v{narrative.get('prompt_version')}); every figure was checked against the stored data."),
              text(narrative.get("summary", ""))]
    highlights = narrative.get("highlights") or []
    if highlights:
        blocks.append(bullets(f"{h['text']} (supported by {len(h.get('evidence_refs') or [])} stored fact(s))" for h in highlights))
    if narrative.get("caveats"):
        blocks += [text("Caveats"), bullets(narrative["caveats"])]
    refs = []
    for h in highlights:
        refs += [r for r in h.get("evidence_refs") or [] if r not in refs]
    if refs and content is not None:
        facts = session_service.facts_for(content)
        shown = refs[:EVIDENCE_ROWS]
        blocks += [text("Stored facts the highlights rely on"
                        + (f" (first {EVIDENCE_ROWS} of {len(refs)}; the rest are in the JSON export)" if len(refs) > EVIDENCE_ROWS else "")),
                   table(["Fact key", "Stored value"], [[r, str(facts.get(r, ""))] for r in shown])]
    return blocks


def brief_lines(content):
    h, cur = content["headline"], content["currency"]
    d = h["decisions"]
    lines = [f"{content['label']} session {content['session_date']}: {h['closed_trades']} trade(s) closed (n = {h['closed_trades']}), "
             f"net {_money(h['net_pnl'], cur)} after costs; {h['open_at_asof']} position(s) open as of {_local(content['zone'], content['asof'])} local."]
    lines.append(f"{h['decisions_total']} strategy decision(s) were evaluated"
                 + (f": {_counts(d)}." if d else "."))
    if content["not_taken"]:
        closest = [g for g in content["not_taken"] if g.get("closest")]
        if closest:
            best = min(closest, key=lambda g: abs(g["closest"]["margin"]))["closest"]
            lines.append(f"The closest a strategy came to trading without doing so: {best['gate']} {best['value']:g} against {best['threshold']:g} "
                         f"({best['margin']:+g}) at {_local(content['zone'], best['at'])} local.")
    if h["strategies_without_evaluation"]:
        lines.append(f"{h['strategies_without_evaluation']} strateg{'y' if h['strategies_without_evaluation'] == 1 else 'ies'} had no evaluation at all in this window.")
    return lines


def _context_status_text(context):
    if not context:
        return "not captured in this record"
    parts = []
    for src in context.get("sources") or []:
        text_ = {"OK": "OK", "DISABLED": "off", "NOT_CONFIGURED": "not configured", "FAILING": "failing", "NO_DATA_YET": "no data yet"}.get(src["state"], src["state"])
        extra = f" ({src['items_total']} items)" if src["state"] == "OK" else ""
        parts.append(f"{src['name']} {text_}{extra}")
    return "; ".join(parts) or "no source information"


def context_section(content):
    """'Outside context that day' from the record's stored context (as published; nothing is judged here)."""
    z, label = content["zone"], content["label"]
    ctx = content.get("context") or {}
    sid = f"{z.lower()}-context"
    title = f"{label}: outside context that day (as published, from stored items)"
    if not ctx.get("available"):
        return {"id": sid, "title": title, "blocks": [text("No outside context was stored for this day"
                + (": " + " ".join(ctx.get("notes") or []) if ctx.get("notes") else " (the sources are off, not configured, or had not run).")
                + " Nothing is shown rather than guessed.")]}
    blocks = []
    if ctx.get("notes"):
        blocks.append(bullets(ctx["notes"]))
    if ctx.get("holiday"):
        blocks.append(kv([("Exchange holiday", ctx["holiday"]["name"]), ("Trading hours", ctx["holiday"].get("trading_hour"))]))
    if ctx.get("events"):
        blocks += [text("Scheduled events that matter to this market"),
                   table(["Time", "Impact", "Country", "Event", "Estimate", "Previous"],
                         [[_local(z, e["at"]), e["impact"] or "", e["region"] or "", e["event"], e["estimate"] if e["estimate"] is not None else "",
                           e["previous"] if e["previous"] is not None else ""] for e in ctx["events"]])]
    if ctx.get("earnings"):
        blocks += [text("Earnings dates for symbols traded here"), table(["Date", "Symbol", "Detail"], [[e["at"][:10] if e["at"] else "", e["symbol"], e["title"]] for e in ctx["earnings"]])]
    if ctx.get("announcements"):
        blocks += [text("Official announcements"), table(["Published", "Source", "Announcement"], [[_local(z, a["at"]), a["source"], a["headline"]] for a in ctx["announcements"]])]
    if ctx.get("headlines"):
        blocks += [text("Headlines (one per story)"),
                   table(["Published", "Source", "Headline", "Sentiment"], [[_local(z, h["at"]), h.get("publisher") or h["source"], h["headline"],
                                                                         "" if h["sentiment"] is None else f"{h['sentiment']:+.2f}"] for h in ctx["headlines"]])]
    exposed = [p for p in ctx.get("open_positions") or [] if p.get("high_impact_events_before_expiry")]
    if exposed:
        blocks += [text("Open positions with high-impact events before their expiry"),
                   table(["Position", "Expiry", "Days left", "Events before expiry"],
                         [[f"{p['instrument']} ({p['instance']})", p["expiry"], p["days_to_expiry"],
                           "; ".join(f"{_local(z, e['at'], True)} {e['event']}" for e in p["high_impact_events_before_expiry"])] for p in exposed])]
    if not (ctx.get("events") or ctx.get("headlines") or ctx.get("announcements") or ctx.get("earnings")):
        blocks.append(text("Sources were reachable but nothing relevant to this market was stored for this day."))
    blocks.append(text("Headlines are shown as published by their sources: they are context, not evidence that anything caused a trade outcome."))
    return {"id": sid, "title": title, "blocks": blocks}


def zone_sections(content, narrative, record_status=None):
    """Sections for one exchange. Ids and titles carry the exchange label so a combined report stays unambiguous."""
    z, label, cur = content["zone"], content["label"], content["currency"]
    sid = lambda name: f"{z.lower()}-{name}"  # noqa: E731
    h = content["headline"]
    flags = content["data_quality"]
    llm = ("not used" if not narrative else f"{narrative.get('model')} (narrative OK)" if narrative.get("status") == "OK"
           else f"unavailable: {narrative.get('reason')}")
    sections = [{"id": sid("sources"), "title": f"{label}: where this report comes from", "blocks": [kv([
        ("Exchange / currency", f"{label} ({z}) / {cur}"), ("Session date (exchange local)", content["session_date"]),
        ("Regular hours", content["regular_hours"] + " local"),
        ("State", f"{content['phase'].replace('_', ' ').lower()}; as of {_local(z, content['asof'], True)} local; record "
                  f"{record_status.lower() if record_status else 'live view, not stored'}"),
        ("Engine database", "read (stored rows only; no broker or market-data call was made)"),
        ("Market data", "delayed (PAPER): path metrics are approximate" if flags.get("PAPER_DELAYED_DATA") else "no delay flagged on these trades"),
        ("Decision ledger", "truncated: earlier decisions of the day may be missing" if flags.get("LEDGER_TRUNCATED") else "complete for this window"),
        ("Language model", llm),
        ("Outside context (news, calendars)", _context_status_text(content.get("context")))])]}]
    sections.append({"id": sid("headline"), "title": f"{label}: headline ({cur} only)", "blocks": [kv([
        ("Trades closed", f"{h['closed_trades']} (winning: {h['winning_trades']})"),
        ("Net P&L after costs", _money(h["net_pnl"], cur)),
        ("Trades opened", h["opened_trades"]), ("Positions open as of the report time", h["open_at_asof"]),
        ("Strategies on this exchange", f"{h['strategies']} ({h['strategies_without_evaluation']} without any evaluation)"),
        ("Decisions evaluated", f"{h['decisions_total']} ({_counts(h['decisions'])})"),
        ("Alerts", _counts(h["alerts"]) if h["alerts"] else "none"),
        ("Operator actions", h["operator_actions"]),
        ("Risk interventions", _counts(h["risk_interventions"]) if h["risk_interventions"] else "none"),
        ("Material reconciliation breaks", h["material_reconciliation_breaks"]), ("Kill switches engaged", h["kill_switches"])]),
        *([bullets(content["notes"])] if content["notes"] else [])]})
    sections.append({"id": sid("brief"), "title": f"{label}: the session in brief",
                     "blocks": [bullets(brief_lines(content)), *_narrative_block(narrative, content)]})
    sections.append(context_section(content))
    sections.append({"id": sid("timeline"), "title": f"{label}: timeline ({z}-local time)", "blocks": [
        table(["Time", "Strategy / instrument", "What", "Detail"],
              [[_local(z, e["time"]), e.get("instance") or "", e["kind"].replace(":", " ").replace("_", " "), e["text"]] for e in content["timeline"]])
        if content["timeline"] else text("No events were recorded in this session."),
        *([text("The timeline is capped; earlier entries are shown first.")] if content["timeline_truncated"] else [])]})
    trade_rows = [[t["instrument"], t["instance"], _disp(t["direction"]), _local(z, t["opened_at"], True), _local(z, t["closed_at"], True),
                   _money(t["net_pnl"], t["currency"]), _disp(t["exit_reason"]), t["review_grade"] or "", ", ".join(_disp(f) for f in t["flags"])]
                  for t in content["closed_trades"]]
    sections.append({"id": sid("trades"), "title": f"{label}: trades closed in this session", "blocks": [
        table(["Instrument", "Strategy", "Direction", "Opened", "Closed", "Net P&L", "Exit", "Review grade", "Flags"], trade_rows)
        if trade_rows else text("No trade closed in this session.")]})
    taken = [[g["instance"], _disp(g["decision"]).lower(), _disp(g["gate"]), g["reason"], g["count"],
              f"{_local(z, g['first'])} - {_local(z, g['last'])}",
              (f"{g['closest']['gate']} {g['closest']['value']:g} vs {g['closest']['threshold']:g} ({g['closest']['margin']:+g}) at {_local(z, g['closest']['at'])}"
               if g.get("closest") else "")] for g in content["not_taken"]]
    blocks = [table(["Strategy", "Decision", "Gate", "Reason", "Evaluations", "First - last", "Closest to trading"], taken) if taken
              else text("No decision was recorded as 'not taken' in this session.")]
    if content["idle_strategies"]:
        blocks += [text("Strategies with no evaluation at all in this window"),
                   table(["Strategy", "Definition", "State", "Enabled"], [[i["instance"], i["strategy"], i["state"], "yes" if i["enabled"] else "no"] for i in content["idle_strategies"]])]
    sections.append({"id": sid("not-taken"), "title": f"{label}: decisions not taken (from the engine's decision ledger)", "blocks": blocks})
    open_rows = []
    for t in content["open_positions"]:
        m = t.get("latest_mark") or {}
        open_rows.append([t["instrument"], t["instance"], t["direction"].replace("_", " "), _local(z, t["opened_at"], True),
                          m.get("mark_pnl") or "not recorded", m.get("pct_of_max_loss") or "", m.get("quote_freshness") or "", ", ".join(_disp(f) for f in t["flags"])])
    sections.append({"id": sid("open"), "title": f"{label}: open positions (as of {_local(z, content['asof'])} local)", "blocks": [
        table(["Instrument", "Strategy", "Direction", "Opened", "Latest mark P&L", "% of max loss", "Quote freshness", "Flags"], open_rows)
        if open_rows else text("No position was open.")]})
    op = [[_local(z, o["at"]), o["type"].replace("_", " ").lower(), o.get("actor") or "", o.get("action") or o.get("note") or
           "; ".join(f"{k}: {a} -> {b}" for k, (a, b) in (o.get("changes") or {}).items()) or ""] for o in content["operator"]]
    sections.append({"id": sid("inputs"), "title": f"{label}: your inputs in this session", "blocks": [
        table(["Time", "What", "Who", "Detail"], op) if op else text("No operator input was journalled in this session.")]})
    al = [[_local(z, a["time"]), a["severity"], a["instrument"], a["text"], a["occurrences"]] for a in content["alerts"]]
    sections.append({"id": sid("alerts"), "title": f"{label}: alerts", "blocks": [
        table(["Time", "Severity", "Instrument", "Alert", "Seen"], al) if al else text("No alert was raised.")]})
    exc = []
    if content["risk"]:
        exc += [text("Risk checks that did not simply approve"),
                table(["Time", "Instrument", "Check", "Decision", "Reason", "Requested", "Approved"],
                      [[_local(z, r["time"]), r["instrument"], r["check"], r["decision"], r["reason"], r["requested"], r["approved"]] for r in content["risk"]])]
    if content["reconciliation"]:
        exc += [text("Material reconciliation breaks"), table(["Time", "Category", "Severity", "Resolved"],
                [[_local(z, b["time"]), b["category"], b["severity"], "yes" if b["resolved"] else "no"] for b in content["reconciliation"]])]
    if content["kill_switches"]:
        exc += [text("Kill switches engaged"), table(["Time", "Scope", "Reason"], [[_local(z, k["time"]), k["scope"], k["reason"]] for k in content["kill_switches"]])]
    exc.append(kv([("Data-quality flags (trades affected)", ", ".join(f"{_disp(k)} x{v}" for k, v in flags.items()) or "none")]))
    sections.append({"id": sid("exceptions"), "title": f"{label}: exceptions and data quality", "blocks": exc})
    return sections


def provenance_section(rows):
    return {"id": "provenance", "title": "Provenance", "blocks": [table(
        ["Exchange", "Session date", "Record", "Version", "Content hash", "Built", "Frozen at"],
        [[r["label"], r["session_date"], r["status"], r["record_version"], (r["content_hash"] or "")[:16], _short_time(r["built_at"]),
          _short_time(r["finalized_at"]) if r["finalized_at"] else ""] for r in rows])]}


def daily_report(day=None, now=None, zones=None):
    """The combined report for `day` (the exchange-local session date; each exchange's own current date when omitted).
    Returns (report dict, frozen) where frozen is True only when every included record is FINAL."""
    from django.utils import timezone

    now = now or timezone.now()
    zones = zones or session_service.zones_with_strategies()
    sections, prov, frozen, shown_day = [], [], True, day
    for zone in zones:
        d = day or session_service.local_date(zone, now)
        shown_day = shown_day or d
        record = SessionRecord.objects.filter(zone=zone, session_date=d).first()
        if record is not None:
            content, narrative, status = record.content, record.narrative, record.status
            built, finalized, digest = record.built_at.isoformat(), record.finalized_at.isoformat() if record.finalized_at else "", record.content_hash
        else:  # nothing stored for this day yet: a live view, computed now from stored rows, never saved
            content = session_service.build_content(zone, d, now)
            narrative, status, built, finalized, digest = {}, "LIVE", now.isoformat(), "", session_service.content_hash(content)
        frozen = frozen and status == SessionRecord.Status.FINAL
        sections += zone_sections(content, narrative, status if status != "LIVE" else None)
        prov.append({"label": content["label"], "session_date": content["session_date"], "status": status,
                     "record_version": content["record_version"], "content_hash": digest, "built_at": built, "finalized_at": finalized})
    if not zones:
        sections.append({"id": "no-strategies", "title": "No strategies", "blocks": [text("No strategy instance exists, so there is no session to report.")]})
        frozen = False
    sections.append(provenance_section(prov))
    day_for_title = shown_day if isinstance(shown_day, date) else session_service.local_date(zones[0], now) if zones else now.date()
    return {"scope": "daily", "date": day_for_title.isoformat(), "title": f"Intelligence Report - {_title_date(day_for_title)}",
            "report_version": DAILY_REPORT_VERSION,
            "provenance": {"report_version": DAILY_REPORT_VERSION, "records": prov, "frozen": frozen},
            "sections": sections}, frozen
