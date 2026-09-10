# `/recordings/ranges` under-reporting — root cause and fix

**Reported:** BE investigation, 2026-09-10, device `100.64.0.15` running `0.17.2-f2e8cf2`
**Status:** root cause confirmed, fix implemented and verified, shipped in `4d810d7d`

The BE report was right on every verified point and right in its inference. This
records what the cause actually was, how it was confirmed, and what remains.

---

## 1. Confirmed against the device

Queried `2-Office-Front`, `variant=main`, a 12 h window ending at the time of
investigation:

| source | result |
|---|---|
| raw `recordings`, 4319 rows, merged at `gap=3` | **1 range, 12.00 h** ← truth |
| `/recordings/ranges?gap=3` | 53 ranges, **3.53 h** |
| `/recordings/ranges?gap=1.0` | 132 ranges, **3.50 h** |
| `/recordings/ranges?gap=0.5` | 216 ranges, **11.94 h** |

`gap=0.5` is the diagnostic: it is below `MATERIALIZED_GAP`, so it **bypasses the
stored table entirely** and merges live. It returns the right answer. So the
merge SQL, the overlap predicate and the endpoint were all fine — the stored
table was wrong.

Narrowing to a settled 30-minute window: 181 segments, 1810 s of raw coverage,
inter-segment gaps between −1.034 s and +1.033 s (no gap anywhere near 3 s).
The stored table held **3 ranges totalling 296 s**, all in the last five minutes.
**151 of 181 segments had no stored coverage at all.**

## 2. Root cause

In `_replace_window`, as shipped:

```python
DELETE WHERE end_time >= lo AND start_time <= hi     # everything overlapping
INSERT live_ranges(lo, hi)                            # admits end_time >= lo
```

Stored ranges are **unclipped** — a run of continuous recording is one row whose
`end_time` sits at the live edge. So `end_time >= lo` matched that entire row,
back to its start hours earlier, while the recompute only regenerated coverage
from `lo` rightward.

Every tick therefore ate the table from the left and kept roughly its own window.
`lo = covered_to - MAX_SEGMENT_DURATION` walks forward with time, so the erosion
was continuous.

The design's own safety net should have hidden this: anything outside coverage is
merged live. But the coverage row was still being advanced to claim the whole
span, so the endpoint trusted a table that no longer held the data. **The
guarantee that "no footage" is never confused with "not computed yet" was
violated from the inside.**

The original reasoning error: I assumed the seek bound at `lo - MAX_SEGMENT_DURATION`
made the recompute a superset of the delete. It does not — that bound is only an
index hint; `end_time >= lo` is what actually admits rows.

### Why the report's inference was slightly off

The report inferred "backfill path correct, incremental path broken". Both paths
share `_replace_window`, so both were affected. The backfill *looked* correct
because it walks leftward into virgin territory, where there is nothing to the
left to destroy — its damage was confined to seams, which is exactly the
unexplained **1–2 % shortfall on 09-05 → 09-07**. Same bug, different geometry.

The predicted self-repair of 09-09 would **not** have happened: the damaged rows
sit between `covered_from` and `covered_to`, and neither path revisits that
region.

## 3. The fix (`4d810d7d`)

Carry the parts of a straddling range that lie outside the window across the
delete, and merge them back with the recomputed rows:

```python
for row_start, row_end in overlapping:
    if row_start < lo: carried.append((row_start, lo))
    if row_end > hi:   carried.append((hi, row_end))

DELETE WHERE end_time >= lo AND start_time <= hi
rows = merge_ranges(sorted(carried + live_ranges(lo, hi)), MATERIALIZED_GAP)
```

Bounded on **both** sides: backfill recomputes windows that have stored coverage
to their right, so an unbounded delete would have traded a left-edge bug for a
right-edge one. Merging the carried remainder back in also keeps a continuous run
as a single row rather than gaining a seam at every window boundary.

### Verification

