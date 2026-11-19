/**
 * Automation (schedules/occurrences) contracts (upgrade plan, docs 02/05).
 * ============================================================================
 * Daily rules are local wall-clock times in an explicit IANA timezone;
 * interval rules are anchored integer second counts in UTC. Occurrences are
 * unique per (schedule, revision, scheduled instant). Schedules never accept
 * shell commands or arbitrary actions — they drive import/conversion only.
 */

import { z } from 'zod';
import { ContractError } from './contract-errors';

export const MISFIRE_POLICIES = ['run_once_latest', 'skip'] as const;
export type MisfirePolicy = (typeof MISFIRE_POLICIES)[number];

export const OVERLAP_POLICIES = ['skip', 'queue_one'] as const;
export type OverlapPolicy = (typeof OVERLAP_POLICIES)[number];

export const SCHEDULE_RULE_KINDS = ['daily', 'interval'] as const;
export type ScheduleRuleKind = (typeof SCHEDULE_RULE_KINDS)[number];

/** Proposed initial bounds (doc 02 §6): 60 s to 365 days, integers only. */
export const INTERVAL_MIN_SECONDS = 60;
export const INTERVAL_MAX_SECONDS = 31_536_000;

export const RUN_STATES = [
  'queued',
  'running',
  'waiting_children',
  'cancelling',
  'succeeded',
  'partial',
  'no_changes',
  'failed',
  'cancelled',
  'skipped',
] as const;
export type RunState = (typeof RUN_STATES)[number];

export const RUN_TERMINAL_STATES: readonly RunState[] = [
  'succeeded',
  'partial',
  'no_changes',
  'failed',
  'cancelled',
  'skipped',
];

export function isTerminalRunState(state: RunState): boolean {
  return RUN_TERMINAL_STATES.includes(state);
}

export const RUN_PHASES = ['scan', 'snapshot', 'submit', 'convert', 'index', 'finalize'] as const;
export type RunPhase = (typeof RUN_PHASES)[number];

export const RUN_TRIGGERS = ['schedule', 'manual', 'recovery'] as const;
export type RunTrigger = (typeof RUN_TRIGGERS)[number];

export interface DailyRule {
  kind: 'daily';
  /** Local wall-clock time `HH:mm` (24h) in the schedule timezone. */
  local_time: string;
}

export interface IntervalRule {
  kind: 'interval';
  every_seconds: number;
  /** UTC anchor; the calendar derives `anchor + n * interval`. */
  anchor_at: string | null;
}

export type ScheduleRule = DailyRule | IntervalRule;

export interface ScheduleCreateRequest {
  name: string;
  source_id: string;
  rule: ScheduleRule;
  timezone: string;
  misfire_policy: MisfirePolicy;
  overlap_policy: OverlapPolicy;
  options: Record<string, unknown> | null;
  index_collection_id: string | null;
}

