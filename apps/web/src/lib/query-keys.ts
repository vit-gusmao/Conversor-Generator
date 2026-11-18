export const STALE_TIME = {
  /** Live conversion status — SSE is the primary source, this is a safety net. */
  status: 2_000,
  history: 15_000,
  audit: 30_000,
  settings: 60_000,
  result: 15_000,
  preview: 60_000,
} as const;

export const queryKeys = {
  auth: {
    session: ['auth', 'session'] as const,
  },
  settings: ['settings'] as const,
  conversions: {
    all: ['conversions'] as const,
    list: (filters: object) => ['conversions', 'list', filters] as const,
    detail: (id: string) => ['conversions', 'detail', id] as const,
  },
  batches: {
    all: ['batches'] as const,
    detail: (id: string) => ['batches', 'detail', id] as const,
    items: (id: string, filters: object) => ['batches', id, 'items', filters] as const,
  },
  result: (id: string) => ['conversions', id, 'result'] as const,
  resultOverview: (id: string) => ['conversions', id, 'result-overview'] as const,
  sheets: (id: string, filters: object) => ['conversions', id, 'sheets', filters] as const,
  sheetPages: (id: string, sheetId: string, cursor: number | null) =>
    ['conversions', id, 'sheets', sheetId, 'pages', cursor] as const,
  // `runId` keeps a retried conversion from showing the previous run's rows.
  preview: (
    id: string,
    sheet: string | null,
    offset: number,
    limit: number,
    runId: string | null = null,
  ) => ['conversions', id, 'preview', { sheet, offset, limit, runId }] as const,
  audit: {
    all: (id: string) => ['audit', id] as const,
    summary: (id: string) => ['audit', id, 'summary'] as const,
    events: (id: string, filters: object) => ['audit', id, 'events', filters] as const,
    pages: (id: string) => ['audit', id, 'pages'] as const,
    page: (id: string, page: number) => ['audit', id, 'pages', page] as const,
    issues: (id: string) => ['audit', id, 'issues'] as const,
    cells: (id: string, filters: object) => ['audit', id, 'cells', filters] as const,
  },
} as const;
