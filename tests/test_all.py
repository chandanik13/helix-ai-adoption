"""Tests for the metrics engine and API.

These tests run without any network access and use only the mini fixtures
defined here (not the real data/ folder), so they will pass on any machine
after:  pip install -r requirements.txt pytest httpx
"""
from __future__ import annotations

import io
import csv
from datetime import date, timedelta
from fractions import Fraction
from pathlib import Path
import tempfile, os

# pyrefly: ignore [missing-import]
import pytest

# ---------------------------------------------------------------------------
# Helpers to build minimal CleanData objects without touching real CSVs
# ---------------------------------------------------------------------------
from app.clean import CleanData, Seat, load, parse_date, parse_event_ts, norm_tool
from app.metrics import BadWindow, UnknownOrg, compute, idle_seats, trend, parse_iso_date


def _ordinal(d: date) -> int:
    return d.toordinal()


def _seat(uid, tool, granted: date, revoked: date | None = None, price: float | None = None):
    end = revoked.toordinal() if revoked else None
    p = Fraction(price).limit_denominator(10000) if price is not None else None
    return Seat(uid, tool, [(_ordinal(granted), end, p)])


def _events(uid, tool, *days: date):
    return {(uid, tool): sorted(d.toordinal() for d in days)}


def _people(*user_ids) -> dict:
    return {uid: None for uid in user_ids}


def _clean(seats, events, people=None, priced_tools=None):
    if people is None:
        people = {s.user_id: None for s in seats}
    if priced_tools is None:
        priced_tools = frozenset(s.tool for s in seats if any(p is not None for _, _, p in s.intervals))
    orgs: dict = {}
    return CleanData(
        people=people,
        people_names={uid: uid for uid in people},  # name = id in tests
        org_parent=orgs,
        org_names={},
        org_levels={},
        org_users={},
        seats=seats,
        priced_tools=priced_tools,
        events=events,
        report={},
    )


# ===========================================================================
# 1. Worked example from Appendix P
# ===========================================================================
class TestWorkedExample:
    """
    Window: 2026-07-01 to 2026-07-30 (30 days).
    P1 tool A (price $19, granted 2026-03-01, active)    → 30 days held, used
    P1 tool C (price $30, granted 2026-07-21, active)    → 10 days held, NOT used
    P2 tool A (price $19, revoked  2026-07-16, active)   → 15 days held, NOT used
    Expected: licensed=2, active=1, engagement=50.0%, spend=38.50, idle=19.50
    """

    d_from = date(2026, 7, 1)
    d_to   = date(2026, 7, 30)

    @pytest.fixture
    def data(self):
        seats = [
            _seat("P1", "A", date(2026, 3, 1),  None,           19),
            _seat("P1", "C", date(2026, 7, 21), None,           30),
            _seat("P2", "A", date(2026, 3, 1),  date(2026, 7, 16), 19),
        ]
        events = _events("P1", "A", date(2026, 7, 5))   # P1 used Tool A once
        return _clean(seats, events, priced_tools=frozenset({"A", "C"}))

    def test_summary_licensed(self, data):
        s, _ = compute(data, self.d_from, self.d_to)
        assert s["licensed_users"] == 2

    def test_summary_active(self, data):
        s, _ = compute(data, self.d_from, self.d_to)
        assert s["active_users"] == 1

    def test_summary_engagement(self, data):
        s, _ = compute(data, self.d_from, self.d_to)
        assert s["engagement_pct"] == 50.0

    def test_summary_spend(self, data):
        s, _ = compute(data, self.d_from, self.d_to)
        assert s["spend"] == 38.50

    def test_summary_idle_spend(self, data):
        s, _ = compute(data, self.d_from, self.d_to)
        assert s["idle_spend"] == 19.50


# ===========================================================================
# 2. Seat boundary semantics (revoked_on is exclusive)
# ===========================================================================
class TestSeatBoundaries:

    def test_revoked_on_is_exclusive(self):
        """User holds seat 2026-07-01 to 2026-07-15 (revoked on 16th).
        Window 2026-07-15 to 2026-07-15 → 1 day held."""
        seat = _seat("U1", "A", date(2026, 7, 1), date(2026, 7, 16), 19)
        data = _clean([seat], {}, priced_tools=frozenset({"A"}))
        s, _ = compute(data, date(2026, 7, 15), date(2026, 7, 15))
        assert s["licensed_users"] == 1
        # spend = 1 day * 19 / 30
        assert s["spend"] == round(19 / 30, 2)

    def test_revoked_on_day_not_charged(self):
        """The revoked_on day itself should NOT be held."""
        seat = _seat("U1", "A", date(2026, 7, 1), date(2026, 7, 16), 19)
        data = _clean([seat], {}, priced_tools=frozenset({"A"}))
        s, _ = compute(data, date(2026, 7, 16), date(2026, 7, 20))
        assert s["licensed_users"] == 0

    def test_active_seat_charges_all_window_days(self):
        """Blank revoked_on → seat held through entire 30-day window."""
        seat = _seat("U1", "A", date(2026, 1, 1), None, 19)
        data = _clean([seat], {}, priced_tools=frozenset({"A"}))
        s, _ = compute(data, date(2026, 7, 1), date(2026, 7, 30))
        assert s["spend"] == round(19 * 30 / 30, 2)   # exactly 19.00


