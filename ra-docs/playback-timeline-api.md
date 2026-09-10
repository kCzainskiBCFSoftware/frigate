# Playback timeline API — integration guide

**Audience:** whoever implements the backend side of the playback timeline. This
document is self-contained: it describes three new Frigate-fork endpoints, what
they return, and how to build a multi-camera timeline from them. You do not need
to read the fork source.

**Status:** implemented, tested, not yet deployed to any device.

**Companion docs (same repo):** `ra-docs/dual-stream-api-changes.md` for the
`?variant=` model; `ra-docs/fork-changes.md` for everything else the fork adds.

---

## 1. What problem this solves

To draw "which parts of this day have footage" you previously called
`GET /api/{camera}/recordings`, which returns **one row per ~10s segment**:
~8,640 rows × 12 fields ≈ **2 MB per camera-day**. You then discarded ten of the
twelve fields and merged the rows into ~40 `{start, end}` ranges (~2 kB). For a
16-camera day view that is ~32 MB of JSON to draw sixteen strips.

The three endpoints below answer the timeline question directly, on the device,
next to the data.

| | before | after |
|---|---|---|
| one camera-day | ~2 MB, ~8,640 rows | ~2 kB, ~40 ranges |
| 32-camera day view | 32 requests, ~64 MB | **1 request**, ~64 kB |

`GET /api/{camera}/recordings` is unchanged and still works. Nothing here is a
breaking change.

---

## 2. Capability detection (do this first)

Devices upgrade independently, so assume a mixed-version fleet. `GET /api/config`
now carries a top-level `fork` key:

```json
{
  "fork": {
    "version": "0.17.2-abc1234",
    "features": ["recording_variants", "recordings_hours", "recordings_ranges"]
  },
  "cameras": { "...": "unchanged" }
}
```

- `recordings_ranges` present → `/api/recordings/ranges` and
  `/api/{camera}/recordings/ranges` exist.
- `recordings_hours` present → `/api/recordings/hours` exists.
- `fork` key **absent entirely** → older fork build; fall back to
  `GET /api/{camera}/recordings` and merge client-side.

You already fetch `/api/config` for camera sync, so this costs no extra request.
The list is **append-only** — a flag that has shipped will never be renamed or
removed. A 404 on the endpoint itself remains a valid secondary signal.

---

## 3. `GET /api/{camera}/recordings/ranges`

Merged coverage for one camera. This is the drop-in replacement for fetching
segments and merging them yourself.

### Request

```
GET /api/{camera}/recordings/ranges?after=1787813986&before=1787900386&variant=sub&gap=3
```

| Param | Type | Default | Meaning |
|---|---|---|---|
| `after` | float, unix seconds | one hour ago | window start |
| `before` | float, unix seconds | now | window end |
| `variant` | `main` \| `sub` \| `all` | `sub` | which recorded stream |
| `gap` | float seconds | `3.0` | holes ≤ this are merged — see §6 |

### Response

`200`, a bare JSON array, ordered by `start`:

```json
[{"start": 1787813986.0, "end": 1787814015.999674},
 {"start": 1787815176.0, "end": 1787819999.5}]
```

Response headers worth reading:

| Header | Example | Why |
|---|---|---|
| `X-Recording-Variant` | `sub` | the variant **actually served** — may differ from what you asked for, see §5 |
| `Cache-Control` | `public, max-age=60, stale-while-revalidate=300` | see §7 |

No footage in the window → `200` with `[]`. That is a real, cacheable answer,
not an error.

---

## 4. `GET /api/recordings/ranges` (batch — prefer this)

The same answer for many cameras in one request. Use this for any multi-camera
view; it replaces a fan-out.

### Request

```
GET /api/recordings/ranges?cameras=front,back,drive&after=...&before=...&variant=sub&gap=3
```

Same params as §3, plus:

| Param | Type | Default | Meaning |
|---|---|---|---|
| `cameras` | comma-separated, or `all` | `all` | intersected with the cameras the caller may see |

### Response

