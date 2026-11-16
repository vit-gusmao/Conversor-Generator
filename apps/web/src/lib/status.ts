import type { ConversionStatus, StageKey, Conversion } from '@pdf2x/contracts';
import { PIPELINE_STAGES, isTerminal, stageIndexForStatus } from '@pdf2x/contracts';

export type StepState = 'done' | 'current' | 'pending' | 'failed';

export interface StageRange {
  /** Overall progress (0..1) when the stage starts. */
  start: number;
  /** Overall progress (0..1) when the stage ends. */
  end: number;
  weight: number;
}

export const TOTAL_WEIGHT: number = PIPELINE_STAGES.reduce((sum, stage) => sum + stage.weight, 0);

export function clamp01(value: number): number {
  if (!Number.isFinite(value)) return 0;
  return Math.min(1, Math.max(0, value));
}

export function formatPercent(value: number, digits = 0): string {
  return `${(clamp01(value) * 100).toFixed(digits)}%`;
}

/** Weight range of a stage inside the whole pipeline. */
export function stageRange(index: number): StageRange {
  const bounded = Math.min(Math.max(0, index), PIPELINE_STAGES.length - 1);
  let start = 0;
  for (let i = 0; i < bounded; i += 1) {
    start += PIPELINE_STAGES[i]?.weight ?? 0;
  }
  const weight = PIPELINE_STAGES[bounded]?.weight ?? 0;
  return {
    start: start / TOTAL_WEIGHT,
    end: (start + weight) / TOTAL_WEIGHT,
    weight: weight / TOTAL_WEIGHT,
  };
}

/** How far we are inside a given stage, derived from overall progress. */
export function fractionWithinStage(index: number, overall: number): number {
  const { start, end } = stageRange(index);
  if (end <= start) return 0;
  return clamp01((clamp01(overall) - start) / (end - start));
}

/**
 * Overall progress derived from a status plus the progress reported by the API.
 * The stage weights give a floor, so a conversion that already reached
 * `extracting` never renders below 15% even when the API reports 0.
 */
export function progressForStatus(status: ConversionStatus, reported = 0): number {
  if (status === 'completed' || status === 'completed_with_warnings') return 1;
  if (status === 'failed' || status === 'cancelled') return clamp01(reported);
  const index = stageIndexForStatus(status);
  const floor = stageRange(index).start;
  return clamp01(Math.max(floor, clamp01(reported)));
}

export function overallProgress(input: {
  status: ConversionStatus;
  progress: number;
  progress_indeterminate?: boolean;
}): number {
  return progressForStatus(input.status, input.progress);
}

export function stageIndexFromKey(stage: StageKey | 'done' | 'failed' | string | null | undefined): number {
  if (!stage) return -1;
  if (stage === 'done') return PIPELINE_STAGES.length;
  return PIPELINE_STAGES.findIndex((item) => item.key === stage);
}

/**
 * State of one stepper step. `stage` is the stage reported by the conversion,
 * which for failures pinpoints exactly where the pipeline stopped.
 */
export function stepStateFor(
  index: number,
  status: ConversionStatus,
  stage?: StageKey | 'done' | 'failed' | string | null,
): StepState {
  if (status === 'completed' || status === 'completed_with_warnings') return 'done';

  if (status === 'failed' || status === 'cancelled') {
    const activeIndex = stageIndexFromKey(stage);
    const failedIndex = activeIndex >= 0 ? Math.min(activeIndex, PIPELINE_STAGES.length - 1) : 0;
    if (index === failedIndex) return 'failed';
    return index < failedIndex ? 'done' : 'pending';
  }

  const currentIndex = stageIndexForStatus(status);
  if (index < currentIndex) return 'done';
  if (index === currentIndex) return 'current';
  return 'pending';
}

export function currentStageLabel(status: ConversionStatus, stage?: string | null): string {
  if (status === 'completed') return 'Concluído';
  if (status === 'completed_with_warnings') return 'Concluído com avisos';
  if (status === 'failed') return 'Falhou';
  if (status === 'cancelled') return 'Cancelado';
  const index = stageIndexForStatus(status);
  return PIPELINE_STAGES[index]?.label ?? 'Preparando';
}

export function isSuccessful(status: ConversionStatus): boolean {
  return status === 'completed' || status === 'completed_with_warnings';
}

export function conversionIsTerminal(conversion: Pick<Conversion, 'status'> | null | undefined): boolean {
  return conversion ? isTerminal(conversion.status) : false;
}

/** Elapsed milliseconds between two ISO timestamps (clamped at zero). */
export function elapsedMs(startIso: string | null | undefined, endIso?: string | null): number {
  if (!startIso) return 0;
  const start = Date.parse(startIso);
  if (Number.isNaN(start)) return 0;
  const end = endIso ? Date.parse(endIso) : Date.now();
  return Math.max(0, (Number.isNaN(end) ? Date.now() : end) - start);
}
