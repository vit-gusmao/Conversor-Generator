import { useEffect, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import {
  ArrowRight,
  Download,
  FileSpreadsheet,
  LayoutGrid,
  Loader2,
  Rows3,
  ShieldCheck,
  Table2,
  TriangleAlert,
} from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Alert } from '@/components/ui/alert';
import { Separator } from '@/components/ui/separator';
import { PageHeader } from '@/components/layout/page-header';
import { EmptyBlock, ErrorBlock, LoadingBlock } from '@/components/layout/states';
import { StatCard } from '@/components/stat-card';
import { QualityBadge, SeverityBadge, StatusBadge } from '@/components/status-badge';
import { SheetPreview } from '@/components/sheet-preview';
import { ExportsPanel } from '@/components/exports-panel';
import { useConversion, useDownloadExcel, useResult } from '@/hooks/use-conversions';
import { useEnsureSession } from '@/hooks/use-session';
import { explainError } from '@/lib/errors';
import { basename, formatBytes, formatNumber } from '@/lib/format';
import { SHEET_KIND_LABELS } from '@/lib/labels';
import { isSuccessful } from '@/lib/status';
import { downloadBlob } from '@/lib/download';
import { cn } from '@/lib/utils';
import { toast } from '@/stores/ui';

export function ResultPage() {
  const { id = '' } = useParams<{ id: string }>();
  useEnsureSession();
  const conversionQuery = useConversion(id);
  const resultQuery = useResult(id);
  const download = useDownloadExcel();
  const [activeSheet, setActiveSheet] = useState<string | null>(null);

  const result = resultQuery.data;
  const conversion = conversionQuery.data;

  useEffect(() => {
    if (result && result.sheets.length > 0 && !activeSheet) {
      setActiveSheet(result.sheets[0]?.name ?? null);
    }
  }, [result, activeSheet]);

  useEffect(() => {
    if (!result) return;
    if (!result.sheets.some((sheet) => sheet.name === activeSheet)) {
      setActiveSheet(result.sheets[0]?.name ?? null);
    }
  }, [result, activeSheet]);

  if (!id) {
    return <ErrorBlock title="Resultado inválido" message="O identificador da conversão não foi informado." />;
  }

  if (conversionQuery.isPending || (resultQuery.isPending && conversionQuery.data?.status !== 'failed')) {
    return (
      <div className="space-y-4">
        <LoadingBlock label="Carregando resultado" />
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          {[0, 1, 2, 3].map((item) => (
            <div key={item} className="h-24 animate-pulse rounded-[var(--radius-card)] border border-border bg-surface/50" />
          ))}
        </div>
      </div>
    );
  }

  const failed =
    conversion?.status === 'failed' ||
    conversion?.status === 'cancelled' ||
    (!result && resultQuery.isError);

  if (failed && !result) {
    return (
      <div className="space-y-4">
        <PageHeader
          backTo={{ to: `/conversions/${id}`, label: 'Processamento' }}
          eyebrow="Resultado"
          title="Esta conversão não gerou um Excel"
          description={conversion?.document.filename}
        />
        <Alert variant="error" title={conversion?.error ? `Código: ${conversion.error.code}` : 'Falha no processamento'}>
          {conversion?.error?.message ?? explainError(resultQuery.error)}
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
          <Button asChild variant="ghost">
            <Link to="/history">Histórico</Link>
          </Button>
        </div>
      </div>
    );
  }

  if (resultQuery.isError || !result) {
    return (
      <div className="space-y-4">
        <ErrorBlock
          title="Não foi possível carregar o resultado"
          message={explainError(resultQuery.error)}
          onRetry={() => void resultQuery.refetch()}
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

  const quality = result.quality;
  const success = isSuccessful(result.status);
  const warnings = result.warnings ?? [];
  const highlighted = warnings.slice(0, 4);

  function handleDownload() {
    download.mutate(id, {
      onSuccess: (blob) => {
        const name = conversion?.document.filename ?? 'conversao.pdf';
        downloadBlob(blob, `${basename(name)}.xlsx`);
        toast({ title: 'Download iniciado', description: 'O arquivo .xlsx foi gerado pela API.', variant: 'success' });
      },
      onError: (cause) => {
        toast({ title: 'Falha no download', description: explainError(cause), variant: 'error' });
      },
    });
  }

  return (
    <div className="space-y-6">
      <PageHeader
        backTo={{ to: `/conversions/${id}`, label: 'Processamento' }}
        eyebrow="Resultado"
        title={success ? 'Conversão concluída' : 'Resultado parcial'}
        description={
          <span className="flex flex-wrap items-center gap-2">
            <span>{conversion?.document.filename ?? 'documento.pdf'}</span>
            {conversion ? (
              <>
                <span className="text-muted-2">· {formatBytes(conversion.document.size_bytes)}</span>
                <QualityBadge quality={quality} />
                <StatusBadge status={result.status} />
              </>
            ) : null}
          </span>
        }
        actions={
          <>
            <Button onClick={handleDownload} disabled={download.isPending}>
              {download.isPending ? (
                <Loader2 className="animate-spin motion-reduce:animate-none" aria-hidden="true" />
              ) : (
                <Download aria-hidden="true" />
              )}
              {download.isPending ? 'Preparando…' : 'Baixar Excel'}
            </Button>
            <Button asChild variant="secondary">
              <Link to={`/conversions/${id}/audit`}>
                <ShieldCheck aria-hidden="true" />
                Ver Auditoria
              </Link>
            </Button>
            <Button asChild variant="outline">
              <Link to="/assistant">
                Perguntar sobre este documento
              </Link>
            </Button>
          </>
        }
      />

      <ExportsPanel conversionId={id} hasTables={(result.tables ?? 0) > 0} />

      {quality === 'partial' ? (
        <Alert variant="warning" title="Conversão concluída com avisos">
          {warnings.length > 0
            ? `${warnings.length} inconsistência(s) foram registradas. Revise a auditoria antes de usar os dados.`
            : 'Alguns elementos do PDF não puderam ser interpretados com confiança.'}{' '}
          <Link className="font-medium underline" to={`/conversions/${id}/audit?tab=issues`}>
            Ver inconsistências
          </Link>
        </Alert>
      ) : null}

      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <StatCard label="Páginas" value={formatNumber(result.pages)} icon={<LayoutGrid className="size-4" aria-hidden="true" />} />
        <StatCard label="Tabelas" value={formatNumber(result.tables)} icon={<Table2 className="size-4" aria-hidden="true" />} />
        <StatCard label="Células" value={formatNumber(result.cells)} icon={<FileSpreadsheet className="size-4" aria-hidden="true" />} />
        <StatCard label="Registros" value={formatNumber(result.records)} icon={<Rows3 className="size-4" aria-hidden="true" />} />
      </div>

      <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.6fr)]">
        <Card>
          <CardHeader className="pb-3">
            <CardTitle>Abas geradas</CardTitle>
          </CardHeader>
          <CardContent className="space-y-2 pt-0">
            {result.sheets.length === 0 ? (
              <EmptyBlock title="Nenhuma aba" description="O arquivo gerado não possui abas." />
            ) : (
              <ul className="space-y-1.5">
                {result.sheets.map((sheet) => (
                  <li key={sheet.name}>
                    <button
                      type="button"
                      onClick={() => setActiveSheet(sheet.name)}
                      aria-pressed={activeSheet === sheet.name}
                      className={cn(
                        'flex w-full flex-col gap-1 rounded-xl border px-3 py-2.5 text-left transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
                        activeSheet === sheet.name
                          ? 'border-accent/40 bg-accent-soft/30'
                          : 'border-border bg-surface-2/40 hover:border-border-strong hover:bg-surface-2/70',
                      )}
                    >
                      <span className="flex items-center justify-between gap-2">
                        <span className="truncate text-sm font-medium text-foreground">{sheet.name}</span>
                        <Badge variant="outline">{SHEET_KIND_LABELS[sheet.kind] ?? sheet.kind}</Badge>
                      </span>
                      <span className="text-[11px] text-muted-2">
                        {formatNumber(sheet.rows)} linhas × {formatNumber(sheet.columns)} colunas · páginas{' '}
                        {sheet.source_pages.join(', ') || '—'}
                      </span>
                    </button>
                  </li>
                ))}
              </ul>
            )}
            <Separator className="my-3" />
            <p className="text-[11px] text-muted-2">
              {formatNumber(result.fields)} campos identificados em {formatNumber(result.sheets_count)} abas.
            </p>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-3">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <CardTitle>Prévia da aba</CardTitle>
              <span className="text-xs text-muted-2">100 linhas por página</span>
            </div>
          </CardHeader>
          <CardContent className="pt-0">
            <SheetPreview
              conversionId={id}
              sheet={activeSheet}
              sheets={result.sheets}
              onSheetChange={setActiveSheet}
            />
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader className="pb-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <CardTitle className="flex items-center gap-2">
              <TriangleAlert className="size-4 text-warning" aria-hidden="true" />
              Principais avisos
            </CardTitle>
            <Link
              to={`/conversions/${id}/audit?tab=issues`}
              className="text-xs font-medium text-accent hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
            >
              Abrir inconsistências na auditoria
            </Link>
          </div>
        </CardHeader>
        <CardContent className="pt-0">
          {highlighted.length === 0 ? (
            <p className="text-sm text-muted">Nenhum aviso registrado nesta conversão.</p>
          ) : (
            <ul className="space-y-2">
              {highlighted.map((issue) => (
                <li
                  key={issue.issue_id}
                  className="flex flex-wrap items-center gap-2 rounded-xl border border-border bg-surface-2/40 px-3 py-2.5"
                >
                  <SeverityBadge severity={issue.severity} />
                  <span className="font-mono text-[11px] text-muted-2">{issue.code}</span>
                  <span className="min-w-0 flex-1 text-sm text-foreground/85">{issue.message}</span>
                  {issue.page_number !== null ? (
                    <Link
                      className="text-xs text-accent hover:underline"
                      to={`/conversions/${id}/audit?tab=pages&page=${issue.page_number}`}
                    >
                      Página {issue.page_number}
                    </Link>
                  ) : null}
                </li>
              ))}
            </ul>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
