import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { LocalModelsPage } from '@/pages/local-models-page';

vi.mock('@/hooks/use-session', () => ({
  useEnsureSession: () => ({ ready: true }),
}));

type Bridge = Record<string, ReturnType<typeof vi.fn>>;

function setBridge(bridge: Bridge | undefined) {
  (window as unknown as { desktop?: Bridge }).desktop = bridge;
}

function makeBridge(overrides: Bridge = {}): Bridge {
  return {
    readRuntimeProfile: vi.fn().mockResolvedValue({
      version: 3,
      mode: 'external',
      ocr_enabled: true,
      ocr_model: 'glm-ocr:latest',
      assistant_enabled: false,
      assistant_model: 'qwen3.5:4b',
      models_dir_configured: true,
    }),
    testModelProfile: vi.fn().mockResolvedValue({
      runtime_detected: true,
      runtime_version: '0.5.1',
      ocr_model_available: true,
      assistant_model_available: false,
      installed_models: ['glm-ocr:latest'],
      notes: [],
    }),
    applyRuntimeProfile: vi.fn().mockResolvedValue({ status: 'applied', applied_version: 4, blockers: [] }),
    pickModelResource: vi.fn().mockResolvedValue(null),
    getRuntimeStatus: vi.fn().mockResolvedValue({}),
    pickSource: vi.fn().mockResolvedValue(null),
    ...overrides,
  };
}

function renderPage() {
  return render(
    <MemoryRouter>
      <LocalModelsPage />
    </MemoryRouter>,
  );
}

