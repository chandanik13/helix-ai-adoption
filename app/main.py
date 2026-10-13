"""FastAPI entrypoint for the AI-tool adoption and spend API.

Start:
    uvicorn app.main:app --port 8000

All data is loaded once at startup from the `data/` folder (relative to cwd).
The process has no outbound network calls at request time.
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Optional

# pyrefly: ignore [missing-import]
from fastapi import FastAPI, HTTPException, Query
# pyrefly: ignore [missing-import]
from fastapi.responses import FileResponse, JSONResponse
# pyrefly: ignore [missing-import]
from fastapi.staticfiles import StaticFiles

from .clean import load, CleanData
from .metrics import BadWindow, UnknownOrg, compute, idle_seats, trend, parse_iso_date

# ---------------------------------------------------------------------------
# State shared across requests
# ---------------------------------------------------------------------------
_data: CleanData | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Load CSV data once at startup."""
    global _data
    data_dir = os.environ.get("DATA_DIR", "data")
    _data = load(data_dir)
    yield
    _data = None


app = FastAPI(
    title="Helix Labs AI Tool Adoption API",
    description="Adoption and spend metrics for AI coding-tool licences.",
    version="1.0.0",
    lifespan=lifespan,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_window(from_: str | None, to: str | None) -> tuple[date, date]:
    """Parse & validate the from/to query params. Raises HTTPException on error."""
    try:
        d_from = parse_iso_date(from_)
    except (ValueError, TypeError):
        raise HTTPException(status_code=422, detail=f"'from' is not a valid YYYY-MM-DD date: {from_!r}")
    try:
        d_to = parse_iso_date(to)
    except (ValueError, TypeError):
        raise HTTPException(status_code=422, detail=f"'to' is not a valid YYYY-MM-DD date: {to!r}")
    return d_from, d_to


def _run_compute(d_from: date, d_to: date, org: str | None):
    """Call compute(), mapping domain exceptions to HTTP errors."""
    try:
        return compute(_data, d_from, d_to, org)
    except BadWindow as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except UnknownOrg as exc:
        raise HTTPException(status_code=404, detail=f"Unknown org: {exc}")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health", tags=["ops"])
def health():
    """Liveness check. Returns 200 once data is loaded."""
    if _data is None:
        raise HTTPException(status_code=503, detail="Data not loaded yet")
    return {"status": "ok"}


@app.get("/api/summary", tags=["metrics"])
def summary(
    from_: str = Query(..., alias="from", description="Start date YYYY-MM-DD (inclusive)"),
    to: str = Query(..., description="End date YYYY-MM-DD (inclusive)"),
    org: Optional[str] = Query(None, description="Organisation unit ID (optional)"),
):
    """
    Return aggregate adoption and spend for the given date window.

    - **licensed_users**: people who held ≥1 seat on ≥1 day in the window
    - **active_users**: people with qualifying usage in the window
    - **engagement_pct**: active / licensed × 100, 1 d.p.
    - **spend**: total spend for priced tools (2 d.p.)
    - **idle_spend**: spend on seats with no qualifying usage (2 d.p.)
    """
    d_from, d_to = _parse_window(from_, to)
    result_summary, _ = _run_compute(d_from, d_to, org or None)
    return result_summary


@app.get("/api/tools", tags=["metrics"])
def tools(
    from_: str = Query(..., alias="from", description="Start date YYYY-MM-DD (inclusive)"),
    to: str = Query(..., description="End date YYYY-MM-DD (inclusive)"),
    org: Optional[str] = Query(None, description="Organisation unit ID (optional)"),
):
    """
    Return per-tool adoption and spend for the given date window.

    One row per tool (A → E), always in that order.
    - **cost_per_active**: null when tool is unpriced or has no active users.
    - **spend / idle_spend**: null for unpriced tools.
    """
    d_from, d_to = _parse_window(from_, to)
    _, tool_rows = _run_compute(d_from, d_to, org or None)
    return tool_rows


@app.get("/api/idle-seats", tags=["insights"])
def idle_seats_endpoint(
    from_: str = Query(..., alias="from", description="Start date YYYY-MM-DD (inclusive)"),
    to: str = Query(..., description="End date YYYY-MM-DD (inclusive)"),
    org: Optional[str] = Query(None, description="Organisation unit ID (optional)"),
):
    """
    List of seats that were held but had no qualifying usage in the window.

    Sorted by cost descending — highest waste first.
    Only priced tools appear (Tool E is excluded as unpriced).
    """
    d_from, d_to = _parse_window(from_, to)
    try:
        return idle_seats(_data, d_from, d_to, org or None)
    except BadWindow as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except UnknownOrg as exc:
        raise HTTPException(status_code=404, detail=f"Unknown org: {exc}")


@app.get("/api/trend", tags=["insights"])
def trend_endpoint(
    from_: str = Query(..., alias="from", description="Start date YYYY-MM-DD (inclusive)"),
    to: str = Query(..., description="End date YYYY-MM-DD (inclusive)"),
    granularity: str = Query("week", pattern="^(day|week)$", description="'week' or 'day'"),
    org: Optional[str] = Query(None, description="Organisation unit ID (optional)"),
):
    """
    Trend of adoption and spend sliced into weekly (default) or daily sub-periods.

    Each item in the list is one period with the same fields as /api/summary.
    """
    d_from, d_to = _parse_window(from_, to)
    try:
        return trend(_data, d_from, d_to, granularity, org or None)
    except BadWindow as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except UnknownOrg as exc:
        raise HTTPException(status_code=404, detail=f"Unknown org: {exc}")


@app.get("/api/orgs", tags=["org"])
def orgs():
    """Return the full org hierarchy — all units with parent, name, and level."""
    return [
        {
            "org_unit_id": oid,
            "parent_id": pid,
            "name": _data.org_names.get(oid, oid),
            "level": _data.org_levels.get(oid, ""),
        }
        for oid, pid in sorted(_data.org_parent.items())
    ]


# ---------------------------------------------------------------------------
# Single-page UI at /
# ---------------------------------------------------------------------------
_STATIC_DIR = Path(__file__).parent.parent / "static"


@app.get("/", include_in_schema=False)
def ui():
    index = _STATIC_DIR / "index.html"
    if not index.exists():
        return JSONResponse({"error": "UI not found"}, status_code=404)
    return FileResponse(str(index))
