import { ReviewSeverity } from "./review";
import { TimelineType } from "./timeline";

export type RecordingVariant = "main" | "sub";

export type RecordingPlaybackPreference = "auto" | "main" | "sub";

export const RECORDING_PLAYBACK_DEFAULT: RecordingPlaybackPreference = "sub";

export type Recording = {
  id: string;
  camera: string;
  start_time: number;
  end_time: number;
  path: string;
  segment_size: number;
  duration: number;
  motion: number;
  objects: number;
  dBFS: number;
  variant?: RecordingVariant | string;
  codec_name?: string | null;
  width?: number | null;
  height?: number | null;
  bitrate?: number | null;
};

export type RecordingSegment = {
  id: string;
  start_time: number;
  end_time: number;
  motion: number;
  objects: number;
  segment_size: number;
  duration: number;
  variant?: RecordingVariant | string;
  codec_name?: string | null;
  width?: number | null;
  height?: number | null;
  bitrate?: number | null;
};

export type RecordingActivity = {
  [hour: number]: RecordingSegmentActivity[];
};

type RecordingSegmentActivity = {
  date: number;
  count: number;
  hasObjects: boolean;
};

export type RecordingStartingPoint = {
  camera: string;
  startTime: number;
  severity: ReviewSeverity;
  timelineType?: TimelineType;
};

export type RecordingPlayerError = "stalled" | "startup";

export const ASPECT_VERTICAL_LAYOUT = 1.5;
export const ASPECT_PORTRAIT_LAYOUT = 1.333;
export const ASPECT_WIDE_LAYOUT = 2;
