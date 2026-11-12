"""Outside context for the intelligence layer (Phase 10): calendars, news and official announcements.

Rules that hold for everything in this package: network I/O happens only in the `celery-intelligence` worker through the
poller (R16); items are stored point in time and never fetched at review time (R18); text from outside is untrusted and
sanitised on the way in (R8, R17); a source outage is recorded and never raises into trading code (R2).
"""
