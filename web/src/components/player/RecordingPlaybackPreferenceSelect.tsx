import { useTranslation } from "react-i18next";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { RecordingPlaybackPreference } from "@/types/record";
import { cn } from "@/lib/utils";

type RecordingPlaybackPreferenceSelectProps = {
  value: RecordingPlaybackPreference;
  onChange: (value: RecordingPlaybackPreference) => void;
  className?: string;
  disabled?: boolean;
};

export default function RecordingPlaybackPreferenceSelect({
  value,
  onChange,
  className,
  disabled,
}: RecordingPlaybackPreferenceSelectProps) {
  const { t } = useTranslation(["components/player"]);

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <div className={cn("inline-flex", className)}>
          <Select
            value={value}
            onValueChange={(v) => onChange(v as RecordingPlaybackPreference)}
            disabled={disabled}
          >
            <SelectTrigger
              aria-label={t("playbackVariant.label")}
              className="h-8 w-28 text-xs"
            >
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="auto">{t("playbackVariant.auto")}</SelectItem>
              <SelectItem value="main">{t("playbackVariant.main")}</SelectItem>
              <SelectItem value="sub">{t("playbackVariant.sub")}</SelectItem>
            </SelectContent>
          </Select>
        </div>
      </TooltipTrigger>
      <TooltipContent>{t("playbackVariant.tooltip")}</TooltipContent>
    </Tooltip>
  );
}
