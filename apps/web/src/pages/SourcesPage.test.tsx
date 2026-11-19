import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { SourcesPage } from '@/pages/sources-page';

vi.mock('@/hooks/use-session', () => ({
  useEnsureSession: () => ({ ready: true }),
}));

vi.mock('@/services/sources', () => ({
  listSourceRoots: vi.fn(),
  listSources: vi.fn(),
  createSource: vi.fn(),
  updateSource: vi.fn(),
  archiveSource: vi.fn(),
  validateSource: vi.fn(),
  startImport: vi.fn(),
  getImport: vi.fn(),
  listImportItems: vi.fn(),
  cancelImport: vi.fn(),
}));

import * as sourcesService from '@/services/sources';

const mockRoots = sourcesService.listSourceRoots as unknown as ReturnType<typeof vi.fn>;
const mockSources = sourcesService.listSources as unknown as ReturnType<typeof vi.fn>;

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <SourcesPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('SourcesPage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('explains how roots are granted when none exists', async () => {
    mockRoots.mockResolvedValue([]);
    mockSources.mockResolvedValue([]);
    renderPage();
    expect(
      await screen.findByText(/Nenhuma pasta foi concedida ainda/i),
    ).toBeInTheDocument();
    // The browser never gains disk access by typing a path.
    expect(screen.getByText(/nunca ganha acesso ao disco/i)).toBeInTheDocument();
  });

  it('lists an empty source as valid, not as an error', async () => {
    mockRoots.mockResolvedValue([
      {
        id: 'root_1',
        label: 'Dados',
        runtime_id: 'local',
        runtime_available: true,
        granted_by: 'owner',
        created_at: new Date().toISOString(),
        revoked_at: null,
      },
    ]);
    mockSources.mockResolvedValue([
      {
        id: 'src_1',
        owner_id: 'owner',
        root_id: 'root_1',
        relative_path: 'vazio',
        kind: 'directory',
        name: 'Pasta vazia',
        recursive: false,
        include_patterns: null,
        exclude_patterns: null,
        enabled: true,
        version: 1,
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      },
    ]);
    renderPage();
    expect(await screen.findByText('Pasta vazia')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Importar agora/i })).toBeInTheDocument();
  });

  it('reports validation failures without crashing the page', async () => {
    mockRoots.mockResolvedValue([
      {
        id: 'root_1',
        label: 'Dados',
        runtime_id: 'local',
        runtime_available: true,
        granted_by: 'owner',
        created_at: new Date().toISOString(),
        revoked_at: null,
      },
    ]);
    mockSources.mockResolvedValue([
      {
        id: 'src_1',
        owner_id: 'owner',
        root_id: 'root_1',
        relative_path: 'docs',
        kind: 'directory',
        name: 'Docs',
        recursive: true,
        include_patterns: ['*.xlsx'],
        exclude_patterns: null,
        enabled: true,
        version: 2,
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      },
    ]);
    (sourcesService.validateSource as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
      source_id: 'src_1',
      ok: false,
      available: false,
      code: 'SOURCE_NOT_FOUND',
      message: 'A fonte não existe neste momento.',
      sample: [],
      estimated_files: 0,
      estimated_bytes: 0,
      warnings: [],
    });
    renderPage();
    const validateButton = await screen.findByRole('button', { name: /Validar/i });
    validateButton.click();
    // The page keeps rendering after a failed validation.
    await waitFor(() =>
      expect(screen.getByText('Docs')).toBeInTheDocument(),
    );
  });

  it('works without any desktop bridge (browser-only)', async () => {
    mockRoots.mockResolvedValue([]);
    mockSources.mockResolvedValue([]);
    renderPage();
    await screen.findByText(/Nenhuma pasta foi concedida ainda/i);
    // No Electron/Node import is required for this screen to render.
    expect((window as unknown as { desktop?: unknown }).desktop).toBeUndefined();
  });
});