export interface ScheduleRecord extends ScheduleCreateRequest {
  id: string;
  owner_id: string;
  enabled: boolean;
  archived: boolean;
  /** Optimistic-concurrency revision; edits increment it. */
  revision: number;
  next_run_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface SchedulePreviewResponse {
  next_occurrences: string[];
  timezone: string;
  rule: ScheduleRule;
}

export interface AutomationRunRecord {
  id: string;
  owner_id: string;
  schedule_id: string;
  schedule_revision: number;
  scheduled_for: string;
  trigger: RunTrigger;
  occurrence_key: string;
  state: RunState;
  phase: RunPhase | null;
  import_id: string | null;
  index_job_id: string | null;
  attempt: number;
  /** Fencing token; recovery bumps it so stale workers cannot finalize. */
  fence?: number;
  error: { code: string; message: string } | null;
  created_at: string;
  updated_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface AutomationEvent {
  run_id: string;
  sequence: number;
  type: string;
  payload: Record<string, unknown>;
  created_at: string;
}

const localTimeSchema = z
  .string()
  .regex(/^([01]\d|2[0-3]):([0-5]\d)$/, 'local_time deve ser HH:mm (00:00–23:59).');

export const scheduleRuleSchema = z.discriminatedUnion('kind', [
  z.object({ kind: z.literal('daily'), local_time: localTimeSchema }).strict(),
  z
    .object({
      kind: z.literal('interval'),
      every_seconds: z.number(),
      anchor_at: z.string().nullable().optional(),
    })
    .strict(),
]);

export const scheduleCreateSchema = z
  .object({
    name: z.string().min(1).max(200),
    source_id: z.string().min(1).max(128),
    rule: scheduleRuleSchema,
    timezone: z.string().min(1).max(100),
    misfire_policy: z.enum(MISFIRE_POLICIES).optional().default('run_once_latest'),
    overlap_policy: z.enum(OVERLAP_POLICIES).optional().default('skip'),
    options: z.record(z.string(), z.unknown()).nullable().optional().default(null),
    index_collection_id: z.string().nullable().optional().default(null),
  })
  .strict();

/**
 * Validates the numeric bounds of an interval rule. Zod accepts any number,
 * so the plan bounds (integers, 60 s…365 d) are enforced here where both the
 * Python mirror and the fixture expectations align.
 */
function validateRuleBounds(rule: ScheduleRule): void {
  if (rule.kind !== 'interval') return;
  const seconds = rule.every_seconds;
  if (!Number.isSafeInteger(seconds) || typeof seconds !== 'number') {
    throw new ContractError('INVALID_SCHEDULE_RULE', 'every_seconds deve ser um número inteiro de segundos.', {
      every_seconds: seconds,
    });
  }
  if (seconds < INTERVAL_MIN_SECONDS || seconds > INTERVAL_MAX_SECONDS) {
    throw new ContractError(
      'INVALID_SCHEDULE_RULE',
      `every_seconds deve estar entre ${INTERVAL_MIN_SECONDS} e ${INTERVAL_MAX_SECONDS} (60 segundos a 365 dias).`,
      { every_seconds: seconds, min: INTERVAL_MIN_SECONDS, max: INTERVAL_MAX_SECONDS },
    );
  }
}

/**
 * Parses `POST /schedules` bodies with the same rejections as
 * `automation/types.py::validate_schedule_create`.
 *
 * @throws ContractError with `UNKNOWN_FIELD`, `INVALID_SCHEDULE_RULE` or
 *         `INVALID_TIMEZONE`.
 */
export function parseScheduleCreate(input: unknown): ScheduleCreateRequest {
  if (typeof input !== 'object' || input === null || Array.isArray(input)) {
    throw new ContractError('INVALID_PAYLOAD', 'O corpo deve ser um objeto JSON.');
  }
  const parsed = scheduleCreateSchema.safeParse(input);
  if (!parsed.success) {
    const extra = parsed.error.issues.filter((issue) => issue.code === 'unrecognized_keys');
    if (extra.length > 0) {
      const fields = (extra[0] as { keys?: string[] }).keys ?? [];
      throw new ContractError(
        'UNKNOWN_FIELD',
        'Campo desconhecido; agendamentos não executam comandos arbitrários.',
        { fields },
      );
    }
    throw new ContractError('INVALID_SCHEDULE_RULE', 'Regra de agendamento inválida.', {
      issues: parsed.error.issues.map((issue) => ({
        path: issue.path.join('.'),
        message: issue.message,
      })),
    });
  }
  const data = parsed.data;
  const rule = data.rule as ScheduleRule;
  validateRuleBounds(rule);
  try {
    // Timezone names are validated with Intl-backed data where available;
    // the canonical check happens server-side via `zoneinfo` + `tzdata`.
    const offset = new Intl.DateTimeFormat('en-US', { timeZone: data.timezone }).format(new Date());
    if (!offset) throw new RangeError();
  } catch {
    throw new ContractError('INVALID_TIMEZONE', `Fuso horário desconhecido: ${data.timezone}.`, {
      timezone: data.timezone,
    });
  }
  return {
    name: data.name,
    source_id: data.source_id,
    rule,
    timezone: data.timezone,
    misfire_policy: data.misfire_policy,
    overlap_policy: data.overlap_policy,
    options: data.options,
    index_collection_id: data.index_collection_id,
  };
}
