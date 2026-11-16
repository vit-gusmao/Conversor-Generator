import type { AuditEvent, AuditIssue, AuditSummary, CellEvidence, PageAudit } from '@pdf2x/contracts';
import { apiBlob, apiFetch } from '@/lib/api';

export interface AuditEventFilters {
  stage?: string | null;
  event_type?: string | null;
  severity?: string | null;
  page?: number | null;
  since_sequence?: number | null;
  limit?: number;
}

export interface AuditEventsPage {
  items: AuditEvent[];
  total: number;
  next_sequence: number | null;
}

export interface AuditIssuesPage {
  items: AuditIssue[];
  total: number;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null;
}

/** Endpoints may return either a bare array or the documented page envelope. */
export function normalizeItems<T>(payload: unknown): { items: T[]; total: number } {
  if (Array.isArray(payload)) return { items: payload as T[], total: (payload as T[]).length };
  if (isRecord(payload)) {
    const items = payload.items;
    if (Array.isArray(items)) {
      const total = typeof payload.total === 'number' ? payload.total : items.length;
      return { items: items as T[], total };
    }
    if ('event' in payload || 'issue_id' in payload) {
      return { items: [payload as T], total: 1 };
    }
  }
  return { items: [], total: 0 };
}

export async function getAuditSummary(id: string): Promise<AuditSummary> {
  return apiFetch<AuditSummary>(`/conversions/${encodeURIComponent(id)}/audit`);
}

export async function getAuditEvents(id: string, filters: AuditEventFilters = {}): Promise<AuditEventsPage> {
  const payload = await apiFetch<unknown>(`/conversions/${encodeURIComponent(id)}/audit/events`, {
    query: {
      stage: filters.stage ?? undefined,
      event_type: filters.event_type ?? undefined,
      severity: filters.severity ?? undefined,
      page: filters.page ?? undefined,
      since_sequence: filters.since_sequence ?? undefined,
      limit: filters.limit ?? 50,
    },
  });
  const { items, total } = normalizeItems<AuditEvent>(payload);
  const maxSequence = items.reduce((max, event) => Math.max(max, event.sequence ?? 0), 0);
  const payloadNext = isRecord(payload) && typeof payload.next_sequence === 'number' ? payload.next_sequence : null;
  const next = payloadNext ?? (items.length > 0 ? maxSequence : null);
  return { items, total, next_sequence: next };
}

export async function getPageAudit(id: string, page: number): Promise<PageAudit> {
  return apiFetch<PageAudit>(`/conversions/${encodeURIComponent(id)}/audit/pages/${page}`);
}

export async function getAuditIssues(id: string): Promise<AuditIssuesPage> {
  const payload = await apiFetch<unknown>(`/conversions/${encodeURIComponent(id)}/audit/issues`);
  const normalized = normalizeItems<AuditIssue>(payload);
  return { items: normalized.items, total: normalized.total };
}

export async function getCellEvidence(
  id: string,
  filters: { sheet?: string | null; cell?: string | null; limit?: number } = {},
): Promise<CellEvidence[]> {
  const payload = await apiFetch<unknown>(`/conversions/${encodeURIComponent(id)}/audit/cells`, {
    query: {
      sheet: filters.sheet ?? undefined,
      cell: filters.cell ?? undefined,
      limit: filters.limit ?? 100,
    },
  });
  return normalizeItems<CellEvidence>(payload).items;
}

export async function exportAuditFile(id: string, format: 'json' | 'txt'): Promise<Blob> {
  return apiBlob(`/conversions/${encodeURIComponent(id)}/audit/export`, { query: { format } });
}

/* ------------------------------------------------------------------------- */
/* Client-side TXT report (the endpoint can also render it server-side)       */
/* ------------------------------------------------------------------------- */

const SEVERITY_TEXT: Record<string, string> = {
  info: 'INFO',
  warning: 'AVISO',
  error: 'ERRO',
  critical: 'CRÍTICO',
};

function line(char = '-', length = 78): string {
  return char.repeat(length);
}

export function buildAuditTextReport(input: {
  summary: AuditSummary;
  issues: AuditIssue[];
  events?: AuditEvent[];
}): string {
  const { summary, issues, events = [] } = input;
  const m = summary.metrics;
  const out: string[] = [];

  out.push(line('='));
  out.push('RELATÓRIO DE AUDITORIA — PDF TO EXCEL');
  out.push(line('='));
  out.push(`Conversão:        ${summary.conversion_id}`);
  out.push(`Execução:         ${summary.run_id}`);
  out.push(`Status:           ${summary.status}`);
  out.push(`Qualidade:        ${summary.quality}`);
  out.push(`Gerado em:        ${summary.generated_at}`);
  out.push(`Cadeia de hashes: ${summary.hash_chain_valid ? 'VÁLIDA' : 'INVÁLIDA'}`);
  out.push('');

  out.push('MÉTRICAS');
  out.push(line());
  out.push(`Páginas analisadas:      ${m.pages_analyzed}/${m.pages_total}`);
  out.push(`Blocos de texto:         ${m.blocks_total}`);
  out.push(`Blocos com destino:      ${m.blocks_assigned} (${m.assignment_percent.toFixed(1)}%)`);
  out.push(`Blocos preservados:      ${m.blocks_preserved}`);
  out.push(`Blocos não classificados:${m.blocks_unclassified}`);
  out.push(`Blocos descartados:      ${m.blocks_dropped}`);
  out.push(`Células geradas:         ${m.cells_generated}`);
  out.push(`Tabelas detectadas:      ${m.tables_detected}`);
  out.push(`Campos detectados:       ${m.fields_detected}`);
  out.push(`Registros detectados:    ${m.records_detected}`);
  out.push(`Regiões de OCR:          ${m.ocr_regions}`);
  out.push(`Eventos auditados:       ${m.events_total}`);
  out.push(`Inconsistências:         ${m.issues_total} (${m.warnings} avisos, ${m.errors} erros)`);
  out.push(`Cobertura textual:       ${m.coverage_percent.toFixed(1)}%`);
  out.push('');

  out.push('MOTORES');
  out.push(line());
  if (summary.engines.length === 0) {
    out.push('(nenhum motor registrado)');
  } else {
    for (const engine of summary.engines) {
      out.push(
        `- ${engine.engine} [${engine.kind}] regiões=${engine.regions} páginas=${engine.pages} aceitos=${engine.accepted} rejeitados=${engine.rejected}`,
      );
    }
  }
  out.push('');

  out.push('ETAPAS E DURAÇÕES');
  out.push(line());
  for (const timing of summary.timings) {
    out.push(`- ${timing.stage}: ${timing.duration_ms} ms (${timing.pages} páginas)`);
  }
  out.push('');

  out.push('INCONSISTÊNCIAS');
  out.push(line());
  if (issues.length === 0) {
    out.push('(nenhuma inconsistência registrada)');
  } else {
    for (const issue of issues) {
      const page = issue.page_number ? `pág. ${issue.page_number}` : 'sem página';
      out.push(`[${SEVERITY_TEXT[issue.severity] ?? issue.severity}] ${issue.code} (${page})`);
      out.push(`  ${issue.message}`);
    }
  }
  out.push('');

  if (events.length > 0) {
    out.push('EVENTOS (amostra)');
    out.push(line());
    for (const event of events) {
      out.push(
        `#${event.sequence} ${event.created_at} [${event.stage}/${event.event_type}] ${event.message} hash=${event.event_hash.slice(0, 12)}…`,
      );
    }
    out.push('');
  }

  out.push(line('='));
  out.push('Fim do relatório.');
  return out.join('\n');
}
