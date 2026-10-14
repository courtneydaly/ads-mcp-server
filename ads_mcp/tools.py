"""
Tool logic, independent of MCP so it can be unit tested directly.

Every tool is read-only, uses bound parameters (never string-formatted user
input), validates its inputs against allow-lists, and caps output size.
"""
from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from typing import Literal

from mcp.server.mcpserver.exceptions import ToolError as _SDKToolError

from .backends import Backend

GroupBy = Literal["platform", "campaign", "objective", "audience", "ad_format", "day", "week", "month"]
Metric = Literal["spend", "impressions", "clicks", "conversions"]

MAX_RANGE_DAYS = 400
MAX_ROWS = 200


class ToolError(_SDKToolError):
    """Raised for bad input. Subclassing the SDK's ToolError means the message is
    returned to the model (so it can correct itself and retry) instead of being
    replaced with a generic "error executing tool"."""


def _clean(v):
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, date):
        return v.isoformat()
    return v


def _rows(rows: list[dict]) -> list[dict]:
    return [{k: _clean(v) for k, v in r.items()} for r in rows]


def _parse_range(start_date: str, end_date: str) -> tuple[date, date]:
    try:
        s, e = date.fromisoformat(start_date), date.fromisoformat(end_date)
    except ValueError as exc:
        raise ToolError("Dates must be YYYY-MM-DD.") from exc
    if e < s:
        raise ToolError("end_date is before start_date.")
    if (e - s).days > MAX_RANGE_DAYS:
        raise ToolError(f"Date range is limited to {MAX_RANGE_DAYS} days.")
    return s, e


def _ratios(r: dict) -> dict:
    """Ratios of sums, rounded for display."""
    sp, im, cl, cv = (float(r[k] or 0) for k in ("spend", "impressions", "clicks", "conversions"))
    r["ctr"] = round(cl / im, 4) if im else None
    r["cpc"] = round(sp / cl, 2) if cl else None
    r["cpm"] = round(sp / im * 1000, 2) if im else None
    r["cvr"] = round(cv / cl, 4) if cl else None
    r["cpa"] = round(sp / cv, 2) if cv else None
    r["spend"] = round(sp, 2)
    return r


def describe_data(db: Backend) -> dict:
    rows = db.query(
        f"""
        select platform, min(date_day) as first_date, max(date_day) as last_date,
               count(distinct campaign_id) as campaigns
        from {db.fact} group by platform order by platform
        """,
        {},
    )
    return {
        "platforms": _rows(rows),
        "group_by_options": list(GroupBy.__args__),
        "metrics": ["spend", "impressions", "clicks", "conversions", "ctr", "cpc", "cpm", "cvr", "cpa"],
        "notes": "Ratios are computed as ratios of sums. Spend is in account currency.",
    }


def get_performance(
    db: Backend,
    start_date: str,
    end_date: str,
    group_by: GroupBy = "platform",
    platform: str | None = None,
) -> dict:
    s, e = _parse_range(start_date, end_date)
    dims = {
        "platform": "platform",
        "campaign": "platform, campaign_name",
        "objective": "objective",
        "audience": "audience",
        "ad_format": "ad_format",
        "day": "date_day",
        "week": db.trunc("week", "date_day"),
        "month": db.trunc("month", "date_day"),
    }
    if group_by not in dims:
        raise ToolError(f"group_by must be one of {list(dims)}")
    params: dict = {"start": s, "end": e}
    where = f"date_day between {db.p('start')} and {db.p('end')}"
    if platform:
        where += f" and platform = {db.p('platform')}"
        params["platform"] = platform.lower()

    dim = dims[group_by]
    select_dim = dim if group_by in ("platform", "campaign", "objective", "audience", "ad_format") else f"{dim} as period"
    group_cols = "platform, campaign_name" if group_by == "campaign" else ("period" if select_dim.endswith("period") else dim)
    rows = db.query(
        f"""
        select {select_dim},
               sum(spend) as spend, sum(impressions) as impressions,
               sum(clicks) as clicks, sum(conversions) as conversions
        from {db.fact}
        where {where}
        group by {group_cols}
        order by {"period" if "period" in select_dim else "spend desc"}
        limit {MAX_ROWS + 1}
        """,
        params,
    )
    truncated = len(rows) > MAX_ROWS
    rows = [_ratios(r) for r in _rows(rows[:MAX_ROWS])]
    total = _ratios({k: sum(r[k] or 0 for r in rows) for k in ("spend", "impressions", "clicks", "conversions")})
    return {"start_date": s.isoformat(), "end_date": e.isoformat(), "group_by": group_by,
            "rows": rows, "total": total, "truncated": truncated}


