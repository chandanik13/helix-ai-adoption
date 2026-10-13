"""Load and clean the five raw exports (standard library only).

Everything here is rule-based, never row-specific, because the grading data is a
different export from the same systems. Every row that is dropped or repaired is
counted in ``CleanData.report`` so the findings can be quoted in DATA.md.

Public API
    load(data_dir) -> CleanData
    parse_date(text) / parse_event_ts(text)   (also used by the tests)

CLI
    python -m app.clean data        # prints the cleaning report as JSON
"""
from __future__ import annotations

import csv
import io
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from pathlib import Path

# --------------------------------------------------------------------------- #
# Decisions (each one is written up in DATA.md)
# --------------------------------------------------------------------------- #
TOOLS = ("A", "B", "C", "D", "E")

# Only these event types are "use of a tool". Anything else (login, error,
# heartbeat, licence events, ...) is not usage. Unknown types are excluded and
# listed in the report so they can be reviewed.
USAGE_EVENT_TYPES = frozenset({"chat", "completion", "agent"})

# event_ts is local time. All three Helix Labs sites (Bengaluru, Hyderabad, Pune)
# are IST, UTC+05:30, no DST. A naive timestamp is taken as written. A timestamp
# with an explicit offset / Z is converted to this zone before taking its date.
LOCAL_TZ = timezone(timedelta(hours=5, minutes=30))


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #
class Report:
    """Counts what was dropped / repaired, with up to 3 examples per key."""

    def __init__(self) -> None:
        self.counts: Counter[str] = Counter()
        self.samples: dict[str, list[str]] = defaultdict(list)

    def add(self, key: str, sample: str | None = None) -> None:
        self.counts[key] += 1
        if sample is not None and len(self.samples[key]) < 3 and sample not in self.samples[key]:
            self.samples[key].append(sample)

    def as_dict(self) -> dict:
        return {"counts": dict(sorted(self.counts.items())), "samples": dict(self.samples)}


