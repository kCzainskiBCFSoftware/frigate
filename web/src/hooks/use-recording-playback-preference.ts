import { useCallback, useMemo } from "react";
import { useUserPersistence } from "@/hooks/use-user-persistence";
import {
  RECORDING_PLAYBACK_DEFAULT,
  RecordingPlaybackPreference,
  RecordingVariant,
} from "@/types/record";

const PERSISTENCE_KEY_PREFIX = "recording-playback";

export type UseRecordingPlaybackPreferenceResult = {
  preference: RecordingPlaybackPreference;
  setPreference: (value: RecordingPlaybackPreference) => void;
  loaded: boolean;
  // The variant string to send to the backend API. "auto" maps to the
  // default playback variant so the server can still serve a single stream.
  variantForApi: RecordingVariant;
};

export default function useRecordingPlaybackPreference(
  camera: string,
): UseRecordingPlaybackPreferenceResult {
  const [storedPreference, setStoredPreference, loaded] =
    useUserPersistence<RecordingPlaybackPreference>(
      `${PERSISTENCE_KEY_PREFIX}-${camera}`,
      RECORDING_PLAYBACK_DEFAULT,
    );

  const preference: RecordingPlaybackPreference =
    storedPreference ?? RECORDING_PLAYBACK_DEFAULT;

  const setPreference = useCallback(
    (value: RecordingPlaybackPreference) => {
      setStoredPreference(value);
    },
    [setStoredPreference],
  );

  const variantForApi: RecordingVariant = useMemo(() => {
    if (preference === "main") {
      return "main";
    }
    return "sub";
  }, [preference]);

  return { preference, setPreference, loaded, variantForApi };
}
