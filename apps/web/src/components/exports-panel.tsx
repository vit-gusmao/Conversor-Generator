import { Download, FileText, Loader2, Plus, Table2, TriangleAlert } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { useCreateExport, useDownloadExport, useExports } from '@/hooks/use-conversions';
import { explainError } from '@/lib/errors';
import { formatBytes } from '@/lib/format';
import { downloadBlob } from '@/lib/download';
import { toast } from '@/stores/ui';

const FORMAT_LABEL: Record<string, string> = {
  txt: 'TXT',
  csv: 'CSV',
  zip: 'CSV (pacote)',
  xlsx: 'XLSX',
};

/**
 * Downloadable artifacts produced from the same run.  TXT/CSV are generated
 * from the stored textual IR, so opening this panel never re-runs OCR.
 */
export function ExportsPanel({
  conversionId,
  hasTables = true,
}: {
  conversionId: string;
  hasTables?: boolean;
}) {
  const exportsQuery = useExports(conversionId);
  const createExport = useCreateExport(conversionId);
  const download = useDownloadExport(conversionId);

  const items = exportsQuery.data?.items ?? [];
  const has = (format: string) =>
    items.some((item) => item.format === format || (format === 'csv' && item.format === 'zip'));

  function handleDownload(exportId: string, filename: string) {
    download.mutate(exportId, {
      onSuccess: (blob) => {
        downloadBlob(blob, filename);
        toast({
          title: 'Download iniciado',
          description: `${filename} foi gerado a partir da mesma conversão.`,
          variant: 'success',
        });
      },
      onError: (cause) => {
        toast({ title: 'Falha no download', description: explainError(cause), variant: 'error' });
      },
    });
  }

  function handleCreate(format: 'txt' | 'csv') {
    createExport.mutate(
      { format },
      {
        onSuccess: () => {
          toast({
            title: `Exportação ${format.toUpperCase()} criada`,
            description: 'O artefato usa o texto já extraído, sem novo OCR.',
            variant: 'success',
          });
        },
        onError: (cause) => {
          toast({ title: 'Falha ao exportar', description: explainError(cause), variant: 'error' });
        },
      },
    );
  }

  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between gap-3 space-y-0 pb-3">
        <CardTitle className="text-base">Arquivos gerados</CardTitle>
        <Badge variant="outline">mesmo OCR · hashes por artefato</Badge>
      </CardHeader>
      <CardContent className="space-y-3 pt-0">
        {exportsQuery.isPending ? (
          <p className="text-sm text-muted">Carregando artefatos…</p>
        ) : exportsQuery.isError ? (
          <p className="text-sm text-muted">Não foi possível listar os artefatos.</p>
        ) : items.length === 0 ? (
          <div className="flex flex-wrap items-center gap-2">
            <p className="mr-auto text-sm text-muted">
              Nenhum artefato adicional. Gere TXT ou CSV sem repetir a extração.
            </p>
            <Button
              size="sm"
              variant="secondary"
              onClick={() => handleCreate('txt')}
              disabled={createExport.isPending}
            >
              <Plus aria-hidden="true" />
              Gerar TXT
            </Button>
            <Button
              size="sm"
              variant="secondary"
              onClick={() => handleCreate('csv')}
              disabled={createExport.isPending || !hasTables}
              title={hasTables ? undefined : 'Nenhuma tabela confiável foi identificada'}
            >
              <Plus aria-hidden="true" />
              Gerar CSV
            </Button>
          </div>
        ) : (
          <ul className="space-y-2">
            {items.map((item) => {
              const quarantined = item.publication_status === 'quarantined';
              const Icon = item.format === 'csv' || item.format === 'zip' ? Table2 : FileText;
              return (
                <li
                  key={item.export_id}
                  className="flex flex-wrap items-center gap-3 rounded-xl border border-border bg-surface/60 px-3 py-2"
                >
                  <span className="grid size-9 place-items-center rounded-lg bg-accent/10 text-accent">
                    <Icon className="size-4" aria-hidden="true" />
                  </span>
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-sm font-medium text-foreground">{item.filename}</p>
                    <p className="text-[11px] text-muted-2">
                      {FORMAT_LABEL[item.format] ?? item.format.toUpperCase()} ·{' '}
                      {formatBytes(item.size_bytes)} · sha256 {item.sha256.slice(0, 12)}…
                    </p>
                  </div>
                  {item.warnings.length > 0 ? (
                    <span
                      className="inline-flex items-center gap-1 text-[11px] text-warning"
                      title={item.warnings.map((warning) => warning.message).join(' ')}
                    >
                      <TriangleAlert className="size-3.5" aria-hidden="true" />
                      {item.warnings.length} aviso(s)
                    </span>
                  ) : null}
                  {quarantined ? (
                    <Badge variant="danger">quarentena</Badge>
                  ) : (
                    <Button
                      size="sm"
                      variant="outline"
                      onClick={() => handleDownload(item.export_id, item.filename)}
                      disabled={download.isPending}
                    >
                      {download.isPending ? (
                        <Loader2 className="animate-spin motion-reduce:animate-none" aria-hidden="true" />
                      ) : (
                        <Download aria-hidden="true" />
                      )}
                      Baixar
                    </Button>
                  )}
                </li>
              );
            })}
          </ul>
        )}

        {items.length > 0 && !has('txt') ? (
          <Button size="sm" variant="ghost" onClick={() => handleCreate('txt')} disabled={createExport.isPending}>
            <Plus aria-hidden="true" />
            Gerar TXT
          </Button>
        ) : null}
        {items.length > 0 && !has('csv') && hasTables ? (
          <Button size="sm" variant="ghost" onClick={() => handleCreate('csv')} disabled={createExport.isPending}>
            <Plus aria-hidden="true" />
            Gerar CSV
          </Button>
        ) : null}
      </CardContent>
    </Card>
  );
}
