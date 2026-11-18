import { useEffect, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { ArrowRight, ShieldCheck } from 'lucide-react';

import { Alert } from '@/components/ui/alert';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { PageHeader } from '@/components/layout/page-header';
import { EmptyBlock, ErrorBlock, LoadingBlock } from '@/components/layout/states';
import { QualityBadge, StatusBadge } from '@/components/status-badge';
import { SheetPreview } from '@/components/sheet-preview';
import { SheetSelector } from '@/components/sheet-selector';
import { ResultActions } from '@/components/result-actions';
import { ResultSummaryLine } from '@/components/result-summary';
import { useConversion } from '@/hooks/use-conversions';
import { useResultOverview, useSheets } from '@/hooks/use-results';
import { useEnsureSession } from '@/hooks/use-session';
import { explainError } from '@/lib/errors';
import { formatBytes } from '@/lib/format';

/**
 * Minimalist result screen: name, status, actions and ONE preview.
 *
 * Everything technical (hashes, engines, per-stage counts) lives behind
 * "Detalhes", and the audit trail keeps its own route. Nothing is removed from
 * the generated workbook: the screen only stops showing it all at once.
 */
export function ResultPage() {
  const { id = '' } = useParams<{ id: string }>();
  useEnsureSession();

  const conversionQuery = useConversion(id);
  const overviewQuery = useResultOverview(id);
  const [activeSheet, setActiveSheet] = useState<string | null>(null);

  const overview = overviewQuery.data;
  const conversion = conversionQuery.data;
  const sheetsQuery = useSheets(id, { limit: 50 });
  const sheets = sheetsQuery.data?.items ?? [];
  // `SheetPreview` only needs name/kind/rows/columns to draw its caption; the
  // page list lives behind the paginated provenance endpoint.
  const previewSheets = sheets.map((sheet) => ({
    name: sheet.name,
    kind: sheet.kind,
    rows: sheet.rows,
    columns: sheet.columns,
    source_pages: [] as number[],
    table_id: sheet.table_id,
  }));

  // The default sheet comes from the server; a stale selection (after a retry)
  // falls back to it instead of showing an empty table.
  useEffect(() => {
    if (!overview) return;
    const stale =
      activeSheet !== null &&
      sheets.length > 0 &&
      !sheets.some((sheet) => sheet.name === activeSheet);
    if (activeSheet === null || stale) setActiveSheet(overview.default_sheet);
  }, [overview, sheets, activeSheet]);

  if (!id) {
    return (
      <ErrorBlock title="Resultado inválido" message="O identificador da conversão não foi informado." />
    );
  }

  const failed =
    conversion?.status === 'failed' ||
    conversion?.status === 'cancelled' ||
    overview?.status === 'failed' ||
    overview?.status === 'cancelled';

  if (overviewQuery.isPending) {
    return <LoadingBlock label="Carregando resultado" />;
  }

  if (failed && !overview) {
    return (
      <div className="space-y-4">
        <PageHeader
          backTo={{ to: `/conversions/${id}`, label: 'Processamento' }}
          eyebrow="Resultado"
          title="Esta conversão não gerou um arquivo"
          description={conversion?.document.filename}
        />
        <Alert
          variant="error"
          title={conversion?.error ? `Código: ${conversion.error.code}` : 'Falha no processamento'}
        >
          {conversion?.error?.message ?? explainError(overviewQuery.error)}
        </Alert>
        {conversion?.status === 'cancelled' ? (
          <Alert variant="warning">A conversão foi cancelada antes da geração do arquivo.</Alert>
        ) : null}
        <div className="flex flex-wrap gap-2">
          <Button asChild>
            <Link to={`/conversions/${id}/audit`}>
              <ShieldCheck aria-hidden="true" />
              Ver auditoria
            </Link>
          </Button>
          <Button asChild variant="secondary">
            <Link to={`/conversions/${id}`}>
              <ArrowRight aria-hidden="true" />
              Abrir processamento
            </Link>
          </Button>
        </div>
      </div>
    );
  }

  if (overviewQuery.isError || !overview) {
    return (
      <div className="space-y-4">
        <ErrorBlock
          title="Não foi possível carregar o resultado"
          message={explainError(overviewQuery.error)}
          onRetry={() => void overviewQuery.refetch()}
        />
        <p className="text-xs text-muted-2">
          Se a conversão ainda estiver em andamento, acompanhe em{' '}
          <Link className="text-accent hover:underline" to={`/conversions/${id}`}>
            processamento
          </Link>
          . A auditoria continua disponível em{' '}
          <Link className="text-accent hover:underline" to={`/conversions/${id}/audit`}>
            /audit
          </Link>
          .
        </p>
      </div>
    );
  }

  const filename = conversion?.document.filename ?? 'documento.pdf';
  const quarantined = overview.publication_status === 'quarantined';

  return (
    <div className="space-y-5">
      <PageHeader
        backTo={{ to: `/conversions/${id}`, label: 'Voltar' }}
        eyebrow="Resultado"
        title={filename}
        description={
          <span className="flex flex-wrap items-center gap-2">
            <StatusBadge status={overview.status} />
            <QualityBadge quality={overview.quality} />
            {conversion ? (
              <span className="text-muted-2">{formatBytes(conversion.document.size_bytes)}</span>
            ) : null}
            {overview.partial ? <Badge variant="warning">Resultado parcial</Badge> : null}
          </span>
        }
        actions={<ResultActions conversionId={id} overview={overview} filename={filename} />}
      />

      {quarantined ? (
        <Alert variant="error" title="Artefato em quarentena">
          A auditoria final reprovou o arquivo gerado, então o download está bloqueado. O
          diagnóstico continua acessível.{' '}
          <Link className="font-medium underline" to={`/conversions/${id}/audit`}>
            Abrir auditoria
          </Link>
        </Alert>
      ) : null}

      {overview.partial && !quarantined ? (
        <Alert variant="warning" title="Resultado incompleto">
          {overview.pages_failed > 0
            ? `${overview.pages_failed} de ${overview.pages_total} página(s) não puderam ser processadas. O arquivo contém apenas o conteúdo recuperado.`
            : 'Alguns elementos do documento não puderam ser interpretados com confiança.'}{' '}
          <Link className="font-medium underline" to={`/conversions/${id}/audit?tab=pages`}>
            Ver páginas
          </Link>
        </Alert>
      ) : null}

      <ResultSummaryLine conversionId={id} overview={overview} />

      <Card>
        <CardHeader className="space-y-3 pb-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <CardTitle>Prévia</CardTitle>
            {overview.counts.sheets > 0 ? (
              <span className="text-xs text-muted-2">{overview.counts.sheets} aba(s) no arquivo</span>
            ) : null}
          </div>
          {overview.counts.sheets > 0 ? (
            <SheetSelector conversionId={id} value={activeSheet} onChange={setActiveSheet} />
          ) : null}
        </CardHeader>
        <CardContent className="pt-0">
          {overview.counts.sheets === 0 ? (
            <EmptyBlock
              title="Sem tabela para prévia"
              description="Este documento foi entregue apenas como texto; abra o arquivo TXT."
            />
          ) : (
            <SheetPreview
              conversionId={id}
              sheet={activeSheet}
              sheets={previewSheets}
              // The selector above is the only sheet control on this page.
              showSheetNavigation={false}
              runId={overview.run_id}
            />
          )}
        </CardContent>
      </Card>

      <details className="rounded-[var(--radius-card)] border border-border bg-surface/50">
        <summary className="cursor-pointer list-none px-4 py-3 text-sm font-medium text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent">
          Detalhes do documento
        </summary>
        <div className="space-y-3 border-t border-border px-4 py-3 text-xs text-muted">
          <dl className="grid gap-x-6 gap-y-1.5 sm:grid-cols-2">
            <div className="flex justify-between gap-3">
              <dt className="text-muted-2">Execução</dt>
              <dd className="truncate font-mono text-foreground/80">{overview.run_id}</dd>
            </div>
            <div className="flex justify-between gap-3">
              <dt className="text-muted-2">Publicação</dt>
              <dd>{overview.publication_status}</dd>
            </div>
            <div className="flex justify-between gap-3">
              <dt className="text-muted-2">Campos</dt>
              <dd>{overview.counts.fields}</dd>
            </div>
            <div className="flex justify-between gap-3">
              <dt className="text-muted-2">Avisos</dt>
              <dd>
                {overview.warning_counts.total} ({overview.warning_counts.error} erro(s))
              </dd>
            </div>
            <div className="flex justify-between gap-3">
              <dt className="text-muted-2">Formatos</dt>
              <dd>{overview.available_formats.join(', ') || '—'}</dd>
            </div>
            <div className="flex justify-between gap-3">
              <dt className="text-muted-2">Aba inicial</dt>
              <dd className="truncate">{overview.default_sheet ?? '—'}</dd>
            </div>
          </dl>
          <p className="text-[11px] text-muted-2">
            Hashes, evidências por célula, motores e tempos por etapa ficam na{' '}
            <Link className="text-accent hover:underline" to={`/conversions/${id}/audit`}>
              auditoria
            </Link>
            .
          </p>
        </div>
      </details>
    </div>
  );
}