```json
{
  "ranges": {
    "front": [{"start": 1787813986.0, "end": 1787814015.999674},
              {"start": 1787815176.0, "end": 1787819999.5}],
    "back":  [],
    "drive": [{"start": 1787813000.0, "end": 1787899000.0}]
  },
  "variants": {"front": "sub", "back": "sub", "drive": "main"}
}
```

- Every requested (and permitted) camera appears in `ranges`, even with no
  footage — an empty list, never a missing key.
- `variants` is **per camera** because the fallback in §5 is per camera. Do not
  assume one variant for the whole response.
- A camera the caller may not see is silently omitted. An empty selection
  returns `{"ranges": {}, "variants": {}}`.

---

## 5. Variant selection and fallback

Same model as the rest of the fork's recording endpoints. Dual-stream cameras
record `main` (HD) and `sub` (SD) as separate rows under one camera entity.

The fallback: **if the requested variant has no footage in the requested window,
the server serves the other variant instead of returning an empty timeline.**
This exists because history predating a camera's dual-stream upgrade only has
`main` rows.

Consequences for you:

- Always read back which variant you got — `variants[camera]` on the batch form,
  the `X-Recording-Variant` header on the single form.
- The fallback is decided **per camera and per window**. The same camera can
  answer `main` for last month and `sub` for today.
- `variant=all` disables the fallback and merges both variants together. Useful
  for "is there any footage at all", wrong for "can I play this at this
  quality".

---

## 6. `gap` — the one parameter that needs explaining

Frigate writes ~10s segments that are *meant* to abut exactly, but the encoder
leaves a few hundred milliseconds between them. That is **not** a recording gap;
the camera never stopped. `gap` is the threshold separating jitter (merge it)
from a real outage (keep it visible — camera offline, recording disabled, disk
full).

It is the entire compression ratio. Segments at `0–10`, `10.4–20.4`,
`20.4–30.4`, then nothing until `300`:

| `gap` | result | |
|---|---|---|
| `0` | `[0,10] [10.4,20.4] [20.4,30.4] [300,…]` | every jitter hole becomes a range — ~8,640 per camera-day, and a striped mess on screen |
| `3` | `[0,30.4] [300,…]` | jitter absorbed, the real 4.5-minute outage still visible ✅ |
| `600` | `[0,…]` | the outage disappears — the timeline lies |

**Send `gap=3` unless you have a reason not to.** It is the default.

One implementation detail that leaks: ranges are precomputed merged at **2.0s**.
Any `gap >= 2.0` is served from that precomputation. A `gap < 2.0` still works
and is still correct, but takes a slower path — avoid it in a hot loop.

---

## 7. Caching

Both range endpoints set `Cache-Control`:

| window | header |
|---|---|
| fully in the past (`before` more than 10 min ago) | `public, max-age=60, stale-while-revalidate=300` |
| still touching now | `public, max-age=5` |

Deliberately **not** `immutable` and not a long max-age: retention deletes
footage out from under past windows, so a day you cached last week can legitimately
shrink. Sixty seconds is the intended ceiling for a past day.

nginx on the device also micro-caches `/api/` JSON for the same lifetime, with
request coalescing, so a burst of identical requests costs one query.

---

## 8. `GET /api/recordings/hours` — coarse but cheap over long windows

24 booleans per camera-day. Use it for a calendar, or to paint a month view
before exact ranges arrive. **Do not** use it to draw the scrub bar — it has
hour resolution.

### Request

```
GET /api/recordings/hours?cameras=front,back&after=...&before=...&variant=sub&timezone=Europe/Warsaw
```

| Param | Default | Meaning |
|---|---|---|
| `cameras` / `after` / `before` / `variant` | as above | |
| `timezone` | `utc` | IANA name; buckets are **local** hours, DST transitions handled |

### Response

```json
{"front": {"2026-09-06": [0,0,1,1,1,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0],
           "2026-09-07": [1,1,1,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0]},
 "back":   {}}
```

Index `i` is local hour `i` of that local day. Days with no footage are omitted
rather than emitted as 24 zeros.

**Known imprecision:** buckets key on a segment's *start* time, so a segment
spanning an hour boundary marks only the hour it began in. Worst case ~10s at a
boundary. Same behaviour as the existing `GET /api/recordings/summary`.