# ===========================================================================
# 3. Usage-counting rules
# ===========================================================================
class TestUsageCounting:

    def test_activity_without_seat_not_counted(self):
        """Event on day when seat is NOT held → user not active."""
        seat = _seat("U1", "A", date(2026, 7, 10), None, 19)
        events = _events("U1", "A", date(2026, 7, 5))   # before seat
        data = _clean([seat], events, priced_tools=frozenset({"A"}))
        s, _ = compute(data, date(2026, 7, 1), date(2026, 7, 31))
        assert s["active_users"] == 0

    def test_activity_with_seat_counted(self):
        seat = _seat("U1", "A", date(2026, 7, 1), None, 19)
        events = _events("U1", "A", date(2026, 7, 5))
        data = _clean([seat], events, priced_tools=frozenset({"A"}))
        s, _ = compute(data, date(2026, 7, 1), date(2026, 7, 31))
        assert s["active_users"] == 1

    def test_activity_outside_window_not_counted(self):
        seat = _seat("U1", "A", date(2026, 6, 1), None, 19)
        events = _events("U1", "A", date(2026, 6, 15))   # outside window
        data = _clean([seat], events, priced_tools=frozenset({"A"}))
        s, _ = compute(data, date(2026, 7, 1), date(2026, 7, 31))
        assert s["active_users"] == 0


# ===========================================================================
# 4. Unpriced tools
# ===========================================================================
class TestUnpricedTools:

    def test_unpriced_tool_spend_is_null(self):
        seat = _seat("U1", "E", date(2026, 7, 1), None, None)   # no price
        data = _clean([seat], {}, priced_tools=frozenset())
        _, rows = compute(data, date(2026, 7, 1), date(2026, 7, 31))
        row_e = next(r for r in rows if r["tool"] == "E")
        assert row_e["spend"] is None
        assert row_e["idle_spend"] is None
        assert row_e["cost_per_active"] is None

    def test_unpriced_tool_still_counts_users(self):
        seat = _seat("U1", "E", date(2026, 7, 1), None, None)
        data = _clean([seat], {}, priced_tools=frozenset())
        _, rows = compute(data, date(2026, 7, 1), date(2026, 7, 31))
        row_e = next(r for r in rows if r["tool"] == "E")
        assert row_e["licensed_users"] == 1


# ===========================================================================
# 5. Error conditions
# ===========================================================================
class TestErrors:

    def test_bad_window_raises(self):
        data = _clean([], {})
        with pytest.raises(BadWindow):
            compute(data, date(2026, 7, 31), date(2026, 7, 1))

    def test_unknown_org_raises(self):
        data = _clean([], {})
        with pytest.raises(UnknownOrg):
            compute(data, date(2026, 7, 1), date(2026, 7, 31), org="NOTEXIST")

    def test_parse_iso_date_invalid(self):
        with pytest.raises(ValueError):
            parse_iso_date("not-a-date")
        with pytest.raises(ValueError):
            parse_iso_date("2026-13-01")   # invalid month
        with pytest.raises(ValueError):
            parse_iso_date(None)

    def test_parse_iso_date_valid(self):
        assert parse_iso_date("2026-07-01") == date(2026, 7, 1)


# ===========================================================================
# 6. Tools always returned A–E in order
# ===========================================================================
def test_tools_always_five_in_order():
    data = _clean([], {})
    _, rows = compute(data, date(2026, 7, 1), date(2026, 7, 31))
    assert [r["tool"] for r in rows] == ["A", "B", "C", "D", "E"]


# ===========================================================================
# 7. Engagement % edge cases
# ===========================================================================
def test_engagement_zero_when_no_licensed():
    data = _clean([], {})
    s, _ = compute(data, date(2026, 7, 1), date(2026, 7, 31))
    assert s["engagement_pct"] == 0.0