Replayed **1380 real segments captured from the device** (real jitter, including
−1.034 s overlaps), fed in over simulated time with the rollup ticking every 60 s:

| | shipped | fixed |
|---|---|---|
| served from stored table | 15 ranges, **0.85 h** | 1 range, **3.83 h** |
| live merge (truth) | 1 range, 3.83 h | 1 range, 3.83 h |
| exact match incl. boundaries | ✗ | ✓ |

`TestCoverageInvariant` now asserts the violated guarantee directly — inside the
window the coverage row claims, the stored answer must equal the live one. Its
three bug-specific cases fail against the shipped code with the right diagnosis
(`stored path reports 250.0s inside its own claimed coverage, live merge reports
4150.0s`) and pass after. Full suite: 284 tests.

## 4. Repairing deployed devices

**Migration `036` discards both tables.** The damaged rows are unreachable by
either rebuild path, so fixing the rollup alone does not heal them. Nothing is
lost — the tables are a cache over `recordings`, and an uncovered window is
merged live.

After upgrade, expect **correct but slower** answers while precomputation
rebuilds: the rollup covers forward from now within a tick, the backfill walks
history about a day per hour. On 30-day retention, roughly a day to fully
precomputed. Answers are correct throughout.

## 5. Verifying on a device after deploy

The `gap=0.5` trick is the check — it forces the live path, so stored and live
must agree:

```bash
CAM=2-Office-Front; A=$(( $(date +%s) - 43200 )); B=$(date +%s)
for G in 3 0.5; do
  curl -s "http://100.64.0.15:5000/api/$CAM/recordings/ranges?after=$A&before=$B&variant=main&gap=$G" \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print(f'gap=$G: {len(d)} ranges, {sum(r[\"end\"]-r[\"start\"] for r in d)/3600:.2f} h')"
done
```

Total hours must match between the two. They differed by 3.4× before the fix.

## 6. The two follow-ups, now measured

Both were left open in the first pass and have since been measured rather than
inferred.

### The 1–2 % shortfall on backfilled days — same bug, confirmed

A dense multi-day backfill replay, asserting the coverage invariant after every
step, fails against the shipped code:

```
after backfill step 1: stored reports 87270s inside claimed coverage
[-25.1h, 0.0h], live reports 90550s (3.6% lost)
```

Same order as the field-reported 1–2 %, and it passes after the fix. So the
report's "secondary, much smaller" item was never a separate problem — it was
the same delete, with backfill's geometry, losing coverage off the **right** edge
of each window instead of the left. This is why the fix bounds the delete on both
sides rather than only fixing the left. Guarded by
`test_backfill_preserves_coverage_it_walks_past`.

### `MATERIALIZED_GAP` — retuned to 2.0, and a camera fault found

Measured across all 24 cameras × both variants:

| threshold | stored rows/day, fleet | over 30 d retention |
|---|---|---|
| 1.0 s (as shipped) | 22,227 | ~667,000 |
| 2.0 s (now) | 8,868 | ~266,000 |

Most streams never exceed 1.0 s. **`4-Bullpen-2` alone produced ~16,700 of those
22,227 rows/day** — three quarters of the fleet. Set to 2.0, which keeps a margin
below the 3.0 s default callers send.

Worth being clear about the severity: this was only ever storage and read cost,
never correctness — read-time merging at `gap=3` returned the right answer
throughout. 667k rows is ~2.5 % of the recordings table, and a day view read
~22k rows against the 276k raw segments it replaces. It was over-flagged.

**The genuinely important finding came out of measuring it.** `4-Bullpen-2` is
writing **8.07 s segments instead of 10 s, with ~1.9 s holes between them, and
covers only 80.6 % of wall-clock time** (4.71 h recorded out of 5.84 h). Its
neighbour on the same device records 99.98 %. That is a recording fault on that
camera, not a timeline artefact — the timeline was faithfully reporting it, and
at `gap=3` those sub-3 s holes merge away so it never showed up in the UI. Worth
investigating separately; nothing in this fix addresses it.
