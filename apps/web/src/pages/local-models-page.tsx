import { useEffect, useState } from 'react';
import { Bot, Cpu, ScanEye, ShieldCheck, Upload } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Badge } from '@/components/ui/badge';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { PageHeader } from '@/components/layout/page-header';
import { EmptyBlock } from '@/components/layout/states';
import { useEnsureSession } from '@/hooks/use-session';
import { desktopBridge, pickModelResource } from '@/lib/platform';
import { explainError } from '@/lib/errors';
import { toast } from '@/stores/ui';

interface Diagnostics {
  runtime_detected?: boolean;
  runtime_version?: string | null;
  ocr_model_available?: boolean;
  assistant_model_available?: boolean;
  ocr_latency_ms?: number | null;
  assistant_latency_ms?: number | null;
  ocr_error?: string | null;
  assistant_error?: string | null;
  installed_models?: string[];
  notes?: string[];
}

interface Draft {
  mode: 'external' | 'managed';
  ollama_path: string | null;
  models_dir: string | null;
  ocr: { enabled: boolean; model: string; endpoint_host: string; endpoint_port: number };
  assistant: {
    enabled: boolean;
    model: string;
    endpoint_host: string;
    endpoint_port: number;
    context_tokens: number;
    max_output_tokens: number;
  };
}

const DEFAULT_DRAFT: Draft = {
  mode: 'external',
  ollama_path: null,
  models_dir: null,
  ocr: { enabled: true, model: 'glm-ocr:latest', endpoint_host: '127.0.0.1', endpoint_port: 11434 },
  assistant: {
    enabled: false,
    model: 'qwen3.5:4b',
    endpoint_host: '127.0.0.1',
    endpoint_port: 11434,
    context_tokens: 8192,
    max_output_tokens: 1024,
  },
};

/**
 * Configurações → IA local (T13). No desktop aplica pelo perfil do host; no
 * navegador mostra apenas os valores efetivos do operador, sem editar o
 * modelo de todas as sessões.
 */
