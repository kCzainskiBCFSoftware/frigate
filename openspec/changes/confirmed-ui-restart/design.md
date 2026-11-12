# Design

## Context

See proposal.md (Why) for the failure. These facts about the current code
shape the approach:

- **How a restart runs.** `restart_frigate()` (`frigate/util/services.py`)
  sends SIGTERM to PID 1 when that is `s6-svscan`. s6 then stops the services
  in reverse dependency order and the container exits. Docker's restart policy
  starts it again, and the config is read fresh. The dependencies are
  `nginx/dependencies.d/frigate` and `frigate/dependencies.d/go2rtc`, so s6
  stops **nginx first**, then Frigate (`frigate/timeout-kill` = 120 s), then
  go2rtc (30 s).
- **When the API answers.** `uvicorn.run()` is the last step of
  `FrigateApp.start()`. A 200 from the API therefore means every Frigate
  process has started. The WebSocket server starts earlier, in
  `init_dispatcher()`, so a reconnected WebSocket does not mean that Frigate is
  up.
- **`GET /api/version`.** It is public (`allow_public`, and listed in the auth
  exempt paths), so it answers without a valid session. nginx neither logs it
  (`access_log off`) nor caches it (it is `text/plain`, and `proxy_no_cache`
  skips anything that is not JSON). It inherits `Cache-Control: no-store` from
  the `/api/` block.
- **Worker threads.** Every request through nginx first runs the `/auth`
  subrequest. `auth()` is a sync handler, so it needs a free thread in the API
  thread pool. Making `restart()` async would therefore not let a restart past
  a full pool.
- **Web tests.** `web/vite.config.ts` points `setupFiles` at
  `web/__test__/test-setup.ts`, which upstream deleted in 2023. No web unit
  tests exist today.

## Goals / Non-Goals

**Goals:**

- The page reloads only after the browser has seen Frigate stop and then
  answer again.
- A restart that did not happen is reported as such, with a way to retry.
- The upstream diff stays small: the logic lives in a new fork-only module, and
  the upstream files only wire it up and render it.

**Non-Goals:**

- Restarting a Frigate whose API cannot answer at all, for example because
  every worker thread is stuck. The UI reports "did not restart" and no more.
  The inter-process thread failure from the incident is fixed separately by
  `ipc-request-resilience`.
- The WebSocket send path for other topics. Camera toggles sent while the
  socket is closed are dropped the same way. That is a possible follow-up.
- `POST /api/config/save?save_option=restart`, and the WebSocket and MQTT
  `restart` topics. They keep their current behavior.
- Recording variants (`main`/`sub`) are not involved: the change touches no
  recording, playback, export or retention path.

## Decisions

### 1. Restart over HTTP, not over the WebSocket

`RestartDialog` sends `POST /api/restart` with axios. The existing default
headers apply, including `X-CSRF-TOKEN`, and the request times out after 10 s.
How the reply is handled:

| Reply | Result |
|---|---|
| 200 | accepted, start confirming |
| 401, 403 | "not allowed", stop |
| other 4xx, 500 | "could not restart", stop |
| network error, timeout, 502, 503, 504 | possibly started, start confirming |

Alternatives considered:

- *Queue the WebSocket message until the socket opens*
  (`sendJsonMessage(msg, true)`). This still gives no reply. A queued message
  is also lost if the page reloads, and the UI still cannot tell a lost restart
  from a real one.
- *Send over both channels.* Both paths end in the same `restart_frigate()` in
  the same process, so the second adds no resilience. It would only make it
  unclear which one worked.

### 2. Confirm by watching `/api/version` go down and come back

The monitor probes `GET /api/version` one probe at a time: each probe starts
about 1 s after the previous one ends. A probe succeeds only on a 200 within
5 s. The restart counts as **stopped** after two failed probes in a row, and
as **back** on the first success after that. Then the dialog sets
`window.location.href = redirectUrl`.

- Two failures, not one, so that a single network glitch does not count as a
  restart. The real down window cannot be missed by two probes. It spans the
  nginx stop, the Frigate stop (seconds, or up to 120 s when wedged), the
  container exit and restart, and Frigate's start up to uvicorn. That is never
  shorter than several seconds.
- Time is read from `Date.now()`, not by counting ticks. A throttled
  background tab can then only delay the reload; it cannot shift the timeouts.

Alternatives considered:

- *WebSocket close and reopen.* The WebSocket server starts before the API,
  and `react-use-websocket` retries on its own schedule. Not a reliable signal.
- *Compare `service.uptime` from `/api/stats` before and after.* This would
  prove a restart even if the down window were missed. But it needs a valid
  session after the restart, and the stats are empty until the stats emitter
  first runs. The down window is long enough that this extra proof is not
  needed.

### 3. Timeouts: 60 s to stop, a notice after 6 min to start

