"""
Synthetic paid media data generator (vendored copy from paid-media-pipeline; keep in sync).

Produces raw tables shaped like what Airbyte lands in BigQuery for four ad
platforms (Meta, LinkedIn, TikTok, Microsoft Ads), plus a monthly budget plan.
Every number is synthetic. The advertiser ("Ridgeline Outdoor Co.") is fictional.

Guarantees (enforced by tests/test_generator.py):
  * clicks <= impressions, conversions <= clicks, spend >= 0
  * deterministic for a given --seed
  * each platform keeps its own raw quirks (nested actions, string metrics,
    JSON columns, a separate dimension table) so staging has real work to do

Usage:
  python generator/generate_ads_data.py --out data/raw
  python generator/generate_ads_data.py --out data/raw --load --project my-gcp-project
"""
from __future__ import annotations

import argparse
import calendar
import json
import math
import random
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

# ---------------------------------------------------------------------------
# Campaign plan
# ---------------------------------------------------------------------------
# Naming convention: FY{yy}_{Objective}_{Audience}_{Format}
# The staging layer parses objective / audience / format out of the name.


@dataclass(frozen=True)
class Campaign:
    platform: str
    campaign_id: str
    name: str
    daily_spend: float  # baseline spend per day
    cpm: float          # cost per 1,000 impressions
    ctr: float          # clicks / impressions
    cvr: float          # conversions / clicks


CAMPAIGNS: list[Campaign] = [
    # Meta
    Campaign("meta", "23851000000000001", "FY26_Awareness_Prospecting_Video", 180, 7.5, 0.006, 0.010),
    Campaign("meta", "23851000000000002", "FY26_Conversion_Retargeting_Carousel", 120, 14.0, 0.015, 0.045),
    Campaign("meta", "23851000000000003", "FY26_Conversion_Lookalike_Static", 140, 10.5, 0.011, 0.030),
    # LinkedIn
    Campaign("linkedin", "612000101", "FY26_LeadGen_B2BWholesale_Static", 95, 38.0, 0.0065, 0.060),
    Campaign("linkedin", "612000102", "FY26_Awareness_B2BWholesale_Video", 60, 30.0, 0.0040, 0.015),
    # TikTok
    Campaign("tiktok", "1790000000000101", "FY26_Awareness_Prospecting_Video", 110, 5.2, 0.0080, 0.006),
    Campaign("tiktok", "1790000000000102", "FY26_Conversion_Prospecting_Spark", 85, 6.8, 0.0110, 0.018),
    # Microsoft Ads
    Campaign("microsoft", "418000201", "FY26_Conversion_Brand_Search", 45, 22.0, 0.0900, 0.085),
    Campaign("microsoft", "418000202", "FY26_Conversion_NonBrand_Search", 130, 28.0, 0.0350, 0.032),
]

# Monthly budget vs. baseline run rate, by platform. Values != 1.0 create
# realistic over/under-pacing for the pacing mart to surface.
BUDGET_FACTOR = {"meta": 0.95, "linkedin": 1.10, "tiktok": 0.85, "microsoft": 1.00}

META_ACCOUNT = "100200300"
LINKEDIN_ACCOUNT = "508000700"
TIKTOK_ADVERTISER = "7100000000000000001"
MICROSOFT_ACCOUNT = "180000900"

# One planted spend spike so anomaly detection has something to find.
ANOMALY = {"campaign_id": "1790000000000102", "date": date(2026, 5, 14), "multiplier": 3.4}


def weekday_factor(d: date, platform: str) -> float:
    weekend = d.weekday() >= 5
    if platform in ("linkedin", "microsoft"):  # B2B / search dip harder on weekends
        return 0.55 if weekend else 1.08
    return 0.92 if weekend else 1.03


def season_factor(d: date) -> float:
    doy = d.timetuple().tm_yday
    return 1.0 + 0.18 * math.sin((doy - 60) / 365 * 2 * math.pi)


