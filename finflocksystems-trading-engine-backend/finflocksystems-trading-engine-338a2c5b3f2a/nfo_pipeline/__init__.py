"""Local, batch-safe NSE index-option ingestion into remote TimescaleDB.

Deliberately does not import `.runner` at package level: `runner.py` pulls in
the IBKR client (`ibapi`), which is only installed on the ingestion host, not
on the backend server. Backend code that only needs `nfo_pipeline.config`
(the shared `TIMESCALE_DB_URL` helper) must not be forced to have `ibapi`
installed. Import `IngestionOptions`/`NFOIndexIngestion` from
`nfo_pipeline.runner` directly where actually needed.
"""
