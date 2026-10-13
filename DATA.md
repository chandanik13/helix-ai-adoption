# DATA.md — Data sourcing, quality findings, and decisions

## Files and their origin

| File | Rows | Source system |
|---|---|---|
| `people.csv` | 400 | HR / identity system |
| `org_units.csv` | 37 | HR / organisational chart |
| `seats.csv` | 680 | Licence management system |
| `price_list.csv` | 7 | Finance / contract system |
| `activity_events.csv` | 39,971 | AI-tool vendor log exports |

---

## Data quality issues found and how we handled them

### 1. Tool E is unpriced (`tools_unpriced: 1`)

**Finding**: `price_list.csv` has a row for Tool E with a blank `monthly_price_usd`.  
**Decision**: Tool E is treated as unpriced throughout. Per Appendix P, `spend`, `idle_spend`, and `cost_per_active` are reported as `null` for Tool E. Licensed and active user counts are still reported correctly.  
**Rule**: A blank or non-numeric price is always unpriced — never zero.

---

### 2. Exact duplicate events (`events_exact_duplicate_dropped: 250`)

**Finding**: 250 rows in `activity_events.csv` are exact duplicates (same user_id, tool, event_type, and event_ts).  
**Decision**: Deduplicate by keeping the first occurrence. This prevents inflating active-user counts and day-sets. A duplicate is defined as identical (user_id, tool, event_type, event_ts) after normalisation.  
**Rule**: Deduplication is content-based, not row-index-based, so it generalises to unseen data.

---

### 3. Events for users not in `people.csv` (`events_user_not_in_people_dropped: 40`, users: U9003, U9002, U9005, …)

**Finding**: 40 event rows reference user IDs (e.g. `U9003`, `U9002`, `U9005`) that do not appear in `people.csv`.  
**Decision**: These events are dropped. Per the brief: *"We report on Helix Labs' own developers only, the people in people.csv."* External contractors or test accounts that slipped into the vendor log are excluded.  
**Rule**: Any user_id in `activity_events.csv` not present in `people.csv` is ignored.

---

### 4. Events for (user, tool) combinations with no seat (`event_days_for_tool_without_any_seat: 120 day-entries`)

**Finding**: 120 event-day entries exist for user-tool pairs where the user holds no seat for that tool at all.  
**Decision**: These event days are stored but do not contribute to active-user counts, because Appendix P defines usage as: *"Use of a tool only counts on a day when the person held a seat for that tool."*  
**Rule**: Qualifying usage requires a concurrent seat interval. Events without a matching seat are harmless to store but never counted.

---

### 5. `event_ts` timezone handling

**Finding**: All observed timestamps are naive (no timezone suffix), formatted as `YYYY-MM-DD HH:MM:SS`.  
**Decision**: Naive timestamps are treated as already in IST (UTC+05:30), since all three Helix Labs sites (Bengaluru, Hyderabad, Pune) are in the same timezone with no DST. The parser also handles UTC/Z-suffixed timestamps and Unix epoch values (10- and 13-digit), converting them to the local IST date before use.  
**Rule**: Naive = local IST. Explicit offset = convert to IST then take the date.

---

### 6. `revoked_on` semantics

**Finding**: `revoked_on` is the **first day without** the seat, not the last day with it.  
**Decision**: A seat is held on day `d` if `granted_on ≤ d < revoked_on`. A blank `revoked_on` means the seat is still active (treated as held through any query window).  
**Rule**: Use `[granted_on, revoked_on)` half-open intervals throughout. Spend is `(days_held_in_window) * monthly_price / 30`.

---

### 7. `monthly_price_usd` covers 30 days (not calendar months)

**Finding**: Appendix P states explicitly: *"A seat's monthly price covers 30 days."*  
**Decision**: Daily rate = `monthly_price / 30`. No calendar-month logic. This ensures consistent results regardless of month length.

---

## Test design

Our test suite (`tests/test_all.py`) is structured in 10 sections:

| Section | What it tests |
|---|---|
| 1. Worked example | Exact reproduction of Appendix P's example (all 5 numbers) |
| 2. Seat boundaries | `revoked_on` is exclusive; active seats charge full window |
| 3. Usage counting | Events without a seat don't count; events outside window don't count |
| 4. Unpriced tools | spend/idle/cost_per_active = null; user counts still work |
| 5. Error conditions | BadWindow, UnknownOrg, bad date strings |
| 6. Tool ordering | A→E always returned in order |
| 7. Engagement edge cases | 0% with no licensed; 100% with all active |
| 8. Date/timestamp parsers | Multi-format dates, timezone conversion, epoch |
| 9. Org filtering | Recursive subtree coverage, unknown org raises |
| 10. API endpoints | All endpoints, 200 paths and all error paths, offline via TestClient |
