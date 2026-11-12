// Fork-only module (see ra-docs/fork-changes.md, "Confirmed UI restart").
//
// Restarts Frigate over HTTP and confirms that it really stopped and came back
// before the caller reloads the page. It holds no React code and no network
// code: the caller passes in `post`, `probe`, `sleep` and `now`, so the logic
// can be tested with fake ones.

/** Time to wait for the reply to the restart request. */
export const RESTART_REQUEST_TIMEOUT_MS = 10_000;
/** Pause between the end of one probe and the start of the next. */
export const PROBE_INTERVAL_MS = 1_000;
/** Time to wait for the reply to one probe. */
export const PROBE_TIMEOUT_MS = 5_000;
/** Failed probes in a row that count as "Frigate stopped". */
export const STOPPED_AFTER_FAILURES = 2;
/** Without having seen Frigate stop by then, the restart did not happen. */
export const STOP_DEADLINE_MS = 60_000;
/** Time down after which the start counts as slow. The wait goes on. */
export const SLOW_START_MS = 6 * 60_000;

export type RestartPhaseName =
  | "requesting"
  | "waitingForStop"
  | "waitingForStart"
  | "slowStart"
  | "back"
  | "notRestarted"
  | "notAllowed"
  | "failed";

export type RestartPhase = {
  phase: RestartPhaseName;
  /** Time since the restart request was sent. */
  elapsedMs: number;
};

/** The phases after which nothing more happens until the user acts. */
export function isRestartSettled(phase: RestartPhaseName): boolean {
  return (
    phase === "back" ||
    phase === "notRestarted" ||
    phase === "notAllowed" ||
    phase === "failed"
  );
}

export type RestartDeps = {
  /**
   * Sends the restart request. Resolves with the HTTP status of the reply, and
   * rejects when there is no reply (network error or timeout).
   */
  post: () => Promise<number>;
  /**
   * Sends one `GET /api/version`. Resolves with the HTTP status of the reply,
   * and rejects when there is no reply. It must give up after
   * `PROBE_TIMEOUT_MS`.
   */
  probe: () => Promise<number>;
  sleep: (ms: number) => Promise<void>;
  /** Milliseconds, as `Date.now()`. Read for every decision, never counted. */
  now: () => number;
  /** Aborting stops the run without a further callback. */
  signal: AbortSignal;
};

async function statusOf(
  request: () => Promise<number>,
): Promise<number | null> {
  try {
    return await request();
  } catch {
    return null;
  }
}

/**
 * Requests a restart and watches `/api/version` go down and come back.
 *
 * Reports every step through `onPhase`, and a settled phase last. The reply to
 * the request decides whether to wait at all:
 *
 * - 401 and 403: `notAllowed`. Other 4xx and 5xx except 502, 503 and 504:
 *   `failed`. Nothing is waited for.
 * - 2xx, no reply, 502, 503, 504: the restart may have started, so probe.
 */
export async function runRestart(
  { post, probe, sleep, now, signal }: RestartDeps,
  onPhase: (phase: RestartPhase) => void,
): Promise<void> {
  const startedAt = now();
  const report = (phase: RestartPhaseName) => {
    if (!signal.aborted) {
      onPhase({ phase, elapsedMs: now() - startedAt });
    }
  };

  report("requesting");
  const postStatus = await statusOf(post);
  if (signal.aborted) {
    return;
  }

  if (postStatus === 401 || postStatus === 403) {
    report("notAllowed");
    return;
  }
  if (
    postStatus !== null &&
    postStatus >= 400 &&
    postStatus !== 502 &&
    postStatus !== 503 &&
    postStatus !== 504
  ) {
    report("failed");
    return;
  }

  let failures = 0;
  let stoppedAt: number | null = null;

  for (;;) {
    if (stoppedAt === null) {
      report("waitingForStop");
    } else if (now() - stoppedAt >= SLOW_START_MS) {
      report("slowStart");
    } else {
      report("waitingForStart");
    }

    const probeStatus = await statusOf(probe);
    if (signal.aborted) {
      return;
    }

    if (probeStatus === 200) {
      if (stoppedAt !== null) {
        report("back");
        return;
      }
      failures = 0;
    } else if (stoppedAt === null) {
      failures += 1;
      if (failures >= STOPPED_AFTER_FAILURES) {
        stoppedAt = now();
        report("waitingForStart");
      }
    }

    // judged after the probe, so that a stop seen just past the deadline
    // is not reported as a restart that did not happen
    if (stoppedAt === null && now() - startedAt >= STOP_DEADLINE_MS) {
      report("notRestarted");
      return;
    }

    await sleep(PROBE_INTERVAL_MS);
    if (signal.aborted) {
      return;
    }
  }
}