def test_engagement_100_when_all_active():
    seat = _seat("U1", "A", date(2026, 7, 1), None, 19)
    events = _events("U1", "A", date(2026, 7, 5))
    data = _clean([seat], events, priced_tools=frozenset({"A"}))
    s, _ = compute(data, date(2026, 7, 1), date(2026, 7, 31))
    assert s["engagement_pct"] == 100.0


# ===========================================================================
# 8. Date/timestamp parsers
# ===========================================================================
class TestParsers:

    def test_parse_date_iso(self):
        assert parse_date("2026-07-01") == date(2026, 7, 1)

    def test_parse_date_slash(self):
        assert parse_date("2026/07/01") == date(2026, 7, 1)

    def test_parse_date_dmy(self):
        assert parse_date("01-07-2026") == date(2026, 7, 1)

    def test_parse_date_month_name(self):
        assert parse_date("01-Jul-2026") == date(2026, 7, 1)

    def test_parse_date_blank_returns_none(self):
        assert parse_date("") is None
        assert parse_date(None) is None

    def test_parse_event_ts_naive(self):
        assert parse_event_ts("2026-07-01 09:00:44") == date(2026, 7, 1)

    def test_parse_event_ts_utc_shifts(self):
        # midnight UTC = 05:30 IST, same day
        assert parse_event_ts("2026-07-01T00:00:00Z") == date(2026, 7, 1)

    def test_parse_event_ts_late_utc_shifts_day(self):
        # 19:00 UTC = 00:30 IST next day
        assert parse_event_ts("2026-07-01T19:00:00Z") == date(2026, 7, 2)

    def test_norm_tool_variants(self):
        assert norm_tool("a") == "A"
        assert norm_tool("Tool A") == "A"
        assert norm_tool("TOOL_A") == "A"
        assert norm_tool("Z") is None


# ===========================================================================
# 9. org filtering (integration via full load from tiny temp CSV files)
# ===========================================================================
def _write_csv(path: Path, rows: list[dict]):
    if not rows:
        return
    path.write_text(
        ",".join(rows[0].keys()) + "\n" +
        "\n".join(",".join(str(v) for v in r.values()) for r in rows),
        encoding="utf-8"
    )


@pytest.fixture(scope="module")
def tiny_data(tmp_path_factory):
    d = tmp_path_factory.mktemp("data")
    _write_csv(d / "org_units.csv", [
        {"org_unit_id": "ROOT", "name": "Root", "parent_id": "",  "level": "L3"},
        {"org_unit_id": "DEPT", "name": "Dept", "parent_id": "ROOT", "level": "L4"},
        {"org_unit_id": "TEAM", "name": "Team", "parent_id": "DEPT", "level": "L5"},
    ])
    _write_csv(d / "people.csv", [
        {"user_id": "U1", "name": "Alice", "org_unit_id": "TEAM", "role": "dev", "location": "x"},
        {"user_id": "U2", "name": "Bob",   "org_unit_id": "ROOT", "role": "dev", "location": "x"},
    ])
    _write_csv(d / "price_list.csv", [
        {"tool": "A", "tier": "standard", "monthly_price_usd": "19"},
        {"tool": "B", "tier": "standard", "monthly_price_usd": ""},
        {"tool": "C", "tier": "standard", "monthly_price_usd": ""},
        {"tool": "D", "tier": "standard", "monthly_price_usd": ""},
        {"tool": "E", "tier": "standard", "monthly_price_usd": ""},
    ])
    _write_csv(d / "seats.csv", [
        {"user_id": "U1", "tool": "A", "tier": "standard", "granted_on": "2026-01-01", "revoked_on": ""},
        {"user_id": "U2", "tool": "A", "tier": "standard", "granted_on": "2026-01-01", "revoked_on": ""},
    ])
    _write_csv(d / "activity_events.csv", [
        {"user_id": "U1", "tool": "A", "event_type": "chat", "event_ts": "2026-07-15 10:00:00"},
    ])
    return load(str(d))


def test_org_filter_team_only(tiny_data):
    s, _ = compute(tiny_data, date(2026, 7, 1), date(2026, 7, 31), org="TEAM")
    assert s["licensed_users"] == 1   # only U1
    assert s["active_users"] == 1


def test_org_filter_root_includes_all(tiny_data):
    s, _ = compute(tiny_data, date(2026, 7, 1), date(2026, 7, 31), org="ROOT")
    assert s["licensed_users"] == 2   # U1 + U2


def test_org_filter_unknown_raises(tiny_data):
    with pytest.raises(UnknownOrg):
        compute(tiny_data, date(2026, 7, 1), date(2026, 7, 31), org="GHOST")


