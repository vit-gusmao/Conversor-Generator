import type { ConversionStatus, StageKey, QualityState } from '@pdf2x/contracts';

/**
 * Runtime mirrors of the contract constants.
 *
 * They are declared here (instead of importing the package at runtime) because
 * `@pdf2x/contracts` ships TypeScript sources, which cannot be `require`d from a
 * CommonJS build. Type safety is preserved by the `satisfies` clauses below:
 * if the contract changes, this file stops compiling.
 */
export const CONVERSION_STATUSES = [
  'received',
  'validating',
  'queued',
  'inspecting',
  'extracting',
  'structuring',
  'normalizing',
  'reconciling',
  'generating',
  'auditing',
  'completed',
  'completed_with_warnings',
  'failed',
  'cancelled',
] as const satisfies readonly ConversionStatus[];

export const TERMINAL_STATUSES = [
  'completed',
  'completed_with_warnings',
  'failed',
  'cancelled',
] as const satisfies readonly ConversionStatus[];

export const QUALITY_STATES = [
  'validated',
  'partial',
  'failed',
  'pending',
] as const satisfies readonly QualityState[];

export const STAGE_KEYS = [
  'receive',
  'inspect',
  'extract',
  'structure',
  'normalize',
  'reconcile',
  'generate',
  'audit',
] as const satisfies readonly StageKey[];

export type ConversionStage = StageKey | 'done' | 'failed';

export function isTerminal(status: ConversionStatus): boolean {
  return (TERMINAL_STATUSES as readonly string[]).includes(status);
}

export function isActive(status: ConversionStatus): boolean {
  return !isTerminal(status);
}

export function asConversionStatus(value: unknown): ConversionStatus | null {
  if (typeof value !== 'string') return null;
  return (CONVERSION_STATUSES as readonly string[]).includes(value)
    ? (value as ConversionStatus)
    : null;
}

export function asQualityState(value: unknown): QualityState | null {
  if (typeof value !== 'string') return null;
  return (QUALITY_STATES as readonly string[]).includes(value)
    ? (value as QualityState)
    : null;
}

const STAGE_BY_STATUS: Record<ConversionStatus, ConversionStage> = {
  received: 'receive',
  validating: 'receive',
  queued: 'inspect',
  inspecting: 'inspect',
  extracting: 'extract',
  structuring: 'structure',
  normalizing: 'normalize',
  reconciling: 'reconcile',
  generating: 'generate',
  auditing: 'audit',
  completed: 'done',
  completed_with_warnings: 'done',
  failed: 'failed',
  cancelled: 'failed',
};

export function stageKeyForStatus(status: ConversionStatus): ConversionStage {
  return STAGE_BY_STATUS[status];
}

/**
 * Lower bound of the overall progress for each status, derived from the
 * contract's stage weights. Used when the engine reports no number yet so the
 * UI never jumps backwards when a status advances.
 */
const PROGRESS_FLOOR: Record<ConversionStatus, number> = {
  received: 0,
  validating: 0.02,
  queued: 0.05,
  inspecting: 0.08,
  extracting: 0.15,
  structuring: 0.4,
  normalizing: 0.6,
  reconciling: 0.7,
  generating: 0.75,
  auditing: 0.9,
  completed: 1,
  completed_with_warnings: 1,
  failed: 1,
  cancelled: 1,
};

export function progressFloorForStatus(status: ConversionStatus): number {
  return PROGRESS_FLOOR[status];
}

export function clampProgress(value: number): number {
  if (!Number.isFinite(value)) return 0;
  if (value < 0) return 0;
  if (value > 1) return 1;
  return Math.round(value * 10000) / 10000;
}
