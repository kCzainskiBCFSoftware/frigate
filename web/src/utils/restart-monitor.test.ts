import { describe, expect, it, vi } from "vitest";
import {
  PROBE_INTERVAL_MS,
  RestartPhase,
  RestartPhaseName,
  isRestartSettled,
  runRestart,
} from "./restart-monitor";

const MINUTE = 60_000;
const NETWORK_ERROR = new Error("Network Error");

/** A clock that only moves when the code under test sleeps or a fake waits. */
function makeClock() {
  let t = 1_000_000;
  return {
    now: () => t,
    advance: (ms: number) => {
      t += ms;
    },
    sleep: async (ms: number) => {
      t += ms;
    },
  };
}

type Reply = number | Error;

/**
 * A fake Frigate. `reply` decides what each probe gets from the time since the
 * run began; every probe takes `latencyMs` of fake time.
 */
function setup(options: {
  post: Reply;
  probe?: (sinceStartMs: number) => Reply;
  latencyMs?: number;
}) {
  const clock = makeClock();
  const startedAt = clock.now();
  const controller = new AbortController();
  const phases: RestartPhase[] = [];
  /** How often it had slept when each phase was reported. */
  const sleepsAtPhase: number[] = [];

  const post = vi.fn(async () => {
    if (options.post instanceof Error) {
      throw options.post;
    }
    return options.post;
  });
  const probe = vi.fn(async () => {
    clock.advance(options.latencyMs ?? 20);
    const reply = (options.probe ?? (() => 200))(clock.now() - startedAt);
    if (reply instanceof Error) {
      throw reply;
    }
    return reply;
  });

  const sleep = vi.fn(clock.sleep);

  const run = () =>
    runRestart(
      {
        post,
        probe,
        sleep,
        now: clock.now,
        signal: controller.signal,
      },
      (phase) => {
        phases.push(phase);
        sleepsAtPhase.push(sleep.mock.calls.length);
      },
    );

  /** The phases in the order they first appeared. */
  const names = (): RestartPhaseName[] =>
    phases
      .map((p) => p.phase)
      .filter((name, i, all) => i === 0 || name !== all[i - 1]);
  const last = () => phases[phases.length - 1];

  return {
    clock,
    controller,
    phases,
    sleepsAtPhase,
    post,
    probe,
    run,
    names,
    last,
  };
}

/** Frigate answers, is down from `downAt` until `upAt`, then answers again. */
const downBetween =
  (downAt: number, upAt: number, whileDown: Reply = NETWORK_ERROR) =>
  (t: number): Reply =>
    t >= downAt && t < upAt ? whileDown : 200;

describe("runRestart: the restart request", () => {
  it("goes through requesting, waiting for stop, waiting for start, back", async () => {
    const t = setup({ post: 200, probe: downBetween(3_000, 12_000) });
    await t.run();

    expect(t.post).toHaveBeenCalledTimes(1);
    expect(t.names()).toEqual([
      "requesting",
      "waitingForStop",
      "waitingForStart",
      "back",
    ]);
    expect(t.last().phase).toBe("back");
  });

  it.each([401, 403])(
    "reply %i is not allowed, and nothing is probed",
    async (status) => {
      const t = setup({ post: status });
      await t.run();

      expect(t.names()).toEqual(["requesting", "notAllowed"]);
      expect(t.probe).not.toHaveBeenCalled();
    },
  );

  it.each([400, 404, 422, 500])(
    "reply %i is a failure, and nothing is probed",
    async (status) => {
      const t = setup({ post: status });
      await t.run();

      expect(t.names()).toEqual(["requesting", "failed"]);
      expect(t.probe).not.toHaveBeenCalled();
    },
  );

  it.each([
    ["a network error", NETWORK_ERROR],
    [
      "a timeout",
      Object.assign(new Error("timeout of 10000ms exceeded"), {
        code: "ECONNABORTED",
      }),
    ],
    ["a 502", 502],
    ["a 503", 503],
    ["a 504", 504],
  ] as [string, Reply][])(
    "%s means it may have started, so probing starts",
    async (_label, post) => {
      const t = setup({ post, probe: downBetween(2_000, 9_000) });
      await t.run();

      expect(t.probe).toHaveBeenCalled();
      expect(t.names()).not.toContain("failed");
      expect(t.names()).not.toContain("notAllowed");
      expect(t.last().phase).toBe("back");
    },
  );
});

