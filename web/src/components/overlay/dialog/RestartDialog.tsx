import { useState, useEffect, useCallback, useRef } from "react";
import axios from "axios";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
  SheetDescription,
} from "@/components/ui/sheet";
import { Button } from "@/components/ui/button";
import ActivityIndicator from "@/components/indicators/activity-indicator";
import { baseUrl } from "@/api/baseUrl";
import {
  PROBE_TIMEOUT_MS,
  RESTART_REQUEST_TIMEOUT_MS,
  RestartPhase,
  isRestartSettled,
  runRestart,
} from "@/utils/restart-monitor";

import { useTranslation } from "react-i18next";

type RestartDialogProps = {
  isOpen: boolean;
  onClose: () => void;
  redirectUrl?: string; // where to reload after restart; defaults to app root
};

// Resolves with the HTTP status of the reply, and rejects when there is none
// (network error or timeout), which is what `runRestart` expects.
async function statusOf(
  request: () => Promise<{ status: number }>,
): Promise<number> {
  try {
    return (await request()).status;
  } catch (error) {
    if (axios.isAxiosError(error) && error.response) {
      return error.response.status;
    }
    throw error;
  }
}

function formatElapsed(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  const minutes = Math.floor(total / 60);
  const seconds = String(total % 60).padStart(2, "0");
  return `${minutes}:${seconds}`;
}