def test_no_org_covers_all(tiny_data):
    s, _ = compute(tiny_data, date(2026, 7, 1), date(2026, 7, 31))
    assert s["licensed_users"] == 2


# ===========================================================================
# 10. API endpoint tests (no network — uses FastAPI TestClient)
# ===========================================================================
# pyrefly: ignore [missing-import]
from fastapi.testclient import TestClient
from unittest.mock import patch


@pytest.fixture(scope="module")
def client(tiny_data):
    from app.main import app, lifespan
    with patch("app.main.load", return_value=tiny_data):
        with TestClient(app) as c:
            yield c


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_summary_ok(client):
    r = client.get("/api/summary?from=2026-07-01&to=2026-07-31")
    assert r.status_code == 200
    body = r.json()
    assert "licensed_users" in body
    assert "active_users" in body
    assert "engagement_pct" in body
    assert "spend" in body
    assert "idle_spend" in body


def test_summary_unknown_org_404(client):
    r = client.get("/api/summary?from=2026-07-01&to=2026-07-31&org=GHOST")
    assert r.status_code == 404


def test_summary_from_after_to_4xx(client):
    r = client.get("/api/summary?from=2026-07-31&to=2026-07-01")
    assert r.status_code in (400, 422)


def test_summary_bad_date_4xx(client):
    r = client.get("/api/summary?from=not-a-date&to=2026-07-31")
    assert r.status_code in (400, 422)


def test_summary_bad_to_date_4xx(client):
    r = client.get("/api/summary?from=2026-07-01&to=2026-13-01")
    assert r.status_code in (400, 422)


def test_tools_ok(client):
    r = client.get("/api/tools?from=2026-07-01&to=2026-07-31")
    assert r.status_code == 200
    rows = r.json()
    assert isinstance(rows, list)
    assert len(rows) == 5
    assert [row["tool"] for row in rows] == ["A", "B", "C", "D", "E"]


def test_tools_unknown_org_404(client):
    r = client.get("/api/tools?from=2026-07-01&to=2026-07-31&org=GHOST")
    assert r.status_code == 404


def test_tools_each_row_has_required_fields(client):
    r = client.get("/api/tools?from=2026-07-01&to=2026-07-31")
    for row in r.json():
        for field in ("tool","licensed_users","active_users","engagement_pct","spend","idle_spend","cost_per_active"):
            assert field in row, f"Missing field {field!r} in row {row}"


def test_ui_returns_200(client):
    r = client.get("/")
    assert r.status_code == 200


# ===========================================================================
# 11. renewal_risk field in /api/tools
# ===========================================================================
def test_renewal_risk_present_in_tools(client):
    r = client.get("/api/tools?from=2026-07-01&to=2026-07-31")
    assert r.status_code == 200
    for row in r.json():
        assert "renewal_risk" in row, f"Missing renewal_risk in {row}"
        assert row["renewal_risk"] in ("high", "medium", "low", "none")


def test_renewal_risk_logic():
    """Tool with 0 of 2 licensed users active → engagement 0% → high risk."""
    seats = [
        _seat("U1", "A", date(2026, 7, 1), None, 19),
        _seat("U2", "A", date(2026, 7, 1), None, 19),
    ]
    data = _clean(seats, {}, priced_tools=frozenset({"A"}))
    _, rows = compute(data, date(2026, 7, 1), date(2026, 7, 31))
    row_a = next(r for r in rows if r["tool"] == "A")
    assert row_a["renewal_risk"] == "high"


def test_renewal_risk_low_when_fully_active():
    seat = _seat("U1", "A", date(2026, 7, 1), None, 19)
    events = _events("U1", "A", date(2026, 7, 5))
    data = _clean([seat], events, priced_tools=frozenset({"A"}))
    _, rows = compute(data, date(2026, 7, 1), date(2026, 7, 31))
    row_a = next(r for r in rows if r["tool"] == "A")
    assert row_a["renewal_risk"] == "low"


# ===========================================================================
# 12. /api/idle-seats endpoint
# ===========================================================================
def test_idle_seats_returns_list(client):
    r = client.get("/api/idle-seats?from=2026-07-01&to=2026-07-31")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_idle_seats_has_required_fields(client):
    r = client.get("/api/idle-seats?from=2026-07-01&to=2026-07-31")
    assert r.status_code == 200
    for item in r.json():
        for field in ("user_id", "name", "tool", "granted_on", "days_held", "cost"):
            assert field in item


