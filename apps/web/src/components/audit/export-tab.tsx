import { useMemo, useState } from 'react';
import type { AuditSummary } from '@pdf2x/contracts';
import { ClipboardCopy, Download, FileJson, FileText, Loader2 } from 'lucide-react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { ErrorBlock, LoadingBlock } from '@/components/layout/states';
import { Button } from '@/components/ui/button';
import { useAuditEvents, useAuditIssues } from '@/hooks/use-audit';
import { copyText, downloadBlob } from '@/lib/download';
import { explainError } from '@/lib/errors';
import { formatNumber } from '@/lib/format';
import { buildAuditTextReport, exportAuditFile } from '@/services/audit';
import { toast } from '@/stores/ui';

export function ExportTab({ conversionId, summary }: { conversionId: string; summary: AuditSummary }) {
  const issues = useAuditIssues(conversionId);
  const events = useAuditEvents(conversionId, { limit: 200 });
  const [exporting, setExporting] = useState<'json' | null>(null);

  const report = useMemo(
    () =>
      buildAuditTextReport({
        summary,
        issues: issues.data?.items ?? [],
        events: (events.data?.pages ?? []).flatMap((page) => page.items),
      }),
    [summary, issues.data, events.data],
  );

  async function handleJson() {
    setExporting('json');
    try {
      const blob = await exportAuditFile(conversionId, 'json');
      downloadBlob(blob, `auditoria-${conversionId}.json`);
      toast({ title: 'Auditoria exportada', description: 'O arquivo JSON foi gerado pela API.', variant: 'success' });
    } catch (cause) {
      toast({ title: 'Falha ao exportar', description: explainError(cause), variant: 'error' });
    } finally {
      setExporting(null);
    }
  }

  function handleTxt() {
    const blob = new Blob([report], { type: 'text/plain;charset=utf-8' });
    downloadBlob(blob, `relatorio-auditoria-${conversionId}.txt`);
    toast({ title: 'Relatório gerado', description: 'O TXT foi montado localmente a partir dos dados carregados.', variant: 'success' });
  }

  async function handleCopy() {
    const ok = await copyText(report);
    toast({
      title: ok ? 'Resumo copiado' : 'Não foi possível copiar',
      description: ok ? 'O relatório está na área de transferência.' : 'Seu navegador bloqueou o acesso à área de transferência.',
      variant: ok ? 'success' : 'error',
    });
  }

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader className="pb-3">
          <CardTitle>Exportar auditoria</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4 pt-0">
          {issues.isError || events.isError ? (
            <ErrorBlock
              title="Não foi possível carregar todos os dados do relatório"
              message={explainError(issues.error ?? events.error)}
              onRetry={() => {
                void issues.refetch();
                void events.refetch();
              }}
            />
          ) : null}
          <p className="text-sm text-muted">
            O JSON é produzido pela API com todos os eventos e metadados. O relatório TXT é montado neste
            navegador a partir do resumo e das inconsistências já carregadas, para leitura rápida e offline.
          </p>
          <div className="flex flex-wrap gap-2">
            <Button onClick={() => void handleJson()} disabled={exporting !== null}>
              {exporting === 'json' ? (
                <Loader2 className="animate-spin motion-reduce:animate-none" aria-hidden="true" />
              ) : (
                <FileJson aria-hidden="true" />
              )}
              Baixar JSON da auditoria
            </Button>
            <Button variant="secondary" onClick={handleTxt}>
              <FileText aria-hidden="true" />
              Baixar relatório TXT
            </Button>
            <Button variant="outline" onClick={() => void handleCopy()}>
              <ClipboardCopy aria-hidden="true" />
              Copiar resumo
            </Button>
          </div>
          <p className="text-[11px] text-muted-2">
            {formatNumber(summary.metrics.events_total)} eventos ·{' '}
            {formatNumber(issues.data?.items.length ?? 0)} inconsistências carregadas
            {events.isFetching ? ' · carregando eventos…' : ''}
          </p>
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="flex items-center gap-2">
            <Download className="size-4 text-accent" aria-hidden="true" />
            Prévia do relatório textual
          </CardTitle>
        </CardHeader>
        <CardContent className="pt-0">
          {issues.isPending || events.isPending ? (
            <LoadingBlock label="Carregando dados do relatório" className="mb-3" />
          ) : null}
          <pre className="max-h-96 overflow-auto rounded-xl border border-border bg-background/70 p-4 text-[11px] leading-relaxed text-muted scrollbar-thin">
            {report}
          </pre>
        </CardContent>
      </Card>
    </div>
  );
}
