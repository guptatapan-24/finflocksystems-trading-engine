"""Per-session RPS feature inputs read from the remote market-data database.

This is the port of the source's `RemoteDBProvider` feature reads
(`trading/providers/remote_db.py`). It supplies the two inputs the decision
needs but the live IBKR path cannot give:

* `underlying_candles` -- the source's `feature_candles`, which it fetches
  "always at 60 minutes, whatever the replay timeframe" and then resamples to
  sessions in `_daily_frame`. `option_analytics.rps.daily_features` groups by
  session date exactly the same way, so the raw intraday rows are handed to it
  unresampled. Reading features here rather than from the strategy's own
  subscription is what makes the RPS score independent of the execution
  timeframe, which is the property the source's `build_rps_observations`
  docstring exists to protect.

* `chain_features` -- the precomputed per-session `iv_rank_252` and
  `iv_percentile_252` (plus ATM IV, realized vol, IV/RV and term structure)
  that the source reads from `index_chain_feature`. Without them the IV blend
  takes the source's short-history fallback of a neutral 50.0 and `C1_IVRANK`
  drops out of TQS; with them both match the source.

`index_event_risk_feature` is deliberately not read. The source documents that
nothing writes it and that it is empty in practice, and it is empty here too,
so the event-risk B block keeps the same neutral fallback on both sides.

**Failure is never silent.** If a remote feature source is configured and
cannot be reached, the caller raises rather than falling back to the neutral
score. A quiet fallback would enter a real position on a materially different
score, which is the divergence this port exists to remove.
"""

from datetime import date, datetime, timedelta, timezone

from django.conf import settings


class RemoteFeatureUnavailable(ValueError):
    """The configured remote feature source could not be read."""

    retryable = True


def remote_features_enabled():
    return bool(getattr(settings, "REMOTE_FEATURE_DB_URL", "") or "")


def remote_symbol(instrument):
    """The remote database's symbol for a local instrument.

    The remote feed keys NIFTY index data as `NIFTY`, while IBKR qualifies the
    same index as `NIFTY50`; nothing derives one from the other, so the mapping
    is configuration.
    """
    mapping = {
        str(key).upper(): str(value).upper()
        for key, value in (getattr(settings, "REMOTE_FEATURE_SYMBOLS", {}) or {}).items()
    }
    symbol = str(getattr(instrument, "symbol", instrument) or "").strip().upper()
    return mapping.get(symbol, symbol)


class RemoteFeatureProvider:
    """Read-only access to the remote per-session feature tables."""

    def __init__(self, *, url=None):
        self.url = url or getattr(settings, "REMOTE_FEATURE_DB_URL", "") or ""
        if not self.url:
            raise RemoteFeatureUnavailable("No remote feature database is configured")

    def _connect(self):
        import psycopg

        try:
            # Read-only and autocommit: every statement releases its AccessShare
            # lock immediately, so these reads cannot block ingestion on the
            # shared database, which is the reason the source does the same.
            return psycopg.connect(
                self.url,
                connect_timeout=int(getattr(settings, "REMOTE_FEATURE_CONNECT_TIMEOUT", 10)),
                autocommit=True,
            )
        except Exception as exc:  # noqa: BLE001 - psycopg raises several unrelated types
            raise RemoteFeatureUnavailable(f"Remote feature database is unavailable: {exc}") from exc

    def underlying_candles(self, symbol, *, sessions, before):
        """Intraday candles covering the `sessions` completed before `before`.

        Returned in `daily_features`' shape, so the caller hands them straight
        to it and the session reduction happens there, as in the source.
        """
        start = before - timedelta(days=int(sessions) * 2 + 10)
        query = (
            "SELECT observed_at, open, high, low, close, volume "
            "FROM index_underlying_candle "
            "WHERE underlying = %s AND observed_at >= %s AND observed_at < %s "
            "ORDER BY observed_at"
        )
        rows = self._query(query, (symbol, _midnight(start), _midnight(before)))
        return [{
            "window_end": observed_at,
            "open": open_, "high": high, "low": low, "close": close, "volume": volume,
        } for observed_at, open_, high, low, close, volume in rows]

    def chain_features(self, symbol, *, before):
        """The last chain-feature observation of the most recent session that
        completed strictly before `before`.

        "Strictly before the decision session" is the source's causal rule, and
        taking the latest *available* session rather than the previous calendar
        day is what makes it correct across weekends and holidays -- a Monday
        decision reads Friday's row.

        The source selects the expiry each session targets through its
        weekly/monthly cycle resolver. This runtime has no replay cycle to
        resolve, so it takes the front expiry -- the nearest one that had not
        yet expired on that session, which is what an ATM IV rank describes and
        what the source's own fallback path calls "front-expiry IV history".
        """
        query = (
            "SELECT observed_at::date, atm_iv, iv_rank_252, iv_percentile_252, "
            "  realized_volatility_20, iv_rv_ratio, term_structure_slope, "
            "  put_25d_iv, call_25d_iv "
            "FROM index_chain_feature "
            "WHERE underlying = %s AND observed_at::date < %s "
            "  AND expiry >= observed_at::date "
            "ORDER BY observed_at::date DESC, expiry ASC, observed_at DESC "
            "LIMIT 1"
        )
        rows = self._query(query, (symbol, before))
        if not rows:
            return None
        (session, atm_iv, iv_rank, iv_percentile, realized, iv_rv,
         term_structure, put_25d, call_25d) = rows[0]
        return {
            "session_date": session,
            "atm_iv": _number(atm_iv),
            "iv_rank_252": _number(iv_rank),
            "iv_percentile_252": _number(iv_percentile),
            "realized_volatility_20": _number(realized),
            "iv_rv_ratio": _number(iv_rv),
            "term_structure_slope": _number(term_structure),
            "skew_25d": (
                _number(put_25d) - _number(call_25d)
                if put_25d is not None and call_25d is not None else None
            ),
        }

    def _query(self, query, arguments):
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                cursor.execute(query, arguments)
                return cursor.fetchall()
        except RemoteFeatureUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - psycopg raises several unrelated types
            raise RemoteFeatureUnavailable(f"Remote feature query failed: {exc}") from exc


def _midnight(value):
    if isinstance(value, datetime):
        value = value.date()
    return datetime.combine(value, datetime.min.time(), tzinfo=timezone.utc)


def _number(value):
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def decision_day(stamp):
    """The decision session's calendar day, the exclusive causal bound."""
    return stamp.date() if isinstance(stamp, datetime) else stamp
