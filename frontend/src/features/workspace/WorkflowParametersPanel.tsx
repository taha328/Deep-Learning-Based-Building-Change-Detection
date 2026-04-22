import { type ReactNode } from "react";

import type { BackendAvailability } from "@/api/contracts";
import type { FrontendRuntimeConfig } from "@/lib/env";
import { useAppStore } from "@/app/store";
import { Input } from "@/components/ui/input";
import { Select } from "@/components/ui/select";
import { cn } from "@/lib/utils";
import { useI18n } from "@/lib/i18n";
import { WorkflowSectionCard } from "@/features/workspace/WorkflowSectionCard";

function FieldLabel({ children, htmlFor }: { children: ReactNode; htmlFor?: string }) {
  return (
    <label htmlFor={htmlFor} className="label-xs font-semibold text-muted-foreground uppercase tracking-wider">
      {children}
    </label>
  );
}

export function WorkflowParametersPanel({
  runtimeConfig,
  backendAvailability,
  backendAvailabilityLoading,
  backendAvailabilityError,
  className,
}: {
  runtimeConfig: FrontendRuntimeConfig;
  backendAvailability: BackendAvailability[];
  backendAvailabilityLoading: boolean;
  backendAvailabilityError: string | null;
  className?: string;
}) {
  const { t } = useI18n();
  const state = useAppStore();
  const setSetting = useAppStore((store) => store.setSetting);

  const availabilityByMode = new Map(backendAvailability.map((entry) => [entry.mode, entry]));
  const selectedBackendMode = state.settings.modelBackend === "bandon_mps" ? "bandon_mps" : state.settings.sam3BackendMode;
  const selectedBackendAvailability = availabilityByMode.get(selectedBackendMode);
  const probeMissingForBandon =
    state.settings.modelBackend === "bandon_mps" &&
    backendAvailability.length === 0 &&
    backendAvailabilityError !== null;
  const selectedBackendBlocked =
    probeMissingForBandon || (backendAvailability.length > 0 && selectedBackendAvailability?.available === false);
  const selectedBackendReason =
    (probeMissingForBandon
      ? "Backend capability probe is unavailable. Restart the backend so the frontend can verify BANDON readiness before running."
      : null) ??
    selectedBackendAvailability?.reason ??
    (selectedBackendBlocked ? "The selected backend is currently unavailable." : null);

  return (
    <WorkflowSectionCard
      title={t("settings.panel.parameters")}
      className={cn("border-sidebar-border bg-sidebar", className)}
      contentClassName="space-y-4"
    >
      <div className="space-y-4 rounded border border-sidebar-border bg-sidebar px-4 py-4">
        <div className="space-y-2">
          <div className="flex items-center justify-between text-sm text-foreground">
            <FieldLabel htmlFor="change-threshold">{t("settings.change_threshold")}</FieldLabel>
            <span>{state.settings.changeThreshold.toFixed(2)}</span>
          </div>
          <input
            id="change-threshold"
            type="range"
            min={0}
            max={1}
            step={0.01}
            value={state.settings.changeThreshold}
            onChange={(event) => setSetting("changeThreshold", Number(event.target.value))}
            className="v0-range"
          />
        </div>

        <div className="space-y-2">
          <div className="flex items-center justify-between text-sm text-foreground">
            <FieldLabel htmlFor="semantic-threshold">{t("settings.semantic_threshold")}</FieldLabel>
            <span>{state.settings.semanticThreshold.toFixed(2)}</span>
          </div>
          <input
            id="semantic-threshold"
            type="range"
            min={0}
            max={1}
            step={0.01}
            value={state.settings.semanticThreshold}
            onChange={(event) => setSetting("semanticThreshold", Number(event.target.value))}
            className="v0-range"
          />
        </div>

        <div className="grid grid-cols-2 gap-3">
          <div className="space-y-2">
            <FieldLabel htmlFor="merge-gap">{t("settings.merge_close_gap")}</FieldLabel>
            <Input
              id="merge-gap"
              type="number"
              min={0}
              step={1}
              value={state.settings.mergeCloseGapM}
              onChange={(event) => setSetting("mergeCloseGapM", Number(event.target.value))}
              className="border-sidebar-border bg-card text-card-foreground shadow-none"
            />
          </div>
          <div className="space-y-2">
            <FieldLabel htmlFor="block-gap">{t("settings.building_block_gap")}</FieldLabel>
            <Input
              id="block-gap"
              type="number"
              min={0}
              step={1}
              value={state.settings.buildingBlockGapM}
              onChange={(event) => setSetting("buildingBlockGapM", Number(event.target.value))}
              className="border-sidebar-border bg-card text-card-foreground shadow-none"
            />
          </div>
        </div>

        <div className="space-y-2">
          <FieldLabel htmlFor="buffer-distances">{t("settings.buffer_distances")}</FieldLabel>
          <Input
            id="buffer-distances"
            value={state.settings.bufferDistancesText}
            onChange={(event) => setSetting("bufferDistancesText", event.target.value)}
            className="border-sidebar-border bg-card text-card-foreground shadow-none"
          />
        </div>
      </div>

      <div className="space-y-4 rounded border border-sidebar-border bg-sidebar px-4 py-4">
        <div className="space-y-2">
          <FieldLabel htmlFor="model-backend">{t("settings.model_backend")}</FieldLabel>
          <Select
            id="model-backend"
            value={state.settings.modelBackend}
            onChange={(event) => setSetting("modelBackend", event.target.value as "bandon_mps" | "sam3")}
            className="border-sidebar-border bg-card text-card-foreground shadow-none"
          >
            <option value="bandon_mps">
              {availabilityByMode.get("bandon_mps")?.available === false ? t("settings.bandon_mps_unavailable") : t("settings.bandon_mps")}
            </option>
            <option value="sam3">{t("settings.sam3_compatibility")}</option>
          </Select>
        </div>

        {runtimeConfig.supportsRequestBackendSelection && state.settings.modelBackend === "sam3" ? (
          <div className="space-y-2">
            <FieldLabel htmlFor="sam3-mode">{t("settings.sam3_mode")}</FieldLabel>
            <Select
              id="sam3-mode"
              value={state.settings.sam3BackendMode}
              onChange={(event) =>
                setSetting("sam3BackendMode", event.target.value as "public_zerogpu" | "local" | "huggingface_gpu")
              }
              className="border-sidebar-border bg-card text-card-foreground shadow-none"
            >
              <option value="public_zerogpu">{t("settings.public_zerogpu")}</option>
              <option value="local">{t("settings.local")}</option>
              <option value="huggingface_gpu">{t("settings.huggingface_gpu")}</option>
            </Select>
          </div>
        ) : null}

        <div className="rounded border border-sidebar-border bg-sidebar px-3 py-3 text-sm text-foreground">
          <p className="font-medium text-foreground">
            {state.settings.modelBackend === "bandon_mps" ? t("settings.bandon_mps") : t("settings.sam3_compatibility")}
          </p>
          <p className="mt-1 leading-6 text-muted-foreground">
            {state.settings.modelBackend === "bandon_mps" ? t("settings.bandon_primary") : t("settings.sam3_remote")}
          </p>
        </div>

        {backendAvailabilityLoading ? <p className="text-sm text-muted-foreground">{t("status.checking_backend")}</p> : null}

        {backendAvailabilityError ? (
          <div className="rounded border border-destructive/30 bg-destructive/10 px-3 py-3 text-sm text-destructive-foreground">
            Backend probe failed: {backendAvailabilityError}
          </div>
        ) : null}

        {selectedBackendBlocked ? (
          <div className="rounded border border-destructive/30 bg-destructive/10 px-3 py-3 text-sm text-destructive-foreground">
            {selectedBackendReason}
          </div>
        ) : null}
      </div>
    </WorkflowSectionCard>
  );
}
