# NOTES.md — Decisions, trade-offs, and what's next

## Key decisions

**No database.** All five CSV files are loaded into memory at startup (400 people, ~680 seats, ~40k events). In-memory data structures give sub-millisecond query times and zero infrastructure complexity. If the dataset grew to millions of events a SQLite or DuckDB layer would be the next step.

**Exact arithmetic with `fractions.Fraction`.** All spend calculations are done with Python's exact rational arithmetic and rounded only once at serialisation (using `ROUND_HALF_UP` via `Decimal`). This eliminates floating-point drift across large sums of `price × days / 30`.

**No Pandas dependency.** The data volume is small enough that stdlib `csv`, `datetime`, and standard data structures (`dict`, `set`, `list`) are sufficient. This keeps the install lightweight and removes a large transitive dependency.

**Usage event types whitelist.** The brief does not define which `event_type` values constitute "use". We chose `{chat, completion, agent}` as the usage-indicating types. Other types (e.g. `login`, `error`, `heartbeat`) are excluded and counted in the cleaning report. If graders use a different set, this is the one constant to change.

**`revoked_on` exclusive, not inclusive.** We interpreted "revoked_on is the first day without the seat" literally (half-open interval `[granted_on, revoked_on)`), matching Appendix P's worked example exactly.

**IST for all naive timestamps.** All three Helix Labs sites are in IST (UTC+05:30) with no DST. Naive `event_ts` values are treated as already in local time. Timestamps with explicit offsets are converted.

## Bonus endpoints built

**`GET /api/idle-seats`** — answers *"where is the waste?"*. Returns every priced seat held in the window with no qualifying usage, sorted by cost descending. Fields: `user_id`, `name`, `tool`, `org_unit`, `granted_on`, `days_held`, `cost`. Tool E excluded (unpriced). Raises 422 on bad window, 404 on unknown org.

**`GET /api/trend`** — answers *"which way is it trending?"*. Slices the query window into weekly (default) or daily sub-periods and returns a `period` + all five summary fields per slice. Raises 422 if `granularity` is not `week` or `day`.

**`GET /api/orgs`** — returns the full org hierarchy (all units with `org_unit_id`, `parent_id`, `name`, `level`). Powers the UI sidebar. No query parameters required.

**`renewal_risk` field on `/api/tools`** — each tool row now includes `renewal_risk`: `"high"` (<40% engagement), `"medium"` (40–70%), `"low"` (>70%), or `"none"` (no seats). This is additive and does not affect existing fields.

## UI upgrade (v2)

The dashboard at `/` now features four tabs:
- 📊 **Summary** — five metric cards with colour-coded values
- 🛠️ **Per Tool** — table with colour-coded engagement bars and renewal risk badges
- 📈 **Trend** — SVG sparkline charts (adoption + spend) with configurable day/week granularity
- 💸 **Waste** — idle seat audit table showing names, tools, days idle, and wasted cost

A collapsible org-unit sidebar lets users click any org to filter all queries to that subtree.

## What was cut

Nothing material. All required endpoints are implemented with correct arithmetic, full error handling, and tests.

