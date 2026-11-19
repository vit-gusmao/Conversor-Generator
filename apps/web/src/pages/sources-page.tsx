import { useState } from 'react';
import { Link } from 'react-router-dom';
import { FolderPlus, Play, RefreshCw, ShieldCheck, XCircle } from 'lucide-react';
import type { SourceRecord, SourceRootInfo } from '@pdf2x/contracts';
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
import { EmptyBlock, ErrorBlock } from '@/components/layout/states';
import { ConfirmDialog } from '@/components/confirm-dialog';
import { useEnsureSession } from '@/hooks/use-session';
import {
  useArchiveSource,
  useCreateSource,
  useImport,
  useImportItems,
  useSourceRoots,
  useSources,
  useStartImport,
  useValidateSource,
} from '@/hooks/use-sources';
import { explainError } from '@/lib/errors';
import { formatBytes, formatDateTime, formatNumber } from '@/lib/format';
import { toast } from '@/stores/ui';

const IMPORT_STATE_LABELS: Record<string, string> = {
  pending: 'Preparando',
  scanning: 'Varrendo pasta',
  submitting: 'Enviando arquivos',
  converting: 'Convertendo',
  indexing: 'Indexando',
  finalize: 'Finalizando',
  completed: 'Concluída',
  partial: 'Parcial',
  failed: 'Falhou',
  cancelled: 'Cancelada',
};

/**
 * Fontes: cadastro de arquivos/pastas dentro de raízes concedidas, validação
 * amostrada e importação manual. Nenhuma conversão começa apenas ao salvar.
 */
