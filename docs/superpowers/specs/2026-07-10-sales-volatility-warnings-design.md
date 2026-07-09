# Sales volatility detection + warnings — design

**Date:** 10 July 2026
**Status:** Scope approved in brainstorm; spec for implementation planning.

## Problem

Velocity is a flat average everywhere: `avg_monthly = total_sold ÷ months`
(inventory agent for months-of-supply/status; recommendation agent for
suggested quantities). The AI never sees the month-by-month shape — the
average is pre-baked before any prompt is built. Consequences:

- **One-off spike** (a bulk order/tender): 11 months × 100 + one month ×
  10,000 → average 925/mo → suggested orders ~9× too large, months-of-supply
  ~9× too small (false CRITICAL), and confidence stays HIGH because
  confidence tracks data *completeness*, not volatility. Confidently wrong.
- **Recurring swings** (offshore periods when vessels are away, festive
  peaks): the average smears the cycle — over-orders in quiet months,
  under-orders at peak.
- **Irregular sellers** (nothing most months, occasional burst): the average
  is small but meaningless.

## Decisions locked in brainstorm (10 July 2026)

1. **Detection + warnings only.** No demand-calendar/config UI — demand
   patterns are customer-driven (vessel customers leaving, not the whole
   company) and berthcast doesn't read customer data, so no clean company-
   level configuration exists. PARKED until real client pull or customer-
   level ingestion. The existing **context form** is the human channel; the
   volatile warning points at it.
2. **Spiky items get auto-corrected** to the typical month (median), stated
   openly on the recommendation. Staff can still edit the quantity.
3. **Swingy items get a warning only** — no safer number exists without
   calendar knowledge, so the number is left alone and the human is asked.
4. **No trend detection** — with ~3 months of pilot history, trend and noise
   are indistinguishable; a false "demand is dying" costs credibility.
5. All deterministic Python. Zero extra AI tokens for detection itself.

## Detection (new, `agents/shared.py`)

One helper computes per-item monthly sales totals and classifies the pattern:

```
monthly_pattern_stats(session_id) ->
    {item_name: {"months": int,          # distinct months with data coverage
                 "monthly": [floats],    # total per covered month
                 "mean": float, "median": float,
                 "pattern": "stable" | "spiky" | "volatile" | "lumpy",
                 "corrected_avg": float | None}}   # median, spiky only
```

- **Source:** the transactional sales table (`sales_<sid>`) — needs a date
  column and a qty column. Month keys come from the existing Python date
  machinery (`count_sales_months` family — handles DD/MM/YYYY, textual,
  Excel serials); NOT naive `substr()`, which only fits ISO dates.
- **Guard rails:** classification requires **≥ 4 distinct months** of dated
  history; otherwise `pattern = "stable"` (silent) — too little signal to
  accuse the data of anything. Wide-matrix summary sheets (12 month columns,
  no date column) are **out of scope v1**: no dated rows → no monthly series
  → silently stable. Sheets whose velocity comes from a stated Avg/Month
  column still get *classified* (if dates exist) and *warned*, but their
  stated average is never overridden — we only correct numbers we computed
  ourselves.
- **Classification rules, checked in this order** (first match wins):
  1. **lumpy** — at least half the covered months have zero sales, but some
     sales exist. Velocity untouched.
  2. **spiky** — median > 0 AND mean ≥ 2 × median. `corrected_avg = median`.
  3. **volatile** — median > 0 AND (max ≥ 3 × min over the months that have
     any sales, OR at least one covered month has zero sales while fewer than
     half do — an item that vanishes some months is swinging even if its
     selling months are steady). Velocity untouched.
  4. **stable** — everything else. No flag, no output, no noise.

Worked checks: the cheese example (11×100 + 1×10,000): mean 925, median 100
→ spiky, sized at 100. Offshore-ish (9×100 + 3×20): mean 80, median 100 →
mean < 2×median, max/min = 5 → volatile → warn only. Flat 12×100 → stable.

## Where it plugs in

- **Inventory agent** (`agents/inventory.py`): items classified spiky use
  `corrected_avg` for `avg_monthly` → months-of-supply and status stop
  false-flagging CRITICAL after one bulk month. (Only on the totals÷months
  path — a sheet-stated average is used as-is, per guard rail above.)
- **Recommendation agent** (`agents/recommendation.py`): same substitution
  for sizing (`suggested_qty = avg × (lead-time + buffer)` now uses the
  median for spiky items). The enriched per-item prompt block gains ONE
  line so Claude can explain it in its reason text:
  - spiky: `Sales pattern: SPIKY — one month dominates; typical month
    (median) = X, raw average = Y. Quantities are sized on the typical
    month.`
  - volatile: `Sales pattern: VOLATILE — monthly sales swing between X and
    Y. The average may mislead; flag this for the buyer.`
  - lumpy: `Sales pattern: IRREGULAR — sells in bursts with many zero
    months.`
- **Deterministic post-pass** (same pattern as the quantity sanitizer —
  never trust the model to self-report): after recs come back, for every
  spiky/volatile/lumpy item append a plain-English entry to the rec's
  `flags` array and **cap confidence at MEDIUM** (HIGH → MEDIUM; lower
  values stay as-is).
  - spiky flag: `Spiky sales history — one month dominates the average;
    quantity sized on the typical month (X/mo, raw average Y/mo).`
  - volatile flag: `Sales swing a lot month to month (X–Y). If this is a
    known cycle (offshore periods, festive season), mention it in the
    analysis context next run.`
  - lumpy flag: `Irregular seller — bursts with quiet months. Verify with
    your team before ordering.`
- **Progress line** (existing `_emit` channel): `Safety check: N items with
  unusual sales patterns (S spiky, V swingy, L irregular)` — only when N>0.
- **Clarity box** (`rec_logic.clarity_gaps`): one counted line when any
  flagged items exist: `N items have unusual sales patterns — check their
  flags before approving.`

## What staff see

Nothing new to learn: the flag text appears in the rec's expanded panel
(flags already render there), confidence chip reads MEDIUM instead of HIGH,
the clarity box counts them, and for spiky items the quantity itself is the
safer number with the reason stating why. Edit field still lets staff
override anything.

## Testing

- New `tests/test_sales_volatility.py`: classifier unit cases (stable /
  spiky boundary at exactly 2× / volatile at 3× max-min / lumpy half-zeros /
  <4 months → stable / median-zero never divides), the cheese worked example
  end-to-end (corrected qty ≈ median × (lt+buffer)), confidence cap
  behaviour, and flag text presence.
- Extend the dummy fixture pipeline test only if cheap; otherwise unit
  coverage + one integration assertion through the recommendation agent with
  the stubbed Claude (planted spiky item → flag + capped confidence in
  final recs).
- Full suite stays green.

## Out of scope (explicit)

- Demand calendar / pattern config UI (parked — see decision 1).
- Trend detection (parked until 6+ months of history).
- Wide-matrix (month-columns) sheets — no dated rows, silently stable.
- Seasonality forecasting/modelling.
- Reading `customers_<sid>` data.

## Maintenance note

Thresholds (2× median, 3× max/min, ≥4 months, half-zero months) live as
named constants in `agents/shared.py` next to the helper — tune from real
client data later without hunting magic numbers.