export default function RestartDialog({
  isOpen,
  onClose,
  redirectUrl = baseUrl,
}: RestartDialogProps) {
  const { t } = useTranslation("components/dialog");
  const [restartDialogOpen, setRestartDialogOpen] = useState(isOpen);
  const [restartingSheetOpen, setRestartingSheetOpen] = useState(false);
  const [phase, setPhase] = useState<RestartPhase | undefined>(undefined);
  const [elapsedMs, setElapsedMs] = useState(0);
  const abortRef = useRef<AbortController | null>(null);
  const startedAtRef = useRef(0);

  const clearBodyPointerEvents = () => {
    if (typeof document !== "undefined") {
      document.body.style.pointerEvents = "";
    }
  };

  useEffect(() => {
    setRestartDialogOpen(isOpen);
  }, [isOpen]);

  // stop watching when the dialog goes away
  useEffect(() => {
    return () => abortRef.current?.abort();
  }, []);

  const phaseName = phase?.phase;
  const running = phaseName !== undefined && !isRestartSettled(phaseName);

  // keep the elapsed time moving between phase reports
  useEffect(() => {
    if (!running) {
      return;
    }
    const interval = setInterval(() => {
      setElapsedMs(Date.now() - startedAtRef.current);
    }, 1000);
    return () => clearInterval(interval);
  }, [running]);

  const startRestart = useCallback(() => {
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    const { signal } = controller;

    startedAtRef.current = Date.now();
    setElapsedMs(0);
    setPhase({ phase: "requesting", elapsedMs: 0 });
    setRestartingSheetOpen(true);

    runRestart(
      {
        post: () =>
          statusOf(() =>
            axios.post("restart", null, {
              timeout: RESTART_REQUEST_TIMEOUT_MS,
              signal,
            }),
          ),
        probe: () =>
          statusOf(() =>
            axios.get("version", { timeout: PROBE_TIMEOUT_MS, signal }),
          ),
        sleep: (ms) =>
          new Promise<void>((resolve) => {
            const timer = setTimeout(resolve, ms);
            signal.addEventListener(
              "abort",
              () => {
                clearTimeout(timer);
                resolve();
              },
              { once: true },
            );
          }),
        now: () => Date.now(),
        signal,
      },
      (next) => {
        setPhase(next);
        setElapsedMs(next.elapsedMs);
        if (next.phase === "back") {
          window.location.href = redirectUrl;
        }
      },
    );
  }, [redirectUrl]);

  const handleCloseSheet = () => {
    abortRef.current?.abort();
    setRestartingSheetOpen(false);
    setPhase(undefined);
  };

  const handleForceReload = () => {
    window.location.href = redirectUrl;
  };

  // static keys, so that every string stays easy to find and to extract
  let titleText = t("restart.restarting.title");
  let statusText: string | null = null;
  switch (phaseName) {
    case "requesting":
      statusText = t("restart.restarting.requesting");
      break;
    case "waitingForStop":
      statusText = t("restart.restarting.waitingForStop");
      break;
    case "waitingForStart":
      statusText = t("restart.restarting.waitingForStart");
      break;
    case "slowStart":
      statusText = t("restart.restarting.slowStart");
      break;
    case "back":
      statusText = t("restart.restarting.back");
      break;
    case "notRestarted":
      titleText = t("restart.restarting.notRestarted.title");
      statusText = t("restart.restarting.notRestarted.content");
      break;
    case "notAllowed":
      titleText = t("restart.restarting.notAllowed.title");
      statusText = t("restart.restarting.notAllowed.content");
      break;
    case "failed":
      titleText = t("restart.restarting.failed.title");
      statusText = t("restart.restarting.failed.content");
      break;
  }
  // the phases that stop and wait for the user to retry or close
  const canRetry =
    phaseName === "notRestarted" ||
    phaseName === "notAllowed" ||
    phaseName === "failed";

  return (
    <>
      <AlertDialog
        open={restartDialogOpen}
        onOpenChange={(open) => {
          if (!open) {
            setRestartDialogOpen(false);
            onClose();
            clearBodyPointerEvents();
          }
        }}
      >
        <AlertDialogContent
          onCloseAutoFocus={(event) => {
            event.preventDefault();
            clearBodyPointerEvents();
          }}
        >
          <AlertDialogHeader>
            <AlertDialogTitle>{t("restart.title")}</AlertDialogTitle>
            <AlertDialogDescription className="sr-only">
              {t("restart.description")}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>
              {t("button.cancel", { ns: "common" })}
            </AlertDialogCancel>
            <AlertDialogAction onClick={startRestart}>
              {t("restart.button")}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <Sheet
        open={restartingSheetOpen}
        onOpenChange={(open) => {
          // while the restart is being watched, only the buttons close it
          if (!open && canRetry) {
            handleCloseSheet();
          }
        }}
      >
        <SheetContent
          side="top"
          onInteractOutside={(e) => e.preventDefault()}
          className="[&>button:first-of-type]:hidden"
        >
          <div className="flex flex-col items-center">
            {!canRetry && <ActivityIndicator />}
            <SheetHeader className="mt-5 text-center">
              <SheetTitle className="text-center">{titleText}</SheetTitle>
              <SheetDescription
                className="text-center"
                aria-live="polite"
                asChild
              >
                <div>
                  <div>{statusText}</div>
                  {(running || phaseName === "notRestarted") && (
                    <div className="mt-1 text-xs">
                      {t("restart.restarting.elapsed", {
                        time: formatElapsed(elapsedMs),
                      })}
                    </div>
                  )}
                </div>
              </SheetDescription>
            </SheetHeader>
            <div className="mt-5 flex flex-wrap justify-center gap-2">
              {canRetry && (
                <Button
                  size="lg"
                  variant="select"
                  aria-label={t("restart.restarting.retry")}
                  onClick={startRestart}
                >
                  {t("restart.restarting.retry")}
                </Button>
              )}
              <Button
                size="lg"
                aria-label={t("restart.restarting.button")}
                onClick={handleForceReload}
              >
                {t("restart.restarting.button")}
              </Button>
              {canRetry && (
                <Button
                  size="lg"
                  aria-label={t("restart.restarting.close")}
                  onClick={handleCloseSheet}
                >
                  {t("restart.restarting.close")}
                </Button>
              )}
            </div>
          </div>
        </SheetContent>
      </Sheet>
    </>
  );
}
