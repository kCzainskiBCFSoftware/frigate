# Tasks

The groups land in order. Group 1 (backend) and group 2 (monitor module) do
not depend on each other. Group 3 needs group 2.

## 1. Endpoint: reply, then restart

- [x] 1.1 In `frigate/api/app.py`, change `restart()` to start a daemon `threading.Timer(1.0, …)` that calls `restart_frigate()` and logs any exception, then return the existing 200 body. Return the existing 500 body only if the timer cannot be started. Verify by reading the diff: one handler body changes, the route and auth dependency do not.
- [x] 1.2 Add tests to `frigate/test/http_api/test_http_app.py`, with `restart_frigate` patched in `frigate.api.app`. An admin `POST /restart` gets 200 with the unchanged body, and `restart_frigate` has not been called when the reply arrives but has been called about 1 s later. A viewer gets 403 and `restart_frigate` is never called. A failing `Timer.start` gives 500. Verify the module passes in the Docker test image.
- [x] 1.3 Run the backend lint gate on the touched files in the container: `ruff check --select E4,E7,E9,F,I --ignore E501,E711,E712 frigate/` and `ruff format --check frigate/api/app.py frigate/test/http_api/test_http_app.py`. Verify both are clean.

## 2. Restart monitor module

- [x] 2.1 Create `web/src/utils/restart-monitor.ts` with `runRestart(deps, onPhase)`, following design Decisions 1–3. `deps` holds `post`, `probe`, `sleep`, `now` and an `AbortSignal`. Use the phases from Decision 5. Put the constants at the top: 10 s request timeout, 1 s probe interval, 5 s probe timeout, 2 failures for "stopped", 60 s stop deadline, 6 min slow-start notice. Verify `npx tsc --noEmit` passes in `web/`.
- [x] 2.2 Make vitest runnable. `web/vite.config.ts` points `setupFiles` at `web/__test__/test-setup.ts`, which does not exist. If `npx vitest run` fails on it, add an empty `web/__test__/test-setup.ts` (new fork file), and do not edit `vite.config.ts`. Verify `npx vitest run` starts and reports "no test files" or runs.
- [x] 2.3 Add `web/src/utils/restart-monitor.test.ts`, using fake `post`, `probe`, `sleep` and `now` (no real timers or network). Cover one test per `web-restart` scenario:
  - 200 then fail, fail, ok → `back`;
  - 403 and 401 → `notAllowed`, with no probe;
  - 500 and 400 → `failed`, with no probe;
  - a network error, a timeout, and 502/503/504 → probing starts;
  - a single failed probe between successes is not counted as stopped;
  - no stop within 60 s → `notRestarted`;
  - stopped but not back after 6 min → `slowStart`, then `back` when a probe succeeds;
  - abort stops the loop.

  Verify `npx vitest run src/utils/restart-monitor.test.ts` passes.

## 3. Dialog and entry points

- [x] 3.1 Rework `web/src/components/overlay/dialog/RestartDialog.tsx` as in design Decision 5. Remove the `onRestart` prop. On confirm, run `runRestart` with axios (`axios.post("restart", null, { timeout })`, `axios.get("version", { timeout })`) and an `AbortController` that is aborted on unmount. Replace the countdown with phase text and the elapsed time. Keep "Force Reload Now" in every phase. Add Retry and Close to `notRestarted`, `notAllowed` and `failed`. On `back`, set `window.location.href = redirectUrl`. Verify `npm run lint` and `npx tsc --noEmit` pass.
- [x] 3.2 Remove `useRestart`, `sendRestart` and the `onRestart` prop from `web/src/components/menu/GeneralSettings.tsx` and `web/src/pages/ConfigEditor.tsx`. Keep `redirectUrl={`${baseUrl}config`}` in the config editor. Leave `web/src/api/ws.tsx` untouched. Verify `git diff --stat` shows only those lines in both files, and that lint passes.
- [x] 3.3 Add the new strings under `restart.restarting` in `web/public/locales/en/components/dialog.json`: waiting for stop, waiting for start, slow start, did not restart, not allowed, could not restart, Retry, Close, and the elapsed-time label. Keep the existing `content` key. Verify every new `t("restart.…")` key in the dialog exists in the file and the JSON parses.
- [ ] 3.4 Manual check against a running dev build (`npm run dev` proxied to a Frigate container under s6). Restart from the menu: the sheet shows stopping, then starting, then reloads to `/`. Save & Restart from the config editor reloads to `/config`. With the API container stopped before confirming, the UI says "did not restart" after 60 s, and Retry works once the container is back. Record the result in the PR description.

## 4. Docs and full runs

- [x] 4.1 Update `ra-docs/fork-changes.md`. Add a short section for the HTTP restart with confirmation (dialog, monitor module, endpoint timer), with its own list of touched upstream files and their conflict risk; the existing "Upstream files touched" list belongs to the recording-path strand. Under "Frontend changes", list the new and changed web files. No client guide in `ra-docs/` mentions `/api/restart`; confirm with `grep -rn "api/restart" ra-docs/` and leave the guides unchanged. Verify the new entries name the right files.
- [x] 4.2 Run the full backend suite in the Docker test image. Verify no new failures compared with the base commit; the 4 `TestGo2rtcStreamAccess` errors are known noise.
- [x] 4.3 Run `npm run lint`, `npx tsc --noEmit`, `npx vitest run` and `npm run build` in `web/`. Verify all pass.
- [x] 4.4 Run `openspec validate confirmed-ui-restart --strict`. Verify it passes.

## Workflow follow-up

- Open the PR against `dev-fork`.
- Ask the user before archiving. Before archiving, confirm that `ra-docs/fork-changes.md` reflects the change.
