/** Portuguese labels shared by the schedule list, detail, form and run pages. */

import type { RunPhase, RunState, RunTrigger, ScheduleRecord } from '@pdf2x/contracts';
import { isTerminalRunState } from '@pdf2x/contracts';
import type { BadgeProps } from '@/components/ui/badge';

export const RUN_STATE_LABELS: Record<RunState, string> = {
  queued: 'Na fila',
  running: 'Em execução',
  waiting_children: 'Aguardando conversões',
  cancelling: 'Cancelando',
  succeeded: 'Concluída',
  partial: 'Parcial',
  no_changes: 'Sem alterações',
  failed: 'Falhou',
  cancelled: 'Cancelada',
  skipped: 'Ignorada',
};

export const RUN_STATE_VARIANTS: Record<RunState, NonNullable<BadgeProps['variant']>> = {
  queued: 'muted',
  running: 'info',
  waiting_children: 'info',
  cancelling: 'warning',
  succeeded: 'success',
  partial: 'warning',
  no_changes: 'muted',
  failed: 'danger',
  cancelled: 'muted',
  skipped: 'muted',
};

export const RUN_PHASE_LABELS: Record<RunPhase, string> = {
  scan: 'varredura',
  snapshot: 'captura',
  submit: 'envio',
  convert: 'conversão',
  index: 'indexação',
  finalize: 'finalização',
};

export const RUN_TRIGGER_LABELS: Record<RunTrigger, string> = {
  schedule: 'agendada',
  manual: 'manual',
  recovery: 'recuperação',
};

export const MISFIRE_LABELS: Record<ScheduleRecord['misfire_policy'], string> = {
  run_once_latest: 'Executar a última ocorrência perdida',
  skip: 'Ignorar e seguir',
};

export const OVERLAP_LABELS: Record<ScheduleRecord['overlap_policy'], string> = {
  skip: 'Ignorar se houver execução ativa',
  queue_one: 'Enfileirar no máximo uma',
};

/** Human labels for the automation event types the backend emits. */
export const RUN_EVENT_LABELS: Record<string, string> = {
  OCCURRENCE_DUE: 'Ocorrência prevista',
  RUN_CLAIMED: 'Execução iniciada',
  LEASE_RECOVERED: 'Execução recuperada',
  RUN_CANCEL_REQUESTED: 'Cancelamento solicitado',
  IMPORT_STARTED: 'Importação iniciada',
  IMPORT_FAILED: 'Importação falhou',
  SOURCE_SCAN_COMPLETED: 'Varredura concluída',
  SNAPSHOT_INCOMPLETE: 'Captura incompleta',
  CONVERSIONS_COMPLETED: 'Conversões concluídas',
  INDEX_COMPLETED: 'Indexação concluída',
  RUN_FINISHED: 'Execução finalizada',
  MISFIRE_COALESCED: 'Horários perdidos agrupados',
  NEXT_RUN_CHANGED: 'Próxima execução recalculada',
  REVISION_CHANGED: 'Agenda alterada',
  EXECUTOR_ERROR: 'Erro no executor',
  EXECUTOR_INTERNAL_ERROR: 'Erro interno do executor',
};

export function eventLabel(type: string): string {
  return RUN_EVENT_LABELS[type] ?? type;
}

export function isRunTerminal(state: RunState | null | undefined): boolean {
  return state ? isTerminalRunState(state) : false;
}

export function runStateLabel(state: RunState | string): string {
  return RUN_STATE_LABELS[state as RunState] ?? state;
}

export function runStateVariant(state: RunState | string): NonNullable<BadgeProps['variant']> {
  return RUN_STATE_VARIANTS[state as RunState] ?? 'muted';
}

/** Human rule text, e.g. "todo dia às 08:00" or "a cada 1 h". */
export function ruleLabel(schedule: Pick<ScheduleRecord, 'rule'>): string {
  const rule = schedule.rule;
  if (rule.kind === 'daily') return `todo dia às ${rule.local_time}`;
  const seconds = Number(rule.every_seconds ?? 0);
  if (seconds % 86_400 === 0) return `a cada ${seconds / 86_400} dia(s)`;
  if (seconds % 3600 === 0) return `a cada ${seconds / 3600} h`;
  if (seconds % 60 === 0) return `a cada ${seconds / 60} min`;
  return `a cada ${seconds} s`;
}

/**
 * Strips server-owned fields so a stored schedule can be sent back to
 * `POST /schedules/preview` (same rule, same timezone).
 */
export function toScheduleCreateRequest(schedule: ScheduleRecord) {
  return {
    name: schedule.name,
    source_id: schedule.source_id,
    rule: schedule.rule,
    timezone: schedule.timezone,
    misfire_policy: schedule.misfire_policy,
    overlap_policy: schedule.overlap_policy,
    options: schedule.options,
    index_collection_id: schedule.index_collection_id,
  };
}
