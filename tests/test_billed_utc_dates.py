"""Ask 2026-11-27-tcp-s2z5-2: since 2026-11-26 the financial dashboard sends consumption dates in UTC with Z. A
pull reads each stamp in Berlin time, so 23:00Z of the day before is the first hour of the day it bills. Invented
rows only, the fixtures of tests/test_billed.py."""
import json

import pytest

from awb.tcp import billed
from tests.test_billed import cache, env, hourly, row  # noqa: F401  (env is a fixture)


def test_hourly_pull_takes_utc_stamps_and_stores_them_in_berlin_time(env):
    env.rows = [hourly("00:00:00", "1") | {"consumption_date": "2026-11-16T23:00:00Z"},
                hourly("01:00:00", "1") | {"consumption_date": "2026-11-17T00:00:00Z"}]
    data = billed.pull_hourly(env.p, "test-1", "2026-11-17")
    starts = [e["start"] for e in data["resources"]["vm-1"]["compute-small"]]
    assert starts == ["2026-11-17T00:00:00+01:00", "2026-11-17T01:00:00+01:00"]
    assert data["rows"] == 2


def test_monthly_pull_counts_a_utc_stamp_on_its_berlin_day(env):
    env.rows = [row(consumption_date="2026-10-31T23:00:00Z"), row(consumption_date="2026-11-17")]
    data = billed.pull(env.p, "test-1", "2026-11")
    assert sorted(json.loads(cache(env).read_text())["days"]) == ["2026-11-01", "2026-11-17"]
    assert data["rows"] == 2


@pytest.mark.parametrize("stamp", ["2026-10-31T22:00:00Z", "2026-11-17T10:00:00"])
def test_a_stamp_outside_the_month_or_without_an_offset_is_still_broken_data(env, stamp):
    env.rows = [row(consumption_date=stamp)]
    with pytest.raises(billed.BilledError):
        billed.pull(env.p, "test-1", "2026-11")
