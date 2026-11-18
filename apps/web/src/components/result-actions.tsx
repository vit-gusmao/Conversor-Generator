import type { ResultOverview } from '@pdf2x/contracts';
import { Download, FileSpreadsheet, FileText, Loader2, MoreHorizontal, ShieldCheck, Table2 } from 'lucide-react';
import { Link } from 'react-router-dom';

import { Button } from '@/components/ui/button';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import { useDownloadExcel, useDownloadExport, useExports } from '@/hooks/use-conversions';
import { useEnsureSession } from '@/hooks/use-session';
import { explainError } from '@/lib/errors';
import { basename } from '@/lib/format';
import { downloadBlob } from '@/lib/download';
import { toast } from '@/stores/ui';

export interface ResultActionsProps {
  conversionId: string;
  overview: ResultOverview;
  filename: string;
}

const FORMAT_LABEL: Record<string, string> = {
  xlsx: 'Excel (.xlsx)',
  txt: 'Texto (.txt)',
  csv: 'CSV',
  zip: 'CSV (pacote .zip)',
};

/**
 * Derives the visible actions from the artefacts that actually exist.
 *
 * An image conversion has no workbook: offering "Baixar Excel" there would be
 * a dead button. XLSX is primary when published, TXT is primary otherwise.
 */
export function ResultActions({ conversionId, overview, filename }: ResultActionsProps) {
  useEnsureSession();
  const exportsQuery = useExports(conversionId);
  const downloadExcel = useDownloadExcel();
  const downloadExport = useDownloadExport(conversionId);

  const approved = (exportsQuery.data?.items ?? []).filter(
    (artifact) => artifact.publication_status === 'approved',
  );
  const byFormat = new Map(approved.map((artifact) => [artifact.format, artifact]));

  const hasExcel = overview.available_formats.includes('xlsx') || byFormat.has('xlsx');
  const txtArtifact = byFormat.get('txt');
  const csvArtifact = byFormat.get('csv') ?? byFormat.get('zip');
  const others = approved.filter(
    (artifact) => !['xlsx', 'txt', 'csv', 'zip'].includes(artifact.format),
  );

  const base = basename(filename).replace(/\.[^.]+$/, '');
  const downloadsDisabled = overview.publication_status === 'quarantined';

  function saveExcel() {
    downloadExcel.mutate(conversionId, {
      onSuccess: (blob) => {
        downloadBlob(blob, `${base}.xlsx`);
        toast({ title: 'Download iniciado', description: 'O arquivo .xlsx foi gerado.', variant: 'success' });
      },
      onError: (cause) => toast({ title: 'Falha no download', description: explainError(cause), variant: 'error' }),
    });
  }

  function saveExport(exportId: string, name: string) {
    downloadExport.mutate(exportId, {
      onSuccess: (blob) => {
        downloadBlob(blob, name);
        toast({ title: 'Download iniciado', description: name, variant: 'success' });
      },
      onError: (cause) => toast({ title: 'Falha no download', description: explainError(cause), variant: 'error' }),
    });
  }

  if (downloadsDisabled) {
    return (
      <div className="flex flex-wrap items-center gap-2">
        <Button disabled>
          <Download aria-hidden="true" />
          Downloads indisponíveis
        </Button>
        <Button asChild variant="secondary">
          <Link to={`/conversions/${conversionId}/audit`}>
            <ShieldCheck aria-hidden="true" />
            Ver auditoria
          </Link>
        </Button>
      </div>
    );
  }

  return (
    <div className="flex flex-wrap items-center gap-2">
      {hasExcel ? (
        <Button onClick={saveExcel} disabled={downloadExcel.isPending}>
          {downloadExcel.isPending ? (
            <Loader2 className="animate-spin motion-reduce:animate-none" aria-hidden="true" />
          ) : (
            <FileSpreadsheet aria-hidden="true" />
          )}
          {downloadExcel.isPending ? 'Preparando…' : 'Baixar Excel'}
        </Button>
      ) : txtArtifact ? (
        <Button
          onClick={() => saveExport(txtArtifact.export_id, `${base}.txt`)}
          disabled={downloadExport.isPending}
        >
          {downloadExport.isPending ? (
            <Loader2 className="animate-spin motion-reduce:animate-none" aria-hidden="true" />
          ) : (
            <FileText aria-hidden="true" />
          )}
          Baixar texto
        </Button>
      ) : null}

      {hasExcel && txtArtifact ? (
        <Button
          variant="secondary"
          onClick={() => saveExport(txtArtifact.export_id, `${base}.txt`)}
          disabled={downloadExport.isPending}
        >
          <FileText aria-hidden="true" />
          Texto
        </Button>
      ) : null}

      {csvArtifact ? (
        <Button
          variant="outline"
          onClick={() =>
            saveExport(
              csvArtifact.export_id,
              `${base}.${csvArtifact.format === 'zip' ? 'zip' : 'csv'}`,
            )
          }
          disabled={downloadExport.isPending}
        >
          <Table2 aria-hidden="true" />
          {FORMAT_LABEL[csvArtifact.format] ?? 'CSV'}
        </Button>
      ) : null}

      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button variant="ghost" aria-label="Mais ações e downloads">
            <MoreHorizontal aria-hidden="true" />
            Mais
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" className="w-64">
          <DropdownMenuLabel>Outros formatos</DropdownMenuLabel>
          {others.length === 0 ? (
            <DropdownMenuItem disabled>Nenhum artefato adicional</DropdownMenuItem>
          ) : (
            others.map((artifact) => (
              <DropdownMenuItem
                key={artifact.export_id}
                onSelect={() => saveExport(artifact.export_id, artifact.filename)}
              >
                <Download aria-hidden="true" />
                {FORMAT_LABEL[artifact.format] ?? artifact.format} · {artifact.filename}
              </DropdownMenuItem>
            ))
          )}
          <DropdownMenuSeparator />
          <DropdownMenuItem asChild>
            <Link to={`/conversions/${conversionId}/audit`}>
              <ShieldCheck aria-hidden="true" />
              Ver auditoria e evidências
            </Link>
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
    </div>
  );
}
