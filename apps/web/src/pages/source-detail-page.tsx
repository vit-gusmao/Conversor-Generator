import { useParams } from 'react-router-dom';
import { CalendarPlus, ShieldCheck } from 'lucide-react';
import { Link } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { PageHeader } from '@/components/layout/page-header';
import { EmptyBlock, ErrorBlock } from '@/components/layout/states';
import { useEnsureSession } from '@/hooks/use-session';
import { useSourceRoots, useSources, useValidateSource, sourceKeys } from '@/hooks/use-sources';
import { explainError } from '@/lib/errors';
import { formatBytes, formatDateTime, formatNumber } from '@/lib/format';
import { listImportItems, getImport } from '@/services/sources';
import { toast } from '@/stores/ui';

export function SourceDetailPage() {
  useEnsureSession();
  const { id } = useParams<{ id: string }>();
  const sources = useSources();
  const roots = useSourceRoots();
  const validate = useValidateSource();
  const source = (sources.data ?? []).find((item) => item.id === id);
  const root = (roots.data ?? []).find((item) => item.id === source?.root_id);

  return (
    <div className="space-y-6">
      <PageHeader
        title={source?.name ?? 'Fonte'}
        description="Detalhes, validação amostrada e histórico de importações desta fonte."
        backTo={{ to: '/sources', label: 'Fontes' }}
        actions={
          source ? (
            <Button asChild variant="outline" size="sm">
              <Link to={`/schedules/new?source=${encodeURIComponent(source.id)}`}>
                <CalendarPlus className="mr-1 h-4 w-4" aria-hidden /> Agendar esta fonte
              </Link>
            </Button>
          ) : null
        }
      />

      {sources.isError ? <ErrorBlock message={explainError(sources.error)} /> : null}
      {!sources.isLoading && !source ? (
        <EmptyBlock
          title="Fonte não encontrada"
          description="Ela pode ter sido arquivada ou pertence a outra sessão."
        />
      ) : null}

      {source ? (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Definição</CardTitle>
          </CardHeader>
          <CardContent className="grid gap-3 text-sm sm:grid-cols-2">
            <Field label="Tipo" value={source.kind === 'directory' ? 'Pasta' : 'Arquivo'} />
            <Field label="Caminho (relativo à raiz)" value={source.relative_path} />
            <Field label="Recursiva" value={source.recursive ? 'sim' : 'não'} />
            <Field label="Incluir" value={(source.include_patterns ?? []).join(', ') || '—'} />
            <Field label="Excluir" value={(source.exclude_patterns ?? []).join(', ') || '—'} />
            <Field label="Raiz / máquina" value={root?.label ?? root?.id ?? source.root_id} />
            <Field label="Versão" value={`v${source.version}`} />
            <Field label="Criada em" value={formatDateTime(source.created_at)} />
          </CardContent>
        </Card>
      ) : null}

      {source ? (
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-base">
              <ShieldCheck className="h-4 w-4" aria-hidden /> Validação
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-3 text-sm">
            <Button
              size="sm"
              variant="outline"
              onClick={() =>
                validate.mutate(source.id, {
                  onSuccess: (result) => {
                    if (!result.ok) {
                      toast({ title: 'Fonte indisponível', description: result.message ?? '', variant: 'error' });
                      return;
                    }
                    toast({
                      title: 'Fonte acessível',
                      description: `${formatNumber(result.estimated_files)} arquivo(s) · ${formatBytes(result.estimated_bytes)}`,
                      variant: 'success',
                    });
                  },
                  onError: (error) => toast({ title: 'Falha', description: explainError(error), variant: 'error' }),
                })
              }
            >
              Validar acesso e formatos
            </Button>
            {validate.data ? (
              <div className="space-y-2">
                <div className="flex flex-wrap items-center gap-2">
                  <Badge variant={validate.data.ok ? 'success' : 'danger'}>
                    {validate.data.ok ? 'disponível' : (validate.data.code ?? 'indisponível')}
                  </Badge>
                  <span className="text-muted">
                    {formatNumber(validate.data.estimated_files)} arquivo(s) ·{' '}
                    {formatBytes(validate.data.estimated_bytes)}
                  </span>
                </div>
                {validate.data.message ? <p className="text-danger">{validate.data.message}</p> : null}
                <ul className="max-h-64 overflow-auto divide-y divide-border/60 text-xs">
                  {validate.data.sample.map((candidate) => (
                    <li key={candidate.relative_path} className="flex justify-between gap-2 py-1">
                      <span className="truncate">{candidate.relative_path}</span>
                      <span className="text-muted">{formatBytes(candidate.size_bytes)}</span>
                    </li>
                  ))}
                </ul>
              </div>
            ) : null}
          </CardContent>
        </Card>
      ) : null}

      {source ? <RecentImports sourceId={source.id} /> : null}
    </div>
  );
}

function Field({ label, value }: { label: string; value: string }) {
  return (
    <div className="space-y-0.5">
      <p className="text-xs uppercase tracking-wide text-muted">{label}</p>
      <p className="break-words text-foreground">{value}</p>
    </div>
  );
}

/**
 * Recent imports of this source. The engine exposes imports by id; the list
 * here reads the last import created for the source when the page reloads
 * (the id travels in the URL of the sources page otherwise).
 */
function RecentImports({ sourceId }: { sourceId: string }) {
  const query = useQuery({
    queryKey: [...sourceKeys.list, sourceId, 'imports'],
    queryFn: async () => {
      // No list-by-source endpoint: read from the last import id stored when
      // the user started one in this browser.
      const stored = window.localStorage.getItem(`pdf2x.lastImport.${sourceId}`);
      if (!stored) return null;
      const record = await getImport(stored);
      const items = await listImportItems(stored);
      return { record, items };
    },
    staleTime: 5_000,
  });

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Últimas importações</CardTitle>
      </CardHeader>
      <CardContent className="space-y-2 text-sm">
        {query.isLoading ? <p className="text-muted">Carregando…</p> : null}
        {!query.isLoading && !query.data ? (
          <p className="text-muted">
            Nenhuma importação registrada nesta máquina. Use “Importar agora” na página de Fontes.
          </p>
        ) : null}
        {query.data ? (
          <>
            <div className="flex flex-wrap items-center gap-2">
              <Badge variant="info">{query.data.record.status}</Badge>
              <span className="text-muted">
                {formatNumber(query.data.record.counts.imported)} importado(s) ·{' '}
                {formatNumber(query.data.record.counts.skipped)} ignorado(s) ·{' '}
                {formatNumber(query.data.record.counts.failed)} falha(s)
              </span>
            </div>
            <ul className="divide-y divide-border/60 text-xs">
              {query.data.items.map((item) => (
                <li key={item.id} className="flex flex-wrap items-center justify-between gap-2 py-1">
                  <span className="truncate">{item.relative_path}</span>
                  <span className="flex items-center gap-2 text-muted">
                    <Badge variant={item.status === 'failed' ? 'danger' : 'muted'}>{item.status}</Badge>
                    {item.conversion_id ? (
                      <Link to={`/conversions/${item.conversion_id}`} className="text-accent hover:underline">
                        conversão
                      </Link>
                    ) : null}
                  </span>
                </li>
              ))}
            </ul>
          </>
        ) : null}
      </CardContent>
    </Card>
  );
}