---

## 9. Limits and errors

| Condition | Response |
|---|---|
| `before - after` > **8 days** | `422` with `{"success": false, "message": "..."}` |
| no cameras permitted / empty selection | `200`, empty result |
| no footage | `200`, `[]` or `{}` |
| unknown `variant` value | `422` (FastAPI validation) |

The 8-day cap exists because an uncovered window falls back to merging raw
segments, and a month across every camera is millions of rows. **Draw a day at a
time.** For anything longer, use `/api/recordings/hours`.

---

## 10. Semantics you must not assume away

Three behaviours that will produce subtle bugs if you "normalise" them:

1. **Ranges straddling the window edge come back whole, not clipped.** Ask for
   `[midnight, midnight+24h]` and the first range may start at `23:59:52` the
   previous day. Clamp on your side if your renderer needs it. The server does
   not clip, because dropping a segment that starts before `after` leaves a
   visible hole at midnight.
2. **Ranges are merged on a running maximum end.** Segments do overlap in the
   field. If you ever re-merge ranges yourself, use `max(end)` — taking the last
   segment's end drags a range's end backwards and silently loses footage off
   the end of the timeline.
3. **Ranges within a camera are ordered by `start` and never overlap each
   other.** Safe to render sequentially. Ordering *across* cameras is not
   meaningful.

---

## 11. How the answer is produced (and why you never have to wait for it)

Ranges are precomputed on the device, not merged per request:

- A background thread that was already running merges settled windows into a
  small table roughly every 5 minutes, and walks history backwards a day per
  hour.
- A separate table records **how much of the timeline that precomputation
  actually speaks for**, so "no footage" is never confused with "not computed
  yet".
- Any window outside that coverage is merged live from the segment rows with the
  same query. **Answers are correct from the very first request.**

What this means for you operationally:

- **Nothing to trigger, nothing to wait for, no warm-up call.** Deploy and use it.
- A freshly upgraded device is simply *slower* for the older days the backfill
  has not reached yet — same answer, more work. Expect a device to be fully
  precomputed within about a day of uptime.
- The segment write path is untouched, so this adds no per-recording cost.

---

## 12. Worked example — a 32-camera day view

```
1. GET /api/config
   → read fork.features; if "recordings_ranges" is absent, use the legacy path.

2. GET /api/recordings/hours?cameras=all&after=<month start>&before=<month end>
        &timezone=<device local>
   → paint the calendar; mark which days have anything.

3. User picks a day. Compute the local-day window [after, before] in unix seconds.

   GET /api/recordings/ranges?cameras=all&after=<day start>&before=<day end>
        &variant=sub&gap=3
   → one request, ~64 kB. Draw 32 strips from response.ranges.
     Record response.variants[camera] alongside each strip.

4. User scrubs on a camera. Play from the VOD endpoints as today, using the
   variant you recorded in step 3 — path form, not query form:

   /vod/{camera}/start/{s}/end/{e}/{variant}/index.m3u8

5. User jumps to a different day → back to step 3. Past days are cacheable for
   60s; today should be refetched.
```

Padding note: you no longer need to pad the window by `gap` before calling.
Ranges come back unclipped, so a range straddling midnight is already whole.

---

## 13. Migration checklist

- [ ] Read `fork.features` from `/api/config`; keep the legacy segment-merge path
      behind that check until the fleet has upgraded.
- [ ] Replace per-camera `GET /api/{camera}/recordings` timeline calls with
      `GET /api/recordings/ranges` (batch) — this removes the fan-out entirely.
- [ ] Store and use `variants[camera]` / `X-Recording-Variant` when building VOD
      URLs; do not assume the requested variant was served.
- [ ] Send `gap=3` explicitly rather than relying on the default.
- [ ] Cap requested windows at 8 days; use `/api/recordings/hours` for longer.
- [ ] Honour `Cache-Control` rather than caching indefinitely — retention shrinks
      past days.
- [ ] If you have a device-side agent doing this merge today, it can be retired
      for fork devices; the fork answers for both variants, which the agent
      could not.