def binomial(rng: random.Random, n: int, p: float) -> int:
    """Binomial draw: exact for small n, normal approximation otherwise; clamped to [0, n]."""
    if n <= 0 or p <= 0:
        return 0
    p = min(p, 1.0)
    mean, var = n * p, n * p * (1 - p)
    if var < 25:
        return sum(1 for _ in range(n) if rng.random() < p)
    return max(0, min(n, round(rng.gauss(mean, math.sqrt(var)))))


def simulate_day(rng: random.Random, c: Campaign, d: date) -> dict:
    spend = c.daily_spend * weekday_factor(d, c.platform) * season_factor(d) * rng.uniform(0.85, 1.15)
    if c.campaign_id == ANOMALY["campaign_id"] and d == ANOMALY["date"]:
        spend *= ANOMALY["multiplier"]
    spend = round(spend, 2)
    impressions = int(spend / (c.cpm * rng.uniform(0.9, 1.1)) * 1000)
    clicks = binomial(rng, impressions, c.ctr * rng.uniform(0.85, 1.15))
    conversions = binomial(rng, clicks, c.cvr * rng.uniform(0.8, 1.2))
    return {"spend": spend, "impressions": impressions, "clicks": clicks, "conversions": conversions}


# ---------------------------------------------------------------------------
# Platform-specific raw shapes (approximating Airbyte source streams)
# ---------------------------------------------------------------------------

def extracted_at(d: date) -> str:
    return f"{(d + timedelta(days=1)).isoformat()}T06:00:00Z"


def meta_row(c: Campaign, d: date, m: dict, rng: random.Random) -> dict:
    leads = binomial(rng, m["conversions"], 0.3)
    purchases = m["conversions"] - leads
    return {
        "account_id": META_ACCOUNT,
        "campaign_id": c.campaign_id,
        "campaign_name": c.name,
        "date_start": d.isoformat(),
        "date_stop": d.isoformat(),
        "impressions": m["impressions"],
        "inline_link_clicks": m["clicks"],
        "spend": m["spend"],
        # Nested array: staging unnests and keeps only configured conversion actions.
        "actions": [
            {"action_type": "link_click", "value": str(m["clicks"])},
            {"action_type": "landing_page_view", "value": str(int(m["clicks"] * 0.82))},
            {"action_type": "offsite_conversion.fb_pixel_purchase", "value": str(purchases)},
            {"action_type": "offsite_conversion.fb_pixel_lead", "value": str(leads)},
        ],
        "_airbyte_extracted_at": extracted_at(d),
    }


def linkedin_row(c: Campaign, d: date, m: dict) -> dict:
    # Analytics rows carry a URN, not a name: staging joins to the campaigns stream.
    return {
        "pivotValue": f"urn:li:sponsoredCampaign:{c.campaign_id}",
        "start_date": d.isoformat(),
        "end_date": d.isoformat(),
        "impressions": m["impressions"],
        "clicks": m["clicks"],
        "costInLocalCurrency": f"{m['spend']:.2f}",
        "externalWebsiteConversions": m["conversions"],
        "_airbyte_extracted_at": extracted_at(d),
    }


def tiktok_row(c: Campaign, d: date, m: dict) -> dict:
    # TikTok's reporting API returns metrics as strings inside JSON objects.
    return {
        "campaign_id": c.campaign_id,
        "stat_time_day": f"{d.isoformat()} 00:00:00",
        "dimensions": {"campaign_id": c.campaign_id, "stat_time_day": f"{d.isoformat()} 00:00:00"},
        "metrics": {
            "campaign_name": c.name,
            "spend": f"{m['spend']:.2f}",
            "impressions": str(m["impressions"]),
            "clicks": str(m["clicks"]),
            "conversion": str(m["conversions"]),
        },
        "advertiser_id": TIKTOK_ADVERTISER,
        "_airbyte_extracted_at": extracted_at(d),
    }


