from enum import Enum
from typing import Literal, Optional, Union

from pydantic import Field, field_validator

from frigate.const import DEFAULT_FFMPEG_VERSION, INCLUDED_FFMPEG_VERSIONS

from ..base import FrigateBaseModel
from ..env import EnvString

__all__ = [
    "CameraFfmpegConfig",
    "CameraInput",
    "CameraRoleEnum",
    "FfmpegConfig",
    "FfmpegOutputArgsConfig",
]

# Note: Setting threads to less than 2 caused several issues with recording segments
# https://github.com/blakeblackshear/frigate/issues/5659
FFMPEG_GLOBAL_ARGS_DEFAULT = ["-hide_banner", "-loglevel", "warning", "-threads", "2"]
FFMPEG_INPUT_ARGS_DEFAULT = "preset-rtsp-generic"

RECORD_FFMPEG_OUTPUT_ARGS_DEFAULT = "preset-record-generic-audio-aac"
DETECT_FFMPEG_OUTPUT_ARGS_DEFAULT = [
    "-threads",
    "2",
    "-f",
    "rawvideo",
    "-pix_fmt",
    "yuv420p",
]


class FfmpegOutputArgsConfig(FrigateBaseModel):
    detect: Union[str, list[str]] = Field(
        default=DETECT_FFMPEG_OUTPUT_ARGS_DEFAULT,
        title="Detect role FFmpeg output arguments.",
    )
    record: Union[str, list[str]] = Field(
        default=RECORD_FFMPEG_OUTPUT_ARGS_DEFAULT,
        title="Record role FFmpeg output arguments.",
    )


class FfmpegConfig(FrigateBaseModel):
    path: str = Field(default="default", title="FFmpeg path")
    global_args: Union[str, list[str]] = Field(
        default=FFMPEG_GLOBAL_ARGS_DEFAULT, title="Global FFmpeg arguments."
    )
    hwaccel_args: Union[str, list[str]] = Field(
        default="auto", title="FFmpeg hardware acceleration arguments."
    )
    input_args: Union[str, list[str]] = Field(
        default=FFMPEG_INPUT_ARGS_DEFAULT, title="FFmpeg input arguments."
    )
    output_args: FfmpegOutputArgsConfig = Field(
        default_factory=FfmpegOutputArgsConfig,
        title="FFmpeg output arguments per role.",
    )
    retry_interval: float = Field(
        default=10.0,
        title="Time in seconds to wait before FFmpeg retries connecting to the camera.",
        gt=0.0,
    )
    apple_compatibility: bool = Field(
        default=False,
        title="Set tag on HEVC (H.265) recording stream to improve compatibility with Apple players.",
    )
    gpu: int = Field(default=0, title="GPU index to use for hardware acceleration.")

    @property
    def ffmpeg_path(self) -> str:
        if self.path == "default":
            return f"/usr/lib/ffmpeg/{DEFAULT_FFMPEG_VERSION}/bin/ffmpeg"
        elif self.path in INCLUDED_FFMPEG_VERSIONS:
            return f"/usr/lib/ffmpeg/{self.path}/bin/ffmpeg"
        else:
            return f"{self.path}/bin/ffmpeg"

    @property
    def ffprobe_path(self) -> str:
        if self.path == "default":
            return f"/usr/lib/ffmpeg/{DEFAULT_FFMPEG_VERSION}/bin/ffprobe"
        elif self.path in INCLUDED_FFMPEG_VERSIONS:
            return f"/usr/lib/ffmpeg/{self.path}/bin/ffprobe"
        else:
            return f"{self.path}/bin/ffprobe"


class CameraRoleEnum(str, Enum):
    audio = "audio"
    record = "record"
    detect = "detect"


DEFAULT_RECORD_VARIANT = "main"

# Plain strings (not an Enum) so f-string interpolation into cache filenames
# and DB rows stays "main"/"sub" on all Python versions.
RecordVariant = Literal["main", "sub"]


class CameraInput(FrigateBaseModel):
    path: EnvString = Field(title="Camera input path.")
    roles: list[CameraRoleEnum] = Field(title="Roles assigned to this input.")
    global_args: Union[str, list[str]] = Field(
        default_factory=list, title="FFmpeg global arguments."
    )
    hwaccel_args: Union[str, list[str]] = Field(
        default_factory=list, title="FFmpeg hardware acceleration arguments."
    )
    input_args: Union[str, list[str]] = Field(
        default_factory=list, title="FFmpeg input arguments."
    )
    record_variant: RecordVariant = Field(
        default=DEFAULT_RECORD_VARIANT,
        title="Recording variant label for this input (only used when 'record' role is assigned).",
    )
    retain_days: Optional[float] = Field(
        default=None,
        ge=0,
        title="Override continuous retention days for this variant (only used when 'record' role is assigned).",
    )


class CameraFfmpegConfig(FfmpegConfig):
    inputs: list[CameraInput] = Field(title="Camera inputs.")

    @field_validator("inputs")
    @classmethod
    def validate_roles(cls, v):
        non_record_roles = [
            role
            for input in v
            for role in input.roles
            if role != CameraRoleEnum.record
        ]

        if len(non_record_roles) != len(set(non_record_roles)):
            raise ValueError(
                "Each non-record input role may only be used once per camera."
            )

        if CameraRoleEnum.detect not in non_record_roles:
            raise ValueError("The detect role is required.")

        record_variants = [
            input.record_variant for input in v if CameraRoleEnum.record in input.roles
        ]
        if len(record_variants) != len(set(record_variants)):
            raise ValueError(
                "Each record_variant may only be used once per camera (assign distinct record_variant values to record inputs)."
            )

        # legacy recordings and pre-existing DB rows are variant=main; requiring a
        # main record input guarantees they keep being retained/expired and that
        # main/sub fallback always has a target
        if record_variants and DEFAULT_RECORD_VARIANT not in record_variants:
            raise ValueError(
                "One record input must use record_variant 'main' (it is the fallback and legacy variant)."
            )

        return v