describe('LocalModelsPage', () => {
  beforeEach(() => {
    setBridge(undefined);
  });

  afterEach(() => {
    setBridge(undefined);
  });

  it('explains that model management is desktop-only when there is no bridge', () => {
    renderPage();
    expect(screen.getByText('Disponível no aplicativo desktop')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Testar modelos/i })).not.toBeInTheDocument();
  });

  it('shows the diagnostics returned by the desktop bridge', async () => {
    const bridge = makeBridge();
    setBridge(bridge);
    renderPage();

    expect(await screen.findByText('perfil v3')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: /Testar modelos/i }));

    expect(await screen.findByText('Diagnóstico')).toBeInTheDocument();
    expect(screen.getByText('runtime detectado')).toBeInTheDocument();
    expect(screen.getByText('versão 0.5.1')).toBeInTheDocument();
    expect(screen.getByText('OCR instalado')).toBeInTheDocument();
    expect(screen.getByText('LLM ausente')).toBeInTheDocument();
    expect(bridge.testModelProfile).toHaveBeenCalledTimes(1);
  });

  it('applies only the paths the user picked and never sends untouched paths as null', async () => {
    const bridge = makeBridge({
      pickModelResource: vi.fn().mockResolvedValue({ kind: 'ollama_executable', path: 'C:/Ollama/ollama.exe' }),
    });
    setBridge(bridge);
    renderPage();
    await screen.findByText('perfil v3');

    // Saved models_dir stays untouched (only a presence flag is known).
    expect(screen.getByPlaceholderText('configurado (mantido)')).toBeInTheDocument();

    await userEvent.click(screen.getAllByRole('button', { name: /Procurar…/ })[0]!);
    await waitFor(() => expect(bridge.pickModelResource).toHaveBeenCalledWith({ kind: 'ollama_executable' }));

    await userEvent.click(screen.getByRole('button', { name: 'Aplicar perfil' }));
    await waitFor(() => expect(bridge.applyRuntimeProfile).toHaveBeenCalled());

    const [payload, expectedVersion] = bridge.applyRuntimeProfile.mock.calls[0]!;
    expect(payload.ollama_path).toBe('C:/Ollama/ollama.exe');
    expect(Object.prototype.hasOwnProperty.call(payload, 'models_dir')).toBe(false);
    expect(expectedVersion).toBe(3);
  });

  it('omits every path key when the user changed no path', async () => {
    const bridge = makeBridge();
    setBridge(bridge);
    renderPage();
    await screen.findByText('perfil v3');

    await userEvent.click(screen.getByRole('button', { name: 'Aplicar perfil' }));
    await waitFor(() => expect(bridge.applyRuntimeProfile).toHaveBeenCalled());

    const [payload] = bridge.applyRuntimeProfile.mock.calls[0]!;
    expect(Object.prototype.hasOwnProperty.call(payload, 'ollama_path')).toBe(false);
    expect(Object.prototype.hasOwnProperty.call(payload, 'models_dir')).toBe(false);
  });

  it('shows the saved Ollama executable as kept, not as missing', async () => {
    setBridge(
      makeBridge({
        readRuntimeProfile: vi.fn().mockResolvedValue({
          version: 3,
          mode: 'managed',
          ocr_enabled: true,
          ocr_model: 'glm-ocr:latest',
          assistant_enabled: false,
          assistant_model: 'qwen3.5:4b',
          ollama_path_configured: true,
          models_dir_configured: false,
        }),
      }),
    );
    renderPage();
    await screen.findByText('perfil v3');
    expect(screen.getByLabelText('Executável selecionado')).toHaveAttribute('placeholder', 'configurado (mantido)');
  });

  it('shows the blockers of a deferred apply and forces it only on request', async () => {
    const bridge = makeBridge({
      applyRuntimeProfile: vi
        .fn()
        .mockResolvedValueOnce({ status: 'deferred', applied_version: null, blockers: ['importações ativas: 2'] })
        .mockResolvedValueOnce({ status: 'applied', applied_version: 4, blockers: [] }),
    });
    setBridge(bridge);
    renderPage();
    await screen.findByText('perfil v3');

    await userEvent.click(screen.getByRole('button', { name: 'Aplicar perfil' }));
    expect(await screen.findByText('Aplicação adiada')).toBeInTheDocument();
    expect(screen.getByText('importações ativas: 2')).toBeInTheDocument();
    // The first call never forces the apply.
    expect(bridge.applyRuntimeProfile.mock.calls[0]![2]).toBeUndefined();

    await userEvent.click(screen.getByRole('button', { name: 'Aplicar mesmo assim' }));
    await waitFor(() => expect(bridge.applyRuntimeProfile).toHaveBeenCalledTimes(2));
    expect(bridge.applyRuntimeProfile.mock.calls[1]![2]).toEqual({ force: true });
    await waitFor(() => expect(screen.queryByText('Aplicação adiada')).not.toBeInTheDocument());
  });

  it('offers the GGUF import only in managed mode and sends just the tag', async () => {
    const bridge = makeBridge({
      importGguf: vi.fn().mockResolvedValue({ tag: 'converter/gguf-import:latest', digest: 'sha256:abc', status: 'imported' }),
      onGgufProgress: vi.fn(() => () => undefined),
    });
    setBridge(bridge);
    renderPage();
    await screen.findByText('perfil v3');

    // External mode: explained, not offered.
    expect(screen.queryByRole('button', { name: 'Importar GGUF' })).not.toBeInTheDocument();
    expect(screen.getByText('Disponível somente no modo “Iniciar Ollama pelo aplicativo”.')).toBeInTheDocument();
  });

  it('imports a GGUF through the bridge in managed mode', async () => {
    const bridge = makeBridge({
      readRuntimeProfile: vi.fn().mockResolvedValue({
        version: 3,
        mode: 'managed',
        ocr_enabled: true,
        ocr_model: 'glm-ocr:latest',
        assistant_enabled: false,
        assistant_model: 'qwen3.5:4b',
        ollama_path_configured: true,
        models_dir_configured: false,
      }),
      importGguf: vi.fn().mockResolvedValue({ tag: 'converter/gguf-import:latest', digest: 'sha256:abc', status: 'imported' }),
      onGgufProgress: vi.fn(() => () => undefined),
    });
    setBridge(bridge);
    renderPage();
    await screen.findByText('perfil v3');

    await userEvent.click(screen.getByRole('button', { name: 'Importar GGUF' }));
    await waitFor(() => expect(bridge.importGguf).toHaveBeenCalledTimes(1));
    expect(bridge.importGguf).toHaveBeenCalledWith({ tag: 'converter/gguf-import:latest' });
  });
});