def microsoft_row(c: Campaign, d: date, m: dict) -> dict:
    return {
        "AccountId": MICROSOFT_ACCOUNT,
        "CampaignId": c.campaign_id,
        "CampaignName": c.name,
        "TimePeriod": d.isoformat(),
        "Impressions": m["impressions"],
        "Clicks": m["clicks"],
        "Spend": m["spend"],
        "Conversions": float(m["conversions"]),
        "_airbyte_extracted_at": extracted_at(d),
    }


TABLES = {
    "meta": ("raw_meta", "ads_insights"),
    "linkedin": ("raw_linkedin", "ad_campaign_analytics"),
    "tiktok": ("raw_tiktok", "campaigns_reports_daily"),
    "microsoft": ("raw_microsoft", "campaign_performance_report_daily"),
}


def daterange(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def generate(start: date, end: date, seed: int = 42) -> dict[str, list[dict]]:
    rng = random.Random(seed)
    out: dict[str, list[dict]] = {p: [] for p in TABLES}
    out["linkedin_campaigns"] = []
    out["budgets"] = []
    # Keep the simulated metrics alongside raw rows so tests can check consistency.
    out["_truth"] = []

    for c in CAMPAIGNS:
        if c.platform == "linkedin":
            out["linkedin_campaigns"].append({
                "id": int(c.campaign_id),
                "name": c.name,
                "account": f"urn:li:sponsoredAccount:{LINKEDIN_ACCOUNT}",
                "status": "ACTIVE",
            })
        for d in daterange(start, end):
            m = simulate_day(rng, c, d)
            out["_truth"].append({"platform": c.platform, "campaign_id": c.campaign_id, "date": d, **m})
            if c.platform == "meta":
                out["meta"].append(meta_row(c, d, m, rng))
            elif c.platform == "linkedin":
                out["linkedin"].append(linkedin_row(c, d, m))
            elif c.platform == "tiktok":
                out["tiktok"].append(tiktok_row(c, d, m))
            else:
                out["microsoft"].append(microsoft_row(c, d, m))

    months = sorted({(d.year, d.month) for d in daterange(start, end)})
    for platform in TABLES:
        base = sum(c.daily_spend for c in CAMPAIGNS if c.platform == platform)
        for (y, mo) in months:
            days = calendar.monthrange(y, mo)[1]
            out["budgets"].append({
                "month": date(y, mo, 1).isoformat(),
                "platform": platform,
                "planned_budget": round(base * days * BUDGET_FACTOR[platform], -2),
            })
    return out


def write_ndjson(rows: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", default="2026-01-01")
    ap.add_argument("--end", default="2026-09-18")  # mid-month, so September shows live pacing
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", default="data/raw")
    ap.add_argument("--load", action="store_true", help="also load the files into BigQuery")
    ap.add_argument("--project", help="GCP project id for --load")
    ap.add_argument("--location", default="US")
    args = ap.parse_args()

    data = generate(date.fromisoformat(args.start), date.fromisoformat(args.end), args.seed)
    out = Path(args.out)
    files: list[tuple[Path, str, str]] = []
    for platform, (dataset, table) in TABLES.items():
        files.append((out / dataset / f"{table}.jsonl", dataset, table))
        write_ndjson(data[platform], files[-1][0])
    files.append((out / "raw_linkedin" / "campaigns.jsonl", "raw_linkedin", "campaigns"))
    write_ndjson(data["linkedin_campaigns"], files[-1][0])
    files.append((out / "raw_planning" / "monthly_budgets.jsonl", "raw_planning", "monthly_budgets"))
    write_ndjson(data["budgets"], files[-1][0])

    for p, _, _ in files:
        print(f"wrote {p}")

    if args.load:
        if not args.project:
            raise SystemExit("--project is required with --load")
        from load_bigquery import load_files  # local import keeps BigQuery optional

        load_files(files, project=args.project, location=args.location)


if __name__ == "__main__":
    main()