describe("runRestart: confirming the restart", () => {
  it("does not count a single failed probe as stopped", async () => {
    // fails once at about 3 s, then keeps answering
    const t = setup({
      post: 200,
      probe: (since) => (since >= 3_000 && since < 4_000 ? NETWORK_ERROR : 200),
    });
    await t.run();

    expect(t.names()).toEqual(["requesting", "waitingForStop", "notRestarted"]);
  });

  it("needs the two failures to be in a row", async () => {
    // one failure, a success, another failure, a success, and so on
    let n = 0;
    const t = setup({
      post: 200,
      probe: () => (n++ % 2 === 0 ? 200 : NETWORK_ERROR),
    });
    await t.run();

    expect(t.names()).toEqual(["requesting", "waitingForStop", "notRestarted"]);
  });

  it("counts a reply other than 200 as a failed probe", async () => {
    // nginx is still up and answers 502 while Frigate is down
    const t = setup({ post: 200, probe: downBetween(2_000, 8_000, 502) });
    await t.run();

    expect(t.names()).toEqual([
      "requesting",
      "waitingForStop",
      "waitingForStart",
      "back",
    ]);
  });

  it("reports the stop as soon as it is seen, not one interval later", async () => {
    // down from the start: the second probe sees the stop
    const t = setup({ post: 200, probe: downBetween(0, 20_000) });
    await t.run();

    const i = t.phases.findIndex((p) => p.phase === "waitingForStart");
    // one sleep, between the first and the second probe, and none since
    expect(t.sleepsAtPhase[i]).toBe(1);
  });

  it("probes about once a second", async () => {
    const t = setup({ post: 200, probe: downBetween(0, 10_000) });
    await t.run();

    // one probe per ~1 s of the 10 s that Frigate is down, and the one after
    const expected = 10_000 / (PROBE_INTERVAL_MS + 20);
    expect(t.probe.mock.calls.length).toBeGreaterThanOrEqual(expected);
    expect(t.probe.mock.calls.length).toBeLessThanOrEqual(expected + 3);
  });

  it("reports the elapsed time since the request", async () => {
    const t = setup({ post: 200, probe: downBetween(0, 5_000) });
    await t.run();

    expect(t.phases[0].elapsedMs).toBe(0);
    const elapsed = t.phases.map((p) => p.elapsedMs);
    expect([...elapsed].sort((a, b) => a - b)).toEqual(elapsed);
    expect(t.last().elapsedMs).toBeGreaterThanOrEqual(5_000);
  });

  it("keeps waiting through a slow shutdown, with no early back", async () => {
    // down at once, back only after 3 minutes
    const t = setup({ post: 200, probe: downBetween(500, 3 * MINUTE) });
    await t.run();

    expect(t.names()).toEqual([
      "requesting",
      "waitingForStop",
      "waitingForStart",
      "back",
    ]);
    expect(t.last().elapsedMs).toBeGreaterThanOrEqual(3 * MINUTE);
  });

  it("reports a restart that did not happen after 60 s", async () => {
    const t = setup({ post: 200 });
    await t.run();

    expect(t.names()).toEqual(["requesting", "waitingForStop", "notRestarted"]);
    expect(t.last().elapsedMs).toBeGreaterThanOrEqual(MINUTE);
    // it stops right there: no probing after the verdict
    expect(t.last().elapsedMs).toBeLessThan(MINUTE + 5_000);
  });

  it("does not report a stop seen just past the deadline as not restarted", async () => {
    const t = setup({
      post: 200,
      probe: downBetween(MINUTE - 1_000, 2 * MINUTE),
    });
    await t.run();

    expect(t.names()).not.toContain("notRestarted");
    expect(t.last().phase).toBe("back");
  });

  it("says the start is slow after 6 minutes down, and still reloads when it is back", async () => {
    // stops at once and answers again after 8 minutes
    const t = setup({ post: 200, probe: downBetween(500, 8 * MINUTE) });
    await t.run();

    expect(t.names()).toEqual([
      "requesting",
      "waitingForStop",
      "waitingForStart",
      "slowStart",
      "back",
    ]);
    const slow = t.phases.find((p) => p.phase === "slowStart");
    expect(slow?.elapsedMs).toBeGreaterThanOrEqual(6 * MINUTE);
    expect(t.last().elapsedMs).toBeGreaterThanOrEqual(8 * MINUTE);
  });

  it("measures the slow start from when it stopped, not from the request", async () => {
    // keeps answering for 40 s, then is down for 5 min: not slow yet
    const t = setup({
      post: 200,
      probe: downBetween(40_000, 40_000 + 5 * MINUTE),
    });
    await t.run();

    expect(t.names()).not.toContain("slowStart");
    expect(t.last().phase).toBe("back");
  });
});

describe("runRestart: abort", () => {
  it("stops probing and reports nothing more once aborted", async () => {
    const t = setup({ post: 200 });
    const run = t.run();

    // let a few probes happen, then abort
    while (t.probe.mock.calls.length < 3) {
      await Promise.resolve();
    }
    t.controller.abort();
    await run;

    const probes = t.probe.mock.calls.length;
    const reported = t.phases.length;
    expect(probes).toBeLessThan(10);
    expect(t.names()).not.toContain("notRestarted");
    expect(t.names()).not.toContain("back");

    // the loop has ended: nothing runs on
    await new Promise((resolve) => setTimeout(resolve, 10));
    expect(t.probe.mock.calls.length).toBe(probes);
    expect(t.phases.length).toBe(reported);
  });

  it("does not probe when aborted during the request", async () => {
    const t = setup({ post: 200 });
    t.post.mockImplementationOnce(async () => {
      t.controller.abort();
      return 200;
    });
    await t.run();

    expect(t.probe).not.toHaveBeenCalled();
    expect(t.names()).toEqual(["requesting"]);
  });
});

describe("isRestartSettled", () => {
  it("is true for the phases that wait for the user, false for the rest", () => {
    const settled: RestartPhaseName[] = [
      "back",
      "notRestarted",
      "notAllowed",
      "failed",
    ];
    const running: RestartPhaseName[] = [
      "requesting",
      "waitingForStop",
      "waitingForStart",
      "slowStart",
    ];
    settled.forEach((p) => expect(isRestartSettled(p)).toBe(true));
    running.forEach((p) => expect(isRestartSettled(p)).toBe(false));
  });
});