def check_pacing(db: Backend, month: str | None = None) -> dict:
    if month:
        try:
            m = date.fromisoformat(f"{month}-01") if len(month) == 7 else date.fromisoformat(month).replace(day=1)
        except ValueError as exc:
            raise ToolError("month must be YYYY-MM.") from exc
    else:
        latest = db.query(f"select max(month) as m from {db.pacing}", {})
        m = latest[0]["m"]
        if m is None:
            raise ToolError("No pacing data available.")
    rows = _rows(db.query(
        f"""
        select platform, as_of_date, planned_budget, spend_mtd, expected_spend_mtd,
               pacing_index, projected_month_spend, pacing_status, days_elapsed, days_in_month
        from {db.pacing}
        where month = {db.p('month')}
        order by pacing_index desc
        """,
        {"month": m},
    ))
    if not rows:
        raise ToolError(f"No pacing rows for {m:%Y-%m}.")
    return {
        "month": m.isoformat()[:7],
        "as_of_date": rows[0]["as_of_date"],
        "platforms": rows,
        "flags": [f"{r['platform']} is {r['pacing_status']} (index {r['pacing_index']})"
                  for r in rows if r["pacing_status"] != "on_track"],
    }


def flag_anomalies(
    db: Backend,
    start_date: str,
    end_date: str,
    metric: Metric = "spend",
    window_days: int = 28,
    threshold: float = 3.5,
) -> dict:
    """Robust z-score (median / MAD) of each campaign-day vs. the same day type
    (weekday or weekend) in that campaign's own trailing window."""
    if metric not in Metric.__args__:
        raise ToolError(f"metric must be one of {list(Metric.__args__)}")
    if not 7 <= window_days <= 90:
        raise ToolError("window_days must be between 7 and 90.")
    s, e = _parse_range(start_date, end_date)
    lookback = s - timedelta(days=window_days)
    rows = db.query(
        f"""
        select platform, campaign_id, campaign_name, date_day, sum({metric}) as value
        from {db.fact}
        where date_day between {db.p('lookback')} and {db.p('end')}
        group by platform, campaign_id, campaign_name, date_day
        order by date_day
        """,
        {"lookback": lookback, "end": e},
    )
    series: dict[tuple, list[tuple[date, float]]] = defaultdict(list)
    for r in rows:
        d = r["date_day"] if isinstance(r["date_day"], date) else date.fromisoformat(str(r["date_day"]))
        series[(r["platform"], r["campaign_id"], r["campaign_name"])].append((d, float(r["value"] or 0)))

    flags = []
    for (platform, cid, name), pts in series.items():
        for i, (d, v) in enumerate(pts):
            if d < s:
                continue
            # Compare weekdays with weekdays and weekends with weekends: B2B and search
            # campaigns spend far less on weekends, which isn't an anomaly.
            hist = [x for dd, x in pts[:i]
                    if dd >= d - timedelta(days=window_days) and (dd.weekday() >= 5) == (d.weekday() >= 5)]
            if len(hist) < 6:
                continue
            med = statistics.median(hist)
            mad = statistics.median(abs(x - med) for x in hist)
            # Small windows can cluster tightly and make MAD near zero, which turns
            # ordinary noise into huge z-scores. Floor it at 5% of the median, and for
            # count metrics at the Poisson noise level (sd ~ sqrt(mean); MAD ~ 0.6745 sd),
            # so "4 conversions vs. a median of 1" isn't reported as a crisis.
            floor = 0.05 * abs(med)
            if metric != "spend":
                floor = max(floor, 0.6745 * (max(med, 1.0) ** 0.5))
            mad = max(mad, floor)
            if mad == 0:
                continue
            z = 0.6745 * (v - med) / mad
            if abs(z) >= threshold:
                flags.append({
                    "date": d.isoformat(), "platform": platform, "campaign_id": cid,
                    "campaign_name": name, "metric": metric, "value": round(v, 2),
                    "trailing_median": round(med, 2), "robust_z": round(z, 1),
                    "direction": "spike" if z > 0 else "drop",
                })
    flags.sort(key=lambda f: -abs(f["robust_z"]))
    return {"metric": metric, "window_days": window_days, "threshold": threshold,
            "anomalies": flags[:50], "count": len(flags)}
