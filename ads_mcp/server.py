"""
MCP server exposing cross-platform ad performance to Claude.

  python -m ads_mcp.server                 # stdio (Claude Desktop / Claude Code)
  python -m ads_mcp.server --http 8080     # streamable HTTP (e.g. Cloud Run)
"""
from __future__ import annotations

import argparse
from functools import lru_cache

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from . import tools
from .backends import from_env

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)

server = MCPServer(
    name="ads-performance",
    instructions=(
        "Read-only access to daily paid media performance across Meta, LinkedIn, TikTok, and "
        "Microsoft Ads, plus monthly budget pacing. Call describe_data first to learn the "
        "available date range. Report ratios (CTR, CPC, CPA) exactly as returned; they are "
        "ratios of sums and should not be averaged or recomputed."
    ),
)


@lru_cache(maxsize=1)
def db():
    return from_env()


@server.tool(annotations=READ_ONLY)
def describe_data() -> dict:
    """List platforms, date coverage, campaign counts, and the valid group_by and metric options."""
    return tools.describe_data(db())


@server.tool(annotations=READ_ONLY)
def get_performance(start_date: str, end_date: str, group_by: tools.GroupBy = "platform",
                    platform: str | None = None) -> dict:
    """Spend, impressions, clicks, conversions, CTR, CPC, CPM, CVR, and CPA for a date range
    (YYYY-MM-DD, inclusive), grouped by platform, campaign, objective, audience, ad_format,
    day, week, or month. Optionally filter to one platform (meta, linkedin, tiktok, microsoft)."""
    return tools.get_performance(db(), start_date, end_date, group_by, platform)


@server.tool(annotations=READ_ONLY)
def check_pacing(month: str | None = None) -> dict:
    """Month-to-date spend vs. planned budget by platform, with pacing index, projected
    month-end spend, and over/under flags. month is YYYY-MM; defaults to the latest month."""
    return tools.check_pacing(db(), month)


@server.tool(annotations=READ_ONLY)
def flag_anomalies(start_date: str, end_date: str, metric: tools.Metric = "spend",
                   window_days: int = 28, threshold: float = 3.5) -> dict:
    """Find campaign-days where a metric deviates sharply from that campaign's own trailing
    window, comparing weekdays to weekdays and weekends to weekends (robust z-score using
    median and MAD). Returns the largest deviations first."""
    return tools.flag_anomalies(db(), start_date, end_date, metric, window_days, threshold)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--http", type=int, metavar="PORT", help="serve streamable HTTP instead of stdio")
    args = ap.parse_args()
    if args.http:
        server.run(transport="streamable-http", port=args.http)
    else:
        server.run()


if __name__ == "__main__":
    main()
