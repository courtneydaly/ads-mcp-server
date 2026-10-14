# ads-mcp-server

A read-only [Model Context Protocol](https://modelcontextprotocol.io) server that lets Claude answer questions about paid media performance and budget pacing straight from the warehouse: "How did TikTok do last week?", "Which platform is over-pacing?", "Anything weird in May?"

It sits on top of the marts built by [paid-media-pipeline](https://github.com/courtneydaly/paid-media-pipeline) and runs against either BigQuery or a local DuckDB file seeded with the same synthetic data.

> All data is synthetic. Campaigns, IDs, and budgets are fictional.

## Tools

| Tool | What it does |
|---|---|
| `describe_data` | Platforms, date coverage, campaign counts, valid options. Claude calls this first so it doesn't guess date ranges. |
| `get_performance` | Spend, impressions, clicks, conversions, CTR, CPC, CPM, CVR, CPA for a date range, grouped by platform, campaign, objective, audience, format, day, week, or month |
| `check_pacing` | Month-to-date spend vs. plan by platform, with pacing index, projected month-end spend, and over/under flags |
| `flag_anomalies` | Campaign-days that deviate sharply from that campaign's own recent history |

## Design decisions

- **Read-only by construction.** The DuckDB connection opens read-only; on BigQuery the service account only needs `bigquery.dataViewer` + `jobUser`. Every tool carries MCP's `read_only_hint`.
- **No SQL from the model.** Claude picks from allow-listed options; values go in as bound query parameters. There's no "run this SQL" tool.
- **Cost guardrail.** BigQuery queries set `maximum_bytes_billed`, so a bad date range fails instead of scanning the warehouse.
- **Errors the model can act on.** Invalid input raises the SDK's `ToolError`, so Claude sees "end_date is before start_date" and retries, rather than a generic failure.
- **Ratios of sums.** CTR/CPC/CPA are computed from summed components and returned ready to quote, so the model never averages daily ratios.
- **Anomaly detection that doesn't cry wolf.** A robust z-score (median and MAD) per campaign, with two fixes that came from testing against realistic data:
  - weekdays are compared to weekdays and weekends to weekends, because B2B and search campaigns dip every weekend
  - MAD is floored at 5% of the median, and at Poisson noise for count metrics, so "4 conversions vs. a median of 1" isn't reported as a crisis

  The synthetic data has one planted spend spike; it's the top result for spend, clicks, impressions, and conversions.

## Run locally

```bash
pip install -e ".[dev]"
python scripts/seed_duckdb.py      # builds data/ads.duckdb
pytest -q
```

### Claude Desktop

```json
{
  "mcpServers": {
    "ads-performance": {
      "command": "python",
      "args": ["-m", "ads_mcp.server"],
      "cwd": "/path/to/ads-mcp-server"
    }
  }
}
```

### Claude Code

```bash
claude mcp add ads-performance -- python -m ads_mcp.server
```

### Against BigQuery

```bash
pip install -e ".[bigquery]"
export ADS_BACKEND=bigquery BQ_PROJECT=your-project BQ_DATASET=ads_reporting
python -m ads_mcp.server
```

Or deploy the `Dockerfile` to Cloud Run (streamable HTTP on port 8080) with a service account scoped to the reporting dataset, and require authentication on the service.

## Tests

`tests/test_tools.py` seeds a fresh DuckDB file and checks that totals reconcile across groupings, ratios are ratios of sums, bad input is rejected with useful messages, pacing flags the expected platforms, the planted anomaly ranks first, weekend dips aren't flagged, and the server exposes exactly four read-only tools.
