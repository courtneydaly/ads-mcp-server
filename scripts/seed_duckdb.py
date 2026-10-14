"""
Build data/ads.duckdb with the same two marts the Dataform project publishes
(fct_daily_ad_performance, mart_monthly_pacing), from synthetic data.

  python scripts/seed_duckdb.py
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).parent))
import _synthetic_ads as syn  # noqa: E402  (vendored from paid-media-pipeline)

NAMES = {c.campaign_id: c.name for c in syn.CAMPAIGNS}


def main(path: str = "data/ads.duckdb", start: str = "2026-01-01", end: str = "2026-09-18") -> None:
    data = syn.generate(date.fromisoformat(start), date.fromisoformat(end), seed=42)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).unlink(missing_ok=True)
    con = duckdb.connect(path)

    con.execute("""
        create table fct_daily_ad_performance (
          date_day date, platform varchar, campaign_id varchar, campaign_name varchar,
          objective varchar, audience varchar, ad_format varchar,
          impressions bigint, clicks bigint, spend decimal(12,2), conversions bigint)
    """)
    rows = []
    for t in data["_truth"]:
        name = NAMES[t["campaign_id"]]
        _, objective, audience, fmt = (p.lower() for p in name.split("_"))
        rows.append((t["date"], t["platform"], t["campaign_id"], name, objective, audience, fmt,
                     t["impressions"], t["clicks"], t["spend"], t["conversions"]))
    con.executemany("insert into fct_daily_ad_performance values (?,?,?,?,?,?,?,?,?,?,?)", rows)

    con.execute("create table monthly_budgets (month date, platform varchar, planned_budget decimal(12,2))")
    con.executemany("insert into monthly_budgets values (?,?,?)",
                    [(b["month"], b["platform"], b["planned_budget"]) for b in data["budgets"]])

    # Same logic as definitions/marts/mart_monthly_pacing.sqlx, in DuckDB SQL.
    con.execute("""
        create table mart_monthly_pacing as
        with as_of as (select max(date_day) as as_of_date from fct_daily_ad_performance),
        spend as (
          select cast(date_trunc('month', date_day) as date) as month, platform, sum(spend) as spend_mtd
          from fct_daily_ad_performance group by 1, 2),
        cal as (
          select b.month, b.platform, b.planned_budget, coalesce(s.spend_mtd, 0) as spend_mtd, a.as_of_date,
                 cast(datediff('day', b.month, last_day(b.month)) + 1 as integer) as days_in_month,
                 cast(case when a.as_of_date >= last_day(b.month) then datediff('day', b.month, last_day(b.month)) + 1
                      else datediff('day', b.month, a.as_of_date) + 1 end as integer) as days_elapsed
          from monthly_budgets b cross join as_of a
          left join spend s on s.month = b.month and s.platform = b.platform
          where b.month <= a.as_of_date)
        select month, platform, as_of_date, planned_budget, spend_mtd, days_elapsed, days_in_month,
               round(planned_budget * days_elapsed / days_in_month, 2) as expected_spend_mtd,
               round(spend_mtd / (planned_budget * days_elapsed / days_in_month), 3) as pacing_index,
               round(spend_mtd / days_elapsed * days_in_month, 2) as projected_month_spend,
               case when spend_mtd / (planned_budget * days_elapsed / days_in_month) > 1.10 then 'over'
                    when spend_mtd / (planned_budget * days_elapsed / days_in_month) < 0.90 then 'under'
                    else 'on_track' end as pacing_status
        from cal
    """)
    n = con.execute("select count(*) from fct_daily_ad_performance").fetchone()[0]
    con.close()
    print(f"wrote {path}: {n} fact rows")


if __name__ == "__main__":
    main(*sys.argv[1:])