export function SourcesPage() {
  useEnsureSession();
  const roots = useSourceRoots();
  const sources = useSources();
  const createSource = useCreateSource();
  const archiveSource = useArchiveSource();
  const validateSource = useValidateSource();
  const startImport = useStartImport();

  const [name, setName] = useState('');
  const [rootId, setRootId] = useState('');
  const [relativePath, setRelativePath] = useState('');
  const [kind, setKind] = useState<'file' | 'directory'>('directory');
  const [recursive, setRecursive] = useState(false);
  const [include, setInclude] = useState('*.xlsx,*.csv,*.pdf');
  const [exclude, setExclude] = useState('~$*,*.tmp');
  const [pendingArchive, setPendingArchive] = useState<SourceRecord | null>(null);
  const [activeImportId, setActiveImportId] = useState<string | null>(null);
  const importState = useImport(activeImportId);
  const importItems = useImportItems(activeImportId);

  const availableRoots: SourceRootInfo[] = (roots.data ?? []).filter(
    (root) => root.runtime_available && !root.revoked_at,
  );
  const hasRoots = availableRoots.length > 0;

  const submit = () => {
    if (!name.trim() || !rootId || !relativePath.trim()) {
      toast({ title: 'Dados incompletos', description: 'Informe nome, raiz concedida e caminho relativo.', variant: 'error' });
      return;
    }
    createSource.mutate(
      {
        root_id: rootId,
        relative_path: relativePath.trim(),
        kind,
        name: name.trim(),
        recursive: kind === 'directory' ? recursive : false,
        include_patterns: include
          .split(',')
          .map((item) => item.trim())
          .filter(Boolean),
        exclude_patterns: exclude
          .split(',')
          .map((item) => item.trim())
          .filter(Boolean),
      },
      {
        onSuccess: () => {
          toast({ title: 'Fonte cadastrada', description: 'Nenhuma conversão foi iniciada.', variant: 'success' });
          setName('');
          setRelativePath('');
        },
        onError: (error) => toast({ title: 'Falha', description: explainError(error), variant: 'error' }),
      },
    );
  };

  const runImport = (source: SourceRecord) => {
    const key = `${source.id}:${Date.now()}`;
    startImport.mutate(
      { sourceId: source.id, body: { options: { schema_version: 3 } }, idempotencyKey: key },
      {
        onSuccess: (record) => {
          setActiveImportId(record.id);
          // Detail page reads the last import for this source on demand.
          window.localStorage.setItem(`pdf2x.lastImport.${source.id}`, record.id);
          toast({ title: 'Importação iniciada', variant: 'success' });
        },
        onError: (error) => {
          toast({ title: 'Falha na importação', description: explainError(error), variant: 'error' });
        },
      },
    );
  };

  return (
    <div className="space-y-6">
      <PageHeader
        title="Fontes"
        description="Arquivos e pastas autorizados são lidos na máquina que executa o conversor."
        actions={
          <Badge variant={hasRoots ? 'success' : 'warning'}>
            {hasRoots ? `${availableRoots.length} raiz(es) disponível(is)` : 'sem raiz concedida'}
          </Badge>
        }
      />

      {roots.isError ? (
        <ErrorBlock message={explainError(roots.error)} onRetry={() => void roots.refetch()} />
      ) : null}

      {!roots.isLoading && !hasRoots ? (
        <Card>
          <CardContent className="space-y-2 pt-6 text-sm text-muted">
            <p className="font-medium text-foreground">Nenhuma pasta foi concedida ainda.</p>
            <p>
              A concessão de uma pasta é uma ação administrativa local. No aplicativo desktop use
              “Escolher pasta”; ao rodar apenas o site local, o operador concede a raiz pela
              configuração local. O navegador nunca ganha acesso ao disco por digitar um caminho.
            </p>
          </CardContent>
        </Card>
      ) : null}

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-base">
            <FolderPlus className="h-4 w-4" aria-hidden /> Adicionar fonte
          </CardTitle>
        </CardHeader>
        <CardContent className="grid gap-4 sm:grid-cols-2">
          <div className="space-y-1">
            <span className="text-sm font-medium">Nome</span>
            <Input
              id="source-name"
              value={name}
              onChange={(event) => setName(event.target.value)}
              placeholder="Financeiro"
            />
          </div>
          <div className="space-y-1">
            <span className="text-sm font-medium">Raiz concedida</span>
            <Select value={rootId} onValueChange={setRootId} disabled={!hasRoots}>
              <SelectTrigger aria-label="Raiz concedida">
                <SelectValue placeholder={hasRoots ? 'Escolha a raiz' : 'Nenhuma raiz'} />
              </SelectTrigger>
              <SelectContent>
                {availableRoots.map((root) => (
                  <SelectItem key={root.id} value={root.id}>
                    {root.label ?? root.id} · {root.runtime_id}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-1">
            <span className="text-sm font-medium">Caminho relativo à raiz</span>
            <Input
              id="source-path"
              value={relativePath}
              onChange={(event) => setRelativePath(event.target.value)}
              placeholder="financeiro/planilhas"
            />
          </div>
          <div className="space-y-1">
            <span className="text-sm font-medium">Tipo</span>
            <Select value={kind} onValueChange={(value) => setKind(value as 'file' | 'directory')}>
              <SelectTrigger aria-label="Tipo da fonte">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="directory">Pasta</SelectItem>
                <SelectItem value="file">Arquivo único</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-1">
            <span className="text-sm font-medium">Incluir (separado por vírgula)</span>
            <Input id="source-include" value={include} onChange={(event) => setInclude(event.target.value)} />
          </div>
          <div className="space-y-1">
            <span className="text-sm font-medium">Excluir (separado por vírgula)</span>
            <Input id="source-exclude" value={exclude} onChange={(event) => setExclude(event.target.value)} />
          </div>
          {kind === 'directory' ? (
            <label className="flex items-center gap-2 text-sm text-muted">
              <input
                type="checkbox"
                checked={recursive}
                onChange={(event) => setRecursive(event.target.checked)}
                className="h-4 w-4 rounded border-border"
              />
              Incluir subpastas
            </label>
          ) : null}
          <div className="sm:col-span-2">
            <Button onClick={submit} disabled={createSource.isPending || !hasRoots}>
              {createSource.isPending ? 'Cadastrando…' : 'Cadastrar fonte'}
            </Button>
            <p className="mt-2 text-xs text-muted">
              Salvar a fonte não inicia conversão. Use “Validar” e depois “Importar agora”.
            </p>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Fontes cadastradas</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          {sources.isLoading ? <p className="text-sm text-muted">Carregando…</p> : null}
          {sources.isError ? (
            <ErrorBlock message={explainError(sources.error)} onRetry={() => void sources.refetch()} />
          ) : null}
          {sources.data && sources.data.length === 0 ? (
            <EmptyBlock
              title="Nenhuma fonte cadastrada"
              description="Cadastre uma pasta para importar planilhas de forma manual ou agendada."
            />
          ) : null}
          {(sources.data ?? []).map((source) => (
            <div
              key={source.id}
              className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-border/70 p-3"
            >
              <div className="min-w-0 space-y-1">
                <div className="flex items-center gap-2">
                  <Link
                    to={`/sources/${source.id}`}
                    className="truncate font-medium text-foreground hover:text-accent"
                  >
                    {source.name}
                  </Link>
                  <Badge variant={source.enabled ? 'success' : 'warning'}>
                    {source.enabled ? 'ativa' : 'desativada'}
                  </Badge>
                  
                </div>
                <p className="truncate text-xs text-muted">
                  {source.kind === 'directory' ? 'pasta' : 'arquivo'} ·{' '}
                  {source.relative_path}
                  {source.recursive ? ' · recursiva' : ''} · v{source.version}
                </p>
              </div>
              <div className="flex flex-wrap items-center gap-2">
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() =>
                    validateSource.mutate(source.id, {
                      onSuccess: (validation) => {
                        if (!validation.ok) {
                          toast({ title: 'Fonte indisponível', description: validation.message ?? 'Sem acesso.', variant: 'error' });
                          return;
                        }
                        toast({
                          title: 'Fonte validada',
                          description:
                            `${formatNumber(validation.estimated_files)} arquivo(s), ` +
                            `${formatBytes(validation.estimated_bytes)} (amostra de ${validation.sample.length}).`,
                          variant: 'success',
                        });
                      },
                      onError: (error) => toast({ title: 'Falha', description: explainError(error), variant: 'error' }),
                    })
                  }
                >
                  <ShieldCheck className="mr-1 h-4 w-4" aria-hidden /> Validar
                </Button>
                <Button size="sm" onClick={() => runImport(source)} disabled={startImport.isPending}>
                  <Play className="mr-1 h-4 w-4" aria-hidden /> Importar agora
                </Button>
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => setPendingArchive(source)}
                  aria-label={`Arquivar ${source.name}`}
                >
                  <XCircle className="h-4 w-4" aria-hidden />
                </Button>
              </div>
            </div>
          ))}
        </CardContent>
      </Card>

      {activeImportId ? (
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-base">
              <RefreshCw className="h-4 w-4" aria-hidden /> Importação
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-3 text-sm">
            <div className="flex flex-wrap items-center gap-2">
              <Badge variant="info">
                {IMPORT_STATE_LABELS[importState.data?.status ?? 'pending'] ??
                  importState.data?.status}
              </Badge>
              {importState.data ? (
                <span className="text-muted">
                  {formatNumber(importState.data.counts.imported)} importado(s) ·{' '}
                  {formatNumber(importState.data.counts.skipped)} ignorado(s) ·{' '}
                  {formatNumber(importState.data.counts.failed)} falha(s)
                </span>
              ) : null}
            </div>
            {importState.data?.error ? (
              <p className="text-danger">{importState.data.error.message}</p>
            ) : null}
            {(importItems.data ?? []).length > 0 ? (
              <ul className="divide-y divide-border/60">
                {(importItems.data ?? []).map((item) => (
                  <li key={item.id} className="flex flex-wrap items-center justify-between gap-2 py-2">
                    <span className="truncate">{item.relative_path}</span>
                    <span className="flex items-center gap-2 text-xs text-muted">
                      <Badge variant={item.status === 'failed' ? 'danger' : 'muted'}>
                        {item.status}
                      </Badge>
                      {item.error_code ? <span title={item.error_message ?? ''}>{item.error_code}</span> : null}
                      {item.conversion_id ? (
                        <Link
                          to={`/conversions/${item.conversion_id}`}
                          className="text-accent hover:underline"
                        >
                          conversão
                        </Link>
                      ) : null}
                    </span>
                  </li>
                ))}
              </ul>
            ) : null}
          </CardContent>
        </Card>
      ) : null}

      <ConfirmDialog
        open={pendingArchive !== null}
        onOpenChange={(open) => {
          if (!open) setPendingArchive(null);
        }}
        title="Arquivar fonte?"
        description={
          pendingArchive
            ? `“${pendingArchive.name}” deixará de importar. O histórico e os artefatos existentes permanecem.`
            : ''
        }
        confirmLabel="Arquivar"
        onConfirm={() => {
          if (!pendingArchive) return;
          archiveSource.mutate(pendingArchive.id, {
            onSuccess: () => toast({ title: 'Fonte arquivada', variant: 'success' }),
            onError: (error) => toast({ title: 'Falha', description: explainError(error), variant: 'error' }),
          });
          setPendingArchive(null);
        }}
      />
      <p className="text-xs text-muted">
        Última atualização: {sources.dataUpdatedAt ? formatDateTime(new Date(sources.dataUpdatedAt).toISOString()) : '—'}
      </p>
    </div>
  );
}
