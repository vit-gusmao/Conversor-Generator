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
  result: (id: string) => ['conversions', id, 'result'] as const,
  preview: (id: string, sheet: string | null, offset: number, limit: number) =>
    ['conversions', id, 'preview', { sheet, offset, limit }] as const,
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
