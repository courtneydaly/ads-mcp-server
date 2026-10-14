"""
Read-only query backends. Same SQL runs on both; only the parameter
placeholder style and the client differ.

  ADS_BACKEND=duckdb    (default) local file built by scripts/seed_duckdb.py
  ADS_BACKEND=bigquery  the marts built by the paid-media-pipeline Dataform project
"""
from __future__ import annotations

import os
from datetime import date
from typing import Any, Protocol


class Backend(Protocol):
    fact: str
    pacing: str

    def p(self, name: str) -> str: ...
    def trunc(self, unit: str, col: str) -> str: ...
    def query(self, sql: str, params: dict[str, Any]) -> list[dict]: ...


class DuckDBBackend:
    def __init__(self, path: str):
        import duckdb

        if not os.path.exists(path):
            raise FileNotFoundError(f"{path} not found. Run: python scripts/seed_duckdb.py")
        self.con = duckdb.connect(path, read_only=True)
        self.fact = "fct_daily_ad_performance"
        self.pacing = "mart_monthly_pacing"

    def p(self, name: str) -> str:
        return f"${name}"

    def trunc(self, unit: str, col: str) -> str:
        return f"cast(date_trunc('{unit}', {col}) as date)"

    def query(self, sql: str, params: dict[str, Any]) -> list[dict]:
        cur = self.con.execute(sql, params)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


class BigQueryBackend:
    # Guardrail: refuse any single query that would scan more than this.
    MAX_BYTES_BILLED = 2 * 1024**3

    def __init__(self, project: str, dataset: str):
        from google.cloud import bigquery

        self.bq = bigquery
        self.client = bigquery.Client(project=project)
        self.fact = f"`{project}.{dataset}.fct_daily_ad_performance`"
        self.pacing = f"`{project}.{dataset}.mart_monthly_pacing`"

    def p(self, name: str) -> str:
        return f"@{name}"

    def trunc(self, unit: str, col: str) -> str:
        return f"date_trunc({col}, {'isoweek' if unit == 'week' else unit})"

    def query(self, sql: str, params: dict[str, Any]) -> list[dict]:
        def qp(k, v):
            if isinstance(v, date):
                return self.bq.ScalarQueryParameter(k, "DATE", v)
            if isinstance(v, float):
                return self.bq.ScalarQueryParameter(k, "FLOAT64", v)
            if isinstance(v, int):
                return self.bq.ScalarQueryParameter(k, "INT64", v)
            return self.bq.ScalarQueryParameter(k, "STRING", v)

        cfg = self.bq.QueryJobConfig(
            query_parameters=[qp(k, v) for k, v in params.items()],
            maximum_bytes_billed=self.MAX_BYTES_BILLED,
        )
        return [dict(r) for r in self.client.query(sql, job_config=cfg).result()]


def from_env() -> Backend:
    kind = os.environ.get("ADS_BACKEND", "duckdb")
    if kind == "bigquery":
        return BigQueryBackend(os.environ["BQ_PROJECT"], os.environ.get("BQ_DATASET", "ads_reporting"))
    return DuckDBBackend(os.environ.get("ADS_DUCKDB_PATH", "data/ads.duckdb"))
