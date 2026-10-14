import asyncio
import subprocess
import sys
from pathlib import Path

import pytest

from ads_mcp import tools
from ads_mcp.backends import DuckDBBackend

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def db(tmp_path_factory):
    path = tmp_path_factory.mktemp("data") / "ads.duckdb"
    subprocess.run([sys.executable, str(ROOT / "scripts/seed_duckdb.py"), str(path)], check=True)
    return DuckDBBackend(str(path))


def test_platform_totals_reconcile_with_daily(db):
    by_platform = tools.get_performance(db, "2026-08-01", "2026-08-31", "platform")
    by_day = tools.get_performance(db, "2026-08-01", "2026-08-31", "day")
    assert by_platform["total"]["spend"] == pytest.approx(by_day["total"]["spend"], abs=0.01)
    assert len(by_day["rows"]) == 31


def test_ratios_are_ratios_of_sums(db):
    r = tools.get_performance(db, "2026-07-01", "2026-07-31", "platform")["total"]
    assert r["cpa"] == pytest.approx(r["spend"] / r["conversions"], abs=0.01)


def test_platform_filter_is_case_insensitive(db):
    r = tools.get_performance(db, "2026-07-01", "2026-07-31", "campaign", platform="TikTok")
    assert {row["platform"] for row in r["rows"]} == {"tiktok"}


@pytest.mark.parametrize("args,msg", [
    (("2026-08-31", "2026-08-01"), "before"),
    (("2026-08-01", "not-a-date"), "YYYY-MM-DD"),
    (("2024-01-01", "2026-08-01"), "limited"),
])
def test_bad_dates_raise_tool_errors(db, args, msg):
    with pytest.raises(tools.ToolError, match=msg):
        tools.get_performance(db, *args)


def test_group_by_is_allow_listed(db):
    with pytest.raises(tools.ToolError):
        tools.get_performance(db, "2026-08-01", "2026-08-31", "spend; drop table x")


def test_pacing_defaults_to_latest_month_and_flags(db):
    p = tools.check_pacing(db)
    assert p["month"] == "2026-09"
    statuses = {r["platform"]: r["pacing_status"] for r in p["platforms"]}
    assert statuses["tiktok"] == "over" and statuses["linkedin"] == "under"
    assert any("tiktok" in f for f in p["flags"])


def test_planted_anomaly_is_top_result(db):
    a = tools.flag_anomalies(db, "2026-04-01", "2026-06-30", metric="spend")
    top = a["anomalies"][0]
    assert (top["date"], top["platform"], top["direction"]) == ("2026-05-14", "tiktok", "spike")


def test_weekends_are_not_flagged_as_drops(db):
    a = tools.flag_anomalies(db, "2026-02-01", "2026-09-18", metric="spend")
    assert a["count"] < 10  # B2B weekend dips would produce dozens without day-type comparison


def test_server_exposes_four_read_only_tools(monkeypatch, db):
    from ads_mcp import server

    monkeypatch.setattr(server, "db", lambda: db)
    listed = asyncio.run(server.server.list_tools())
    assert {t.name for t in listed} == {"describe_data", "get_performance", "check_pacing", "flag_anomalies"}
    assert all(t.annotations.read_only_hint for t in listed)
    res = asyncio.run(server.server.call_tool("check_pacing", {}))
    assert not res.is_error