def test_idle_seats_sorted_by_cost_desc(client):
    r = client.get("/api/idle-seats?from=2026-07-01&to=2026-07-31")
    costs = [item["cost"] for item in r.json()]
    assert costs == sorted(costs, reverse=True)


def test_idle_seats_unknown_org_404(client):
    r = client.get("/api/idle-seats?from=2026-07-01&to=2026-07-31&org=GHOST")
    assert r.status_code == 404


def test_idle_seats_bad_window_4xx(client):
    r = client.get("/api/idle-seats?from=2026-07-31&to=2026-07-01")
    assert r.status_code in (400, 422)


def test_idle_seats_logic():
    """U1 has Tool A seat but no usage → should appear in idle seats."""
    seat = _seat("U1", "A", date(2026, 7, 1), None, 19)
    data = _clean([seat], {})
    results = idle_seats(data, date(2026, 7, 1), date(2026, 7, 30))
    assert len(results) == 1
    assert results[0]["user_id"] == "U1"
    # 30 days * 19 / 30 = 19.00
    assert results[0]["cost"] == 19.00


def test_idle_seats_active_user_excluded():
    """U1 uses the tool on day 5 → should NOT appear in idle seats."""
    seat = _seat("U1", "A", date(2026, 7, 1), None, 19)
    events = _events("U1", "A", date(2026, 7, 5))
    data = _clean([seat], events)
    results = idle_seats(data, date(2026, 7, 1), date(2026, 7, 30))
    assert len(results) == 0


def test_idle_seats_unpriced_tool_excluded():
    """Tool E has no price → should NOT appear even if idle."""
    seat = _seat("U1", "E", date(2026, 7, 1), None, None)
    data = _clean([seat], {}, priced_tools=frozenset())
    results = idle_seats(data, date(2026, 7, 1), date(2026, 7, 30))
    assert len(results) == 0


# ===========================================================================
# 13. /api/trend endpoint
# ===========================================================================
def test_trend_returns_list(client):
    r = client.get("/api/trend?from=2026-07-01&to=2026-07-28")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_trend_week_returns_4_periods(client):
    r = client.get("/api/trend?from=2026-07-01&to=2026-07-28&granularity=week")
    assert r.status_code == 200
    assert len(r.json()) == 4


def test_trend_day_returns_correct_count(client):
    r = client.get("/api/trend?from=2026-07-01&to=2026-07-07&granularity=day")
    assert r.status_code == 200
    assert len(r.json()) == 7


def test_trend_period_field_present(client):
    r = client.get("/api/trend?from=2026-07-01&to=2026-07-07&granularity=day")
    periods = [item["period"] for item in r.json()]
    assert periods[0] == "2026-07-01"
    assert periods[-1] == "2026-07-07"


def test_trend_has_summary_fields(client):
    r = client.get("/api/trend?from=2026-07-01&to=2026-07-07")
    for item in r.json():
        for field in ("period", "licensed_users", "active_users", "engagement_pct", "spend", "idle_spend"):
            assert field in item


def test_trend_invalid_granularity_4xx(client):
    r = client.get("/api/trend?from=2026-07-01&to=2026-07-31&granularity=month")
    assert r.status_code == 422


def test_trend_unknown_org_404(client):
    r = client.get("/api/trend?from=2026-07-01&to=2026-07-31&org=GHOST")
    assert r.status_code == 404


def test_trend_logic():
    """U1 is active on 2026-07-05 — week 1 should show active=1, week 2 active=0."""
    seat = _seat("U1", "A", date(2026, 7, 1), None, 19)
    events = _events("U1", "A", date(2026, 7, 5))  # week 1
    data = _clean([seat], events)
    results = trend(data, date(2026, 7, 1), date(2026, 7, 14), granularity="week")
    assert len(results) == 2
    assert results[0]["active_users"] == 1
    assert results[1]["active_users"] == 0


# ===========================================================================
# 14. /api/orgs endpoint
# ===========================================================================
def test_orgs_returns_list(client):
    r = client.get("/api/orgs")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_orgs_has_required_fields(client):
    r = client.get("/api/orgs")
    for item in r.json():
        for field in ("org_unit_id", "parent_id", "name", "level"):
            assert field in item


def test_orgs_integration(tiny_data):
    """Verify org data matches what was written in the tiny fixture."""
    assert "ROOT" in tiny_data.org_names
    assert tiny_data.org_names["ROOT"] == "Root"
    assert tiny_data.org_levels["ROOT"] == "L3"
    assert tiny_data.org_levels["DEPT"] == "L4"
    assert tiny_data.org_parent["DEPT"] == "ROOT"