- **Stop within 60 s of the request, or report "did not restart".** The
  incident analysis suggested ~180 s, assuming a wedged `FrigateApp.stop()`
  delays the API going dark. The s6 order says it does not: nginx is stopped before Frigate, so the
  API goes dark within seconds of the SIGTERM to PID 1. A wedged stop makes the
  down phase longer, not its start later. 60 s leaves a wide margin for a
  loaded NAS, and a user waits one minute, not three, for a restart that did
  not happen.
- **Back within 6 min of stopping, or show "taking longer than expected" and
  keep waiting.** The worst case is about 5 min: a 120 s Frigate kill timeout,
  plus 30 s for go2rtc, plus the container restart, plus 1–2 min to start
  32 cameras on the 4-core NAS. The UI does not give up, because Frigate does come
  back in the end: with an invalid config it starts in safe mode.
- The constants sit together at the top of the monitor module.

### 4. The endpoint replies, then restarts 1 s later on a timer

`restart()` in `frigate/api/app.py` starts a daemon `threading.Timer(1.0, …)`
that runs `restart_frigate()` and logs any exception. It then returns the
existing 200 body, or 500 if the timer could not be started. Today the SIGTERM
goes out while the reply is still on its way to the browser, and s6 begins
stopping nginx at once. nginx's fast shutdown closes open connections, so on a
loaded NAS the reply can be lost.

The client copes with a lost reply anyway (Decision 1), so the delay is not
needed for correctness. It makes the common path explicit: the user sees
"accepted" and not "connection lost". An error from `restart_frigate()` inside
the timer is no longer returned as 500. The UI still catches it with the
60 s "did not restart" check.

Alternatives considered:

- *`loop.call_later` in an async handler.* This frees no worker thread, because
  of `/auth` (see Context), and it would make the handler async for no gain.
- *A background task that runs after the response.* Starlette runs
  `BackgroundTasks` after the body is sent to nginx, not after nginx has sent
  it on to the browser. It still needs a delay, so it is no simpler than a
  timer.

Two tabs, or a retry during a slow stop, can send SIGTERM to PID 1 twice. The
second one is harmless, because the shutdown is already running.

### 5. Code layout

- `web/src/utils/restart-monitor.ts` (new, fork-only):
  `runRestart({ post, probe, sleep, now, signal }, onPhase)`. It has no React
  code. The phases are `requesting`, `waitingForStop`, `waitingForStart`,
  `slowStart`, `back`, `notRestarted`, `notAllowed` and `failed`, each with the
  elapsed time. The axios calls are passed in, so the tests can use fake ones
  and fake timers.
- `RestartDialog.tsx` keeps its confirm dialog and its sheet. The countdown
  effect is replaced: on confirm, the dialog runs the monitor with an
  `AbortController`, which is aborted on unmount. The sheet shows the phase
  text and the elapsed time. It keeps "Force Reload Now" in every phase, and
  adds Retry and Close to `notRestarted`, `notAllowed` and `failed`. The
  `onRestart` prop is removed.
- `GeneralSettings.tsx` and `ConfigEditor.tsx` drop `useRestart` and
  `onRestart`. `redirectUrl` stays as it is. `ws.tsx` is not edited.
- `en/components/dialog.json` gets new keys under `restart.restarting`. The
  existing `content` key, with its `{{countdown}}`, stays unused, so no
  translation breaks. Other locales fall back to English for the new keys.

## Risks / Trade-offs

- [A reverse proxy in front of Frigate answers 200 with its own page while
  Frigate is down] → The UI reloads early and shows that page. This is no worse
  than today's countdown. Accepted. If it happens in the field, also require a
  `text/plain` content type.
- [The API is wedged and cannot take the POST] → The POST times out, the probes
  keep succeeding or keep timing out, and after 60 s the UI says Frigate did not
  restart. The UI tells the truth but cannot fix it. Recovery from a wedged
  process belongs to the backend (`ipc-request-resilience` and possible
  watchdog work).
- [`restart_frigate()` fails inside the timer] → It is logged, and the UI shows
  "did not restart" after 60 s instead of an immediate 500.
- [s6 is not PID 1, as in a dev run] → `restart_frigate()` sends SIGINT to the
  process, and nothing starts it again. The UI shows "taking longer than
  expected" and waits. Dev only.
- [An upstream rewrite of `RestartDialog.tsx`] → Redo the wiring by hand. The
  logic sits in `restart-monitor.ts` and is not affected.
- [vitest cannot start because the deleted setup file is missing] → Add an
  empty `web/__test__/test-setup.ts`. It is a new fork file, so it cannot
  conflict.

## Migration Plan

The UI and the endpoint ship in the same image, and nothing is persisted.
Rolling out means updating the image. Rolling back means running the previous
image.