export function LocalModelsPage() {
  useEnsureSession();
  const bridge = desktopBridge();
  const [draft, setDraft] = useState<Draft>(DEFAULT_DRAFT);
  const [version, setVersion] = useState<number | null>(null);
  const [diagnostics, setDiagnostics] = useState<Diagnostics | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!bridge) return;
    void bridge
      .readRuntimeProfile()
      .then((profile) => {
        setVersion(Number(profile.version ?? 1));
        setDraft((current) => ({
          ...current,
          mode: (profile.mode as Draft['mode']) ?? current.mode,
          ocr: { ...current.ocr, enabled: Boolean(profile.ocr_enabled), model: String(profile.ocr_model ?? current.ocr.model) },
          assistant: {
            ...current.assistant,
            enabled: Boolean(profile.assistant_enabled),
            model: String(profile.assistant_model ?? current.assistant.model),
          },
          models_dir: profile.models_dir_configured ? current.models_dir : null,
        }));
      })
      .catch(() => undefined);
  }, [bridge]);

  if (!bridge) {
    return (
      <div className="space-y-6">
        <PageHeader
          title="IA local"
          description="Escolha os modelos locais de OCR e do assistente."
          backTo={{ to: '/settings', label: 'Configurações' }}
        />
        <EmptyBlock
          title="Disponível no aplicativo desktop"
          description="A seleção de caminhos e o gerenciamento dos processos locais exigem o aplicativo Windows. No site, o operador configura o runtime; esta página apenas informa."
        />
      </div>
    );
  }

  const test = async () => {
    setBusy(true);
    try {
      const result = (await bridge.testModelProfile(draft as unknown as Record<string, unknown>)) as Diagnostics;
      setDiagnostics(result);
      toast({
        title: result.runtime_detected ? 'Runtime detectado' : 'Runtime indisponível',
        description: result.ocr_error ?? result.assistant_error ?? result.notes?.[0] ?? undefined,
        variant: result.runtime_detected ? 'success' : 'warning',
      });
    } catch (error) {
      toast({ title: 'Falha no diagnóstico', description: explainError(error), variant: 'error' });
    } finally {
      setBusy(false);
    }
  };

  const apply = async () => {
    setBusy(true);
    try {
      const result = (await bridge.applyRuntimeProfile(
        draft as unknown as Record<string, unknown>,
        version,
      )) as { status?: string; applied_version?: number | null; blockers?: string[] };
      setVersion(result.applied_version ?? version);
      toast({
        title:
          result.status === 'applied'
            ? 'Perfil aplicado'
            : result.status === 'deferred'
              ? 'Aplicação adiada'
              : 'Perfil revertido',
        description: result.blockers?.length
          ? `Aguardando: ${result.blockers.join(', ')}`
          : 'O backend reiniciou com os modelos selecionados.',
        variant: result.status === 'applied' ? 'success' : 'warning',
      });
    } catch (error) {
      toast({ title: 'Falha ao aplicar', description: explainError(error), variant: 'error' });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-6">
      <PageHeader
        title="IA local"
        description="Configuração da máquina: executável, pasta de modelos, tags de OCR e LLM."
        backTo={{ to: '/settings', label: 'Configurações' }}
        actions={<Badge variant="muted">perfil v{version ?? '—'}</Badge>}
      />

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-base">
            <Cpu className="h-4 w-4" aria-hidden /> Runtime
          </CardTitle>
        </CardHeader>
        <CardContent className="grid gap-3 sm:grid-cols-2">
          <div className="space-y-1">
            <span className="text-sm font-medium">Modo</span>
            <Select
              value={draft.mode}
              onValueChange={(value) => setDraft({ ...draft, mode: value as Draft['mode'] })}
            >
              <SelectTrigger aria-label="Modo do Ollama">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="external">Usar Ollama já ativo</SelectItem>
                <SelectItem value="managed">Iniciar Ollama pelo aplicativo</SelectItem>
              </SelectContent>
            </Select>
            <p className="text-xs text-muted">
              No modo externo o aplicativo não reconfigura nem reinicia o serviço existente.
            </p>
          </div>
          <div className="space-y-1">
            <span className="text-sm font-medium">Executável (ollama.exe)</span>
            <div className="flex gap-2">
              <Input value={draft.ollama_path ?? ''} readOnly placeholder="não selecionado" />
              <Button
                variant="outline"
                onClick={async () => {
                  const selection = await pickModelResource('ollama_executable');
                  if (selection) setDraft({ ...draft, ollama_path: selection.path });
                }}
              >
                Procurar…
              </Button>
            </div>
          </div>
          <div className="space-y-1 sm:col-span-2">
            <span className="text-sm font-medium">Pasta de modelos do Ollama</span>
            <div className="flex gap-2">
              <Input value={draft.models_dir ?? ''} readOnly placeholder="padrão do Ollama" />
              <Button
                variant="outline"
                onClick={async () => {
                  const selection = await pickModelResource('models_directory');
                  if (selection) setDraft({ ...draft, models_dir: selection.path });
                }}
              >
                Procurar…
              </Button>
            </div>
            <p className="text-xs text-muted">
              A pasta é aplicada apenas ao processo iniciado pelo aplicativo (OLLAMA_MODELS).
            </p>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-base">
            <ScanEye className="h-4 w-4" aria-hidden /> GLM-OCR
          </CardTitle>
        </CardHeader>
        <CardContent className="grid gap-3 sm:grid-cols-2">
          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={draft.ocr.enabled}
              onChange={(event) =>
                setDraft({ ...draft, ocr: { ...draft.ocr, enabled: event.target.checked } })
              }
              className="h-4 w-4 rounded border-border"
            />
            Ativar OCR
          </label>
          <div className="space-y-1">
            <span className="text-sm font-medium">Tag do modelo</span>
            <Input
              value={draft.ocr.model}
              onChange={(event) => setDraft({ ...draft, ocr: { ...draft.ocr, model: event.target.value } })}
              placeholder="glm-ocr:latest"
            />
          </div>
          <div className="space-y-1">
            <span className="text-sm font-medium">Endpoint</span>
            <Input
              value={`${draft.ocr.endpoint_host}:${draft.ocr.endpoint_port}`}
              readOnly
            />
            <p className="text-xs text-muted">Loopback apenas nesta versão.</p>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-base">
            <Bot className="h-4 w-4" aria-hidden /> Assistente (LLM do RAG)
          </CardTitle>
        </CardHeader>
        <CardContent className="grid gap-3 sm:grid-cols-2">
          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={draft.assistant.enabled}
              onChange={(event) =>
                setDraft({
                  ...draft,
                  assistant: { ...draft.assistant, enabled: event.target.checked },
                })
              }
              className="h-4 w-4 rounded border-border"
            />
            Ativar assistente
          </label>
          <div className="space-y-1">
            <span className="text-sm font-medium">Tag do LLM</span>
            <Input
              value={draft.assistant.model}
              onChange={(event) =>
                setDraft({ ...draft, assistant: { ...draft.assistant, model: event.target.value } })
              }
              placeholder="qwen3.5:4b"
            />
          </div>
          <div className="space-y-1">
            <span className="text-sm font-medium">Contexto (tokens)</span>
            <Input
              type="number"
              value={draft.assistant.context_tokens}
              onChange={(event) =>
                setDraft({
                  ...draft,
                  assistant: { ...draft.assistant, context_tokens: Number(event.target.value) },
                })
              }
            />
          </div>
          <div className="space-y-1">
            <span className="text-sm font-medium">Limite de saída (tokens)</span>
            <Input
              type="number"
              value={draft.assistant.max_output_tokens}
              onChange={(event) =>
                setDraft({
                  ...draft,
                  assistant: { ...draft.assistant, max_output_tokens: Number(event.target.value) },
                })
              }
            />
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-base">
            <Upload className="h-4 w-4" aria-hidden /> Importar GGUF
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-2 text-sm">
          <Button
            variant="outline"
            onClick={async () => {
              const selection = await pickModelResource('gguf');
              if (selection) {
                toast({
                  title: 'GGUF selecionado',
                  description:
                    'A importação cria uma tag local com um Modelfile gerado pelo aplicativo.',
                  variant: 'info',
                });
              }
            }}
          >
            Selecionar arquivo .gguf
          </Button>
          <p className="text-xs text-muted">
            Pesos não são baixados automaticamente e não acompanham o instalador.
          </p>
        </CardContent>
      </Card>

      <div className="flex flex-wrap items-center gap-2">
        <Button onClick={test} disabled={busy}>
          <ShieldCheck className="mr-1 h-4 w-4" aria-hidden /> Testar modelos
        </Button>
        <Button variant="outline" onClick={apply} disabled={busy}>
          Aplicar perfil
        </Button>
        <p className="text-xs text-muted">
          Aplicar grava o perfil atomicamente e reinicia apenas os processos gerenciados; falha
          restaura a versão anterior.
        </p>
      </div>

      {diagnostics ? (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Diagnóstico</CardTitle>
          </CardHeader>
          <CardContent className="space-y-2 text-sm">
            <div className="flex flex-wrap items-center gap-2">
              <Badge variant={diagnostics.runtime_detected ? 'success' : 'danger'}>
                runtime {diagnostics.runtime_detected ? 'detectado' : 'ausente'}
              </Badge>
              {diagnostics.runtime_version ? (
                <span className="text-muted">versão {diagnostics.runtime_version}</span>
              ) : null}
              <Badge variant={diagnostics.ocr_model_available ? 'success' : 'warning'}>
                OCR {diagnostics.ocr_model_available ? 'instalado' : 'ausente'}
              </Badge>
              <Badge variant={diagnostics.assistant_model_available ? 'success' : 'warning'}>
                LLM {diagnostics.assistant_model_available ? 'instalado' : 'ausente'}
              </Badge>
            </div>
            {diagnostics.ocr_error ? <p className="text-danger">OCR: {diagnostics.ocr_error}</p> : null}
            {diagnostics.assistant_error ? (
              <p className="text-danger">LLM: {diagnostics.assistant_error}</p>
            ) : null}
            {diagnostics.ocr_latency_ms ? (
              <p className="text-muted">Latência OCR: {diagnostics.ocr_latency_ms} ms</p>
            ) : null}
            {diagnostics.assistant_latency_ms ? (
              <p className="text-muted">Latência LLM: {diagnostics.assistant_latency_ms} ms</p>
            ) : null}
            {(diagnostics.notes ?? []).map((note) => (
              <p key={note} className="text-muted">
                {note}
              </p>
            ))}
            {(diagnostics.installed_models ?? []).length > 0 ? (
              <p className="text-xs text-muted">
                Instalados: {(diagnostics.installed_models ?? []).join(', ')}
              </p>
            ) : null}
          </CardContent>
        </Card>
      ) : null}
    </div>
  );
}
