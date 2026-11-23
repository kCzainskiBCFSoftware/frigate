# Proposal: a UI restart that cannot be silently lost

## Why

On field devices, a restart from the web UI often has no effect: config edits
are not applied, and a wedged Frigate stays wedged. Most recently, recording
stopped after an `IntegrityError` ended the main process's inter-process
request thread. Only a `docker compose` restart helped, and end users have no
Docker access.

The restart itself works. The UI sends it from the menu and from the config
editor as a WebSocket message (`useRestart`), and `ws.tsx` drops the message
without an error unless the socket is open. `RestartDialog.tsx` then counts
down 60 s and reloads either way, so a lost restart looks exactly like a real
one.

## What Changes

- Both restart entry points request the restart with `POST /api/restart`
  (existing, admin-only) instead of the WebSocket. The request no longer depends
  on the WebSocket state.
- The UI shows a rejected request as an error: 401/403 as "not allowed", other
  error replies as "could not restart". A network error, a timeout, or a
  502/503/504 counts as "the restart may have started" and goes to the
  confirmation step.
- The UI confirms the restart instead of counting down. It polls the public
  `GET /api/version` until Frigate has stopped answering and then until it
  answers again, and only then reloads the page. It shows the elapsed time and
  what it is waiting for.
- If Frigate does not stop within 60 s of the request, the UI says that Frigate
  did not restart and offers Retry. If Frigate stops but takes more than 6 min
  to come back, the UI says so and keeps waiting.
- `POST /api/restart` replies first and starts the shutdown about 1 s later, on
  a timer, so the reply gets through nginx before s6 stops nginx. The reply's
  status codes and body do not change. Not breaking: external clients only see
  the restart begin about 1 s later.
- The config-editor redirect (`web-navigation`) is kept: the automatic reload,
  now at "Frigate is back" instead of "countdown reached 0", and the forced
  reload both go to `/config`.

Not changed: the WebSocket and MQTT `restart` topics (`handle_restart` in the
dispatcher), `POST /api/config/save?save_option=restart`, and the WebSocket
send path for other topics. No new endpoint, so no new `fork` feature flag.

## Capabilities

### New Capabilities

- `web-restart`: how the web UI restarts Frigate, confirms that the restart
  really happened, and reports a restart that did not happen; and how
  `POST /api/restart` replies before it restarts.

### Modified Capabilities

- `web-navigation`: "Restart from the config editor returns to the config
  editor" is reworded for the new flow. The automatic reload happens once
  Frigate is back, not at the end of a countdown. The redirect targets do not
  change.

## Impact

**Code**

- `web/src/components/overlay/dialog/RestartDialog.tsx`: sends the request and
  shows progress and failures, instead of the countdown. Loses the `onRestart`
  prop.
- `web/src/components/menu/GeneralSettings.tsx`, `web/src/pages/ConfigEditor.tsx`:
  stop using `useRestart` and stop passing `onRestart`.
- New `web/src/utils/restart-monitor.ts` (fork-only): the request and the
  down/up polling as a plain function, with unit tests next to it.
- `web/public/locales/en/components/dialog.json`: new keys under
  `restart.restarting`. Existing keys stay, so other locales do not break.
- `frigate/api/app.py`: `restart()` starts `restart_frigate()` on a 1 s
  timer instead of calling it directly.
- `frigate/test/http_api/test_http_app.py`: endpoint test.
- `ra-docs/fork-changes.md`: new entry. No client guide in `ra-docs/`
  mentions `/api/restart`, so none needs changes.

**Upstream merge impact.** All edited files except the new monitor module are
upstream files:

- `RestartDialog.tsx` is the largest change. The fork already changed it
  (`redirectUrl`), and upstream rarely touches it. Medium risk: a conflict, if
  any, is a rewrite to redo by hand.
- `GeneralSettings.tsx`: three removed lines. Upstream edits this menu often,
  but conflicts here are trivial.
- `ConfigEditor.tsx`: three removed lines next to the fork's existing
  `redirectUrl` change. Low.
- `en/components/dialog.json`: added keys only. Upstream's Weblate commits
  touch locale files often, but rarely this block. Low.
- `frigate/api/app.py`: one handler body. Low.
- `ws.tsx` is not edited. `useRestart` stays in place with no callers, so
  upstream changes to it still merge cleanly.

**Database.** No migration.

**Cost on the NAS.** While a restart dialog is open, each browser polls
`GET /api/version` about once a second. The reply is a short plain-text string
and nginx neither caches nor logs it. No effect on recording, playback, the
database or nginx config.

**Mixed fleet.** The UI is served by the same device it restarts, so the UI and
the endpoint always match. No client-visible API change.