# --------------------------------------------------------------------------- #
# Parsers / normalisers
# --------------------------------------------------------------------------- #
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def _mk(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def parse_date(text: str | None, report: Report | None = None) -> date | None:
    """Parse a calendar date. Returns None when it cannot be read.

    Accepts YYYY-MM-DD (also with / or .), DD-MM-YYYY style, 01-Jul-2026,
    YYYYMMDD, and ignores any trailing time part. For nn/nn/yyyy the larger
    number decides which is the day; if both are <= 12 it is read day-first.
    """
    s = (text or "").strip()
    if not s:
        return None
    m = re.fullmatch(r"([0-9]{4})[-/.]([0-9]{1,2})[-/.]([0-9]{1,2})(?:[T\s].*)?", s)
    if m:
        return _mk(*map(int, m.groups()))
    m = re.fullmatch(r"([0-9]{1,2})[-/.]([0-9]{1,2})[-/.]([0-9]{4})(?:[T\s].*)?", s)
    if m:
        a, b, y = map(int, m.groups())
        if a > 12:
            day, mon = a, b
        elif b > 12:
            mon, day = a, b
        else:
            day, mon = a, b
            if a != b and report is not None:
                report.add("dates_ambiguous_read_day_first", s)
        return _mk(y, mon, day)
    m = re.fullmatch(r"([0-9]{1,2})[-\s/]([A-Za-z]{3})[A-Za-z]*[-\s/,]*([0-9]{4})", s)
    if m and m.group(2).lower() in _MONTHS:
        return _mk(int(m.group(3)), _MONTHS[m.group(2).lower()], int(m.group(1)))
    m = re.fullmatch(r"([0-9]{4})([0-9]{2})([0-9]{2})", s)
    if m:
        return _mk(*map(int, m.groups()))
    return None


_TS_RE = re.compile(
    r"^(?P<date>\S+?)"
    r"(?:[T\s]+(?P<h>[0-9]{1,2}):(?P<mi>[0-9]{2})(?::(?P<s>[0-9]{2})(?:[.,][0-9]+)?)?)?"
    r"\s*(?P<ap>[AaPp][Mm])?\s*"
    r"(?P<tz>Z|UTC|GMT|[+-][0-9]{2}(?::?[0-9]{2})?)?$",
    re.IGNORECASE,
)


def parse_event_ts(text: str | None, report: Report | None = None) -> date | None:
    """Return the *local* calendar date of an event timestamp, or None.

    - "2026-07-01 09:00:44" / "2026-07-01T09:00:44"  -> date as written
    - with Z / UTC / +05:30 / -0500                  -> converted to LOCAL_TZ first
    - 10- or 13-digit epoch                           -> UTC, converted to LOCAL_TZ
    - 12-hour clocks ("09:00 PM") and fractional seconds are understood
    """
    s = (text or "").strip()
    if not s:
        return None
    if re.fullmatch(r"[0-9]{10}(?:\.[0-9]+)?|[0-9]{13}", s):
        secs = float(s) / 1000 if len(s) == 13 else float(s)
        return datetime.fromtimestamp(secs, tz=timezone.utc).astimezone(LOCAL_TZ).date()
    m = _TS_RE.match(s)
    if not m:
        return None
    d = parse_date(m.group("date"), report)
    if d is None:
        return None
    if m.group("h") is None:
        return d
    h, mi, sec = int(m.group("h")), int(m.group("mi")), int(m.group("s") or 0)
    ap = m.group("ap")
    if ap:
        if not 1 <= h <= 12:
            return None
        h = h % 12 + (12 if ap.lower() == "pm" else 0)
    if h > 23 or mi > 59 or sec > 59:
        return None
    tz = m.group("tz")
    if tz is None:
        return d  # naive: already local
    if tz.upper() in ("Z", "UTC", "GMT"):
        offset = timedelta(0)
    else:
        sign = -1 if tz[0] == "-" else 1
        digits = tz[1:].replace(":", "")
        offset = sign * timedelta(hours=int(digits[:2]), minutes=int(digits[2:4] or 0))
    aware = datetime(d.year, d.month, d.day, h, mi, sec, tzinfo=timezone(offset))
    return aware.astimezone(LOCAL_TZ).date()


def norm_id(s: str | None) -> str:
    return (s or "").strip().upper()


def norm_tool(s: str | None) -> str | None:
    """'a', ' A ', 'Tool A', 'tool_a' -> 'A'. None if not one of A..E."""
    m = re.fullmatch(r"(?:TOOL[\s_\-]*)?([A-Z])", (s or "").strip().upper())
    return m.group(1) if m and m.group(1) in TOOLS else None


def norm_tier(s: str | None) -> str:
    return (s or "").strip().lower()


def parse_price(s: str | None) -> Fraction | None:
    """Blank / non-numeric / negative -> None (= not priced)."""
    t = re.sub(r"(?i)usd|\$|,|\s", "", s or "")
    if not t:
        return None
    try:
        v = Decimal(t)
    except InvalidOperation:
        return None
    if not v.is_finite() or v < 0:
        return None
    return Fraction(v)


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #
@dataclass
class Seat:
    """One (person, tool) seat. ``intervals`` are disjoint, sorted:
    (first_day_ordinal, first_day_without_seat_ordinal | None, monthly_price | None).
    """
    user_id: str
    tool: str
    intervals: list[tuple[int, int | None, Fraction | None]]


@dataclass
class CleanData:
    people: dict[str, str | None]            # user_id -> org_unit_id (None if unknown)
    people_names: dict[str, str]             # user_id -> display name
    org_parent: dict[str, str | None]        # org_unit_id -> parent (None = root)
    org_names: dict[str, str]               # org_unit_id -> name
    org_levels: dict[str, str]              # org_unit_id -> level string (e.g. "L4")
    org_users: dict[str, frozenset[str]]     # org_unit_id -> users in it and all below
    seats: list[Seat]
    priced_tools: frozenset[str]             # tools with at least one priced tier
    events: dict[tuple[str, str], list[int]] # (user, tool) -> sorted unique local-day ordinals
    report: dict


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #
def _read_csv(path: Path) -> list[dict[str, str]]:
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    rows = csv.reader(io.StringIO(text, newline=""))
    header = next(rows, None)
    if header is None:
        return []
    cols = [h.strip().lower() for h in header]
    out = []
    for r in rows:
        if not any(c.strip() for c in r):
            continue
        out.append({cols[i]: r[i].strip() for i in range(min(len(cols), len(r)))})
    return out


# --------------------------------------------------------------------------- #
# Per-file cleaning
# --------------------------------------------------------------------------- #
def _clean_orgs(rows, rep: Report) -> tuple[dict[str, str | None], dict[str, str], dict[str, str]]:
    """Returns (parent_map, org_names, org_levels)."""
    parent: dict[str, str | None] = {}
    names: dict[str, str] = {}
    levels: dict[str, str] = {}
    for r in rows:
        oid = norm_id(r.get("org_unit_id"))
        if not oid:
            rep.add("org_units_blank_id")
            continue
        if oid in parent:
            rep.add("org_units_duplicate_id_last_wins", oid)
        pid = norm_id(r.get("parent_id")) or None
        parent[oid] = pid
        names[oid] = (r.get("name") or "").strip() or oid
        levels[oid] = (r.get("level") or "").strip()
    for oid, pid in list(parent.items()):
        if pid is not None and (pid not in parent or pid == oid):
            rep.add("org_units_parent_unknown_or_self_treated_as_root", f"{oid}->{pid}")
            parent[oid] = None
    # break cycles: walk up from every node; if we meet a node on the current path, cut it
    state: dict[str, int] = {}
    for start in parent:
        path, cur = [], start
        while cur is not None and state.get(cur, 0) == 0:
            state[cur] = 1
            path.append(cur)
            cur = parent[cur]
        if cur is not None and state.get(cur) == 1:
            rep.add("org_units_cycle_edge_cut", f"{path[-1]}->{cur}")
            parent[path[-1]] = None
        for n in path:
            state[n] = 2
    return parent, names, levels


def _org_user_sets(parent, people) -> dict[str, frozenset[str]]:
    children: dict[str, list[str]] = defaultdict(list)
    for oid, pid in parent.items():
        if pid is not None:
            children[pid].append(oid)
    direct: dict[str, set[str]] = defaultdict(set)
    for uid, oid in people.items():
        if oid is not None:
            direct[oid].add(uid)
    out = {}
    for oid in parent:
        users: set[str] = set()
        stack, seen = [oid], set()
        while stack:
            n = stack.pop()
            if n in seen:
                continue
            seen.add(n)
            users |= direct.get(n, set())
            stack.extend(children.get(n, ()))
        out[oid] = frozenset(users)
    return out


def _clean_people(rows, orgs, rep: Report) -> tuple[dict[str, str | None], dict[str, str]]:
    """Returns (people_org_map, people_names)."""
    people: dict[str, str | None] = {}
    names: dict[str, str] = {}
    for r in rows:
        uid = norm_id(r.get("user_id"))
        if not uid:
            rep.add("people_blank_id")
            continue
        if uid in people:
            rep.add("people_duplicate_id_last_wins", uid)
        oid = norm_id(r.get("org_unit_id"))
        if oid not in orgs:
            rep.add("people_unknown_org_kept_company_wide_only", f"{uid}:{oid or '<blank>'}")
            oid = None
        people[uid] = oid
        names[uid] = (r.get("name") or "").strip() or uid
    return people, names


def _clean_prices(rows, rep: Report) -> tuple[dict[tuple[str, str], Fraction | None], frozenset[str]]:
    prices: dict[tuple[str, str], Fraction | None] = {}
    for r in rows:
        tool, tier = norm_tool(r.get("tool")), norm_tier(r.get("tier"))
        if tool is None:
            rep.add("price_list_unknown_tool", r.get("tool", ""))
            continue
        price = parse_price(r.get("monthly_price_usd"))
        if (tool, tier) in prices:
            rep.add("price_list_duplicate_tool_tier", f"{tool}/{tier}")
            if price is None:           # a blank never overrides a real price
                continue
        prices[(tool, tier)] = price
    priced = frozenset(t for (t, _), p in prices.items() if p is not None)
    return prices, priced


def _resolve_overlaps(rows: list[tuple[int, int | None, str]]):
    """Same person+tool listed more than once. Same-tier overlaps are unioned (no day
    is ever charged twice); on a tier change the later grant supersedes the earlier.
    """
    rows = sorted(rows, key=lambda r: r[0])   # stable: file order breaks ties
    out: list[tuple[int, int | None, str]] = []
    for s in rows:
        if out and (out[-1][1] is None or out[-1][1] > s[0]):
            p = out[-1]
            if p[2] == s[2]:
                end = None if (p[1] is None or s[1] is None) else max(p[1], s[1])
                out[-1] = (p[0], end, p[2])
            elif s[0] == p[0]:
                out[-1] = s
            else:
                out[-1] = (p[0], s[0], p[2])
                out.append(s)
        else:
            out.append(s)
    return out


def _clean_seats(rows, people, prices, priced_tools, rep: Report) -> list[Seat]:
    grouped: dict[tuple[str, str], list[tuple[int, int | None, str]]] = defaultdict(list)
    for r in rows:
        uid, tool = norm_id(r.get("user_id")), norm_tool(r.get("tool"))
        if uid not in people:
            rep.add("seats_user_not_in_people_dropped", uid)
            continue
        if tool is None:
            rep.add("seats_unknown_tool_dropped", r.get("tool", ""))
            continue
        start = parse_date(r.get("granted_on"), rep)
        if start is None:
            rep.add("seats_bad_granted_on_dropped", r.get("granted_on", ""))
            continue
        raw_end = r.get("revoked_on", "")
        end = None
        if raw_end:
            end_d = parse_date(raw_end, rep)
            if end_d is None:
                rep.add("seats_bad_revoked_on_dropped", raw_end)
                continue
            end = end_d.toordinal()
            if end <= start.toordinal():
                rep.add("seats_revoked_not_after_granted_dropped", f"{uid}/{tool}")
                continue
        grouped[(uid, tool)].append((start.toordinal(), end, norm_tier(r.get("tier"))))

    seats = []
    for (uid, tool), items in grouped.items():
        if len(items) > 1:
            rep.add("seats_multiple_rows_per_user_tool_resolved", f"{uid}/{tool}")
        intervals = []
        for start, end, tier in _resolve_overlaps(items):
            price = prices.get((tool, tier)) if tool in priced_tools else None
            if tool in priced_tools and price is None:
                rep.add("seats_tier_without_price_charged_zero", f"{tool}/{tier}")
            intervals.append((start, end, price))
        seats.append(Seat(uid, tool, intervals))
    return seats


def _clean_events(rows, people, rep: Report) -> dict[tuple[str, str], list[int]]:
    days: dict[tuple[str, str], set[int]] = defaultdict(set)
    seen_rows: set[tuple[str, str, str, str]] = set()
    for r in rows:
        uid, tool = norm_id(r.get("user_id")), norm_tool(r.get("tool"))
        etype = re.sub(r"[\s\-]+", "_", (r.get("event_type") or "").strip().lower())
        if tool is None:
            rep.add("events_unknown_tool_dropped", r.get("tool", ""))
            continue
        if etype not in USAGE_EVENT_TYPES:
            rep.add(f"events_non_usage_type_dropped:{etype or '<blank>'}")
            continue
        day = parse_event_ts(r.get("event_ts"), rep)
        if day is None:
            rep.add("events_bad_timestamp_dropped", r.get("event_ts", ""))
            continue
        if uid not in people:
            rep.add("events_user_not_in_people_dropped", uid)
            continue
        key = (uid, tool, etype, (r.get("event_ts") or "").strip())
        if key in seen_rows:
            rep.add("events_exact_duplicate_dropped")
            continue
        seen_rows.add(key)
        days[(uid, tool)].add(day.toordinal())
    return {k: sorted(v) for k, v in days.items()}


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def load(data_dir: str | Path = "data") -> CleanData:
    d = Path(data_dir)
    rep = Report()

    org_parent, org_names, org_levels = _clean_orgs(_read_csv(d / "org_units.csv"), rep)
    people, people_names = _clean_people(_read_csv(d / "people.csv"), org_parent, rep)
    prices, priced_tools = _clean_prices(_read_csv(d / "price_list.csv"), rep)
    seats = _clean_seats(_read_csv(d / "seats.csv"), people, prices, priced_tools, rep)
    events = _clean_events(_read_csv(d / "activity_events.csv"), people, rep)

    seat_keys = {(s.user_id, s.tool) for s in seats}
    for key, ds in events.items():
        if key not in seat_keys:
            rep.counts["event_days_for_tool_without_any_seat"] += len(ds)
    for t in TOOLS:
        if t not in priced_tools:
            rep.add("tools_unpriced", t)

    return CleanData(
        people=people,
        people_names=people_names,
        org_parent=org_parent,
        org_names=org_names,
        org_levels=org_levels,
        org_users=_org_user_sets(org_parent, people),
        seats=seats,
        priced_tools=priced_tools,
        events=events,
        report=rep.as_dict(),
    )


if __name__ == "__main__":
    data = load(sys.argv[1] if len(sys.argv) > 1 else "data")
    print(json.dumps(data.report, indent=2))
